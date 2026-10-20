"""Geometry from a raster drawing view, without a language model.

Drawing lines come in two weights (ESKD): thick solid lines for visible outlines and thin lines for
dimensions, extension lines, axes and hatching. Removing the thin ones leaves the outline; flooding
the background from the image border then gives the silhouette of the part in that view.
"""
from __future__ import annotations

import numpy as np
from PIL import Image


def _erode(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out &= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def load_ink(path: str, upscale: int = 4, threshold: int = 150) -> np.ndarray:
    im = Image.open(path).convert("L")
    im = im.resize((im.width * upscale, im.height * upscale), Image.BICUBIC)
    return np.asarray(im) < threshold


def stroke_widths(ink: np.ndarray) -> np.ndarray:
    """Histogram of horizontal+vertical run lengths of ink (to tell thick lines from thin)."""
    runs = []
    for arr in (ink, ink.T):
        for row in arr:
            d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
            starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
            runs.extend((ends - starts).tolist())
    return np.bincount(np.array(runs)) if runs else np.zeros(1)


def thick_lines(ink: np.ndarray, r: int) -> np.ndarray:
    """Morphological opening: keeps strokes wider than 2r+1 pixels."""
    return _dilate(_erode(ink, r), r)


def flood_background(mask: np.ndarray) -> np.ndarray:
    """Pixels reachable from the border without crossing `mask`."""
    h, w = mask.shape
    bg = np.zeros_like(mask)
    stack = [(y, x) for x in range(w) for y in (0, h - 1) if not mask[y, x]] + \
            [(y, x) for y in range(h) for x in (0, w - 1) if not mask[y, x]]
    for y, x in stack:
        bg[y, x] = True
    while stack:
        y, x = stack.pop()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < h and 0 <= nx < w and not bg[ny, nx] and not mask[ny, nx]:
                bg[ny, nx] = True
                stack.append((ny, nx))
    return bg


def silhouette(path: str, upscale: int = 4, inset_stroke: bool = False) -> dict:
    """Silhouette of the part in one view: mask, bounding box (px) and the thick-line mask."""
    ink = load_ink(path, upscale)
    hist = stroke_widths(ink)
    # outline thickness differs a lot between a clean CAD export and a low-resolution scan: take the
    # typical width of the heaviest common strokes and keep only strokes at least ~60 % that wide
    w = np.arange(len(hist))
    sel = (w >= 2) & (w <= 8 * upscale)
    weights = hist[sel] * w[sel]
    if weights.sum():
        cum = np.cumsum(weights) / weights.sum()
        w_thick = int(w[sel][np.searchsorted(cum, 0.5)])
    else:
        w_thick = upscale
    thick = thick_lines(ink, max(1, min(upscale // 2, int(w_thick * 0.3))))
    # close small gaps in the outline (antialiasing, line joins)
    closed = _dilate(thick, 1)
    bg = flood_background(closed)
    solid = ~bg
    # keep the largest connected solid region
    from collections import deque
    h, w = solid.shape
    seen = np.zeros_like(solid)
    best = None
    for y0, x0 in zip(*np.nonzero(solid)):
        if seen[y0, x0]:
            continue
        q = deque([(y0, x0)])
        seen[y0, x0] = True
        pix = []
        while q:
            y, x = q.popleft()
            pix.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and solid[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        if best is None or len(pix) > len(best):
            best = pix
    region = np.zeros_like(solid)
    ys, xs = zip(*best)
    region[list(ys), list(xs)] = True
    # the silhouette includes the outline stroke itself; the geometric edge runs along its middle,
    # so the box is moved in by half a stroke on every side
    x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
    bbox = (x0, y0, x1, y1)
    if inset_stroke:  # exact renders (vector files): the scale is known, so the half-stroke matters
        i = max(0, int(round(w_thick / 2)))
        if x1 - x0 > 4 * i and y1 - y0 > 4 * i:
            bbox = (x0 + i, y0 + i, x1 - i, y1 - i)
            region = _erode(region, i)  # every edge, inner ones too, onto the middle of its stroke
    return {"mask": region, "thick": thick, "ink": ink, "bbox": bbox, "stroke_px": w_thick,
            "stroke_hist": hist[:12].tolist()}


def orthogonal_profile(mask: np.ndarray, bbox, size_mm: tuple[float, float], step: float = 0.5,
                       min_feature_mm: float = 2.0) -> list[list[float]]:
    """Turns a silhouette made of horizontal/vertical edges into a closed polyline in mm (origin at the
    bottom-left of the bounding box, Y up). Bumps and notches smaller than `min_feature_mm` (dimension
    digits touching the outline, line joins) are removed."""
    x0, y0, x1, y1 = bbox
    sx, sy = size_mm[0] / (x1 - x0), size_mm[1] / (y1 - y0)
    nx, ny = int(round(size_mm[0] / step)), int(round(size_mm[1] / step))
    grid = np.zeros((ny, nx), dtype=bool)
    for j in range(ny):
        py = int(y1 - (j + 0.5) * step / sy)
        for i in range(nx):
            px = int(x0 + (i + 0.5) * step / sx)
            grid[j, i] = mask[min(max(py, y0), y1 - 1), min(max(px, x0), x1 - 1)]
    r = max(1, int(round(min_feature_mm / step / 2)))
    grid = _dilate(_erode(grid, r), r)  # opening: removes thin bumps
    grid = _erode(_dilate(grid, r), r)  # closing: fills thin notches
    return trace_grid(grid, step)


def snap_profile(points: list[list[float]], xs: list[float], ys: list[float], tol: float = 2.0) -> list[list[float]]:
    """Moves each coordinate to the nearest value that follows from the drawing's dimensions (if one is
    within `tol` mm), then removes the points that became redundant."""
    def snap(v, cands):
        best = min(cands, key=lambda c: (round(abs(c - v), 2), cands.index(c)), default=None)
        return best if best is not None and abs(best - v) <= tol else round(v * 2) / 2
    pts = [[snap(x, xs), snap(y, ys)] for x, y in points]
    out = []
    for p in pts:
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        for k in range(len(out)):
            a, b, c = out[k - 1], out[k], out[(k + 1) % len(out)]
            if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) == 0:
                del out[k]
                changed = True
                break
    return out


def candidates(total: float, values: list[float]) -> list[float]:
    """Coordinates a dimension can produce along an axis of length `total`: the value itself, measured
    from the far end, and centred (symmetric features)."""
    primary = {0.0, float(total), total / 2}  # the middle: axes of symmetry are rarely dimensioned
    for v in values:
        primary |= {v, total - v, (total - v) / 2, (total + v) / 2}
    secondary = {abs(v - w) for v in values for w in values}  # chains: "20" and "15" give a level at 5
    # order matters: on a tie the snap prefers the direct readings of a dimension
    first = sorted(c for c in primary if 0 <= c <= total)
    return first + sorted(c for c in secondary - primary if 0 <= c <= total)


def trace_grid(grid: np.ndarray, step: float) -> list[list[float]]:
    """Outer boundary of the filled cells of a boolean grid as a closed orthogonal polyline."""
    ny, nx = grid.shape
    g = np.pad(grid, 1)
    edges = {}
    for j in range(ny):
        for i in range(nx):
            if not grid[j, i]:
                continue
            # cell (i, j) spans x i..i+1, y j..j+1 (y up); add boundary edges counter-clockwise
            if not g[j + 1, i + 2]:
                edges[(i + 1, j)] = (i + 1, j + 1)
            if not g[j + 2, i + 1]:
                edges[(i + 1, j + 1)] = (i, j + 1)
            if not g[j + 1, i]:
                edges[(i, j + 1)] = (i, j)
            if not g[j, i + 1]:
                edges[(i, j)] = (i + 1, j)
    if not edges:
        return []
    start = min(edges)
    pts = [start]
    cur = edges[start]
    while cur != start and len(pts) < 100000:
        pts.append(cur)
        cur = edges[cur]
    # drop collinear points
    out = []
    n = len(pts)
    for k in range(n):
        a, b, c = pts[k - 1], pts[k], pts[(k + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
            out.append([b[0] * step, b[1] * step])
    return out


def find_circles(path: str, bbox, size_mm, diameters: list[float], upscale: int = 4,
                 min_score: float = 0.6) -> list[dict]:
    """Finds circles of the given diameters (read from the drawing) by matching a ring against the
    thick lines (a Hough transform restricted to known radii). Returns centres in mm."""
    sil = silhouette(path, upscale)
    ink = _dilate(sil["thick"], 1).astype(np.float32)
    x0, y0, x1, y1 = bbox
    sx, sy = size_mm[0] / (x1 - x0), size_mm[1] / (y1 - y0)
    s = (sx + sy) / 2
    h, w = ink.shape
    F = np.fft.rfft2(ink)
    found = []
    for d in sorted(set(diameters), reverse=True):
        r = d / 2 / s
        if r < 3:
            continue
        yy, xx = np.mgrid[-int(r) - 3:int(r) + 4, -int(r) - 3:int(r) + 4]
        ring = (np.abs(np.hypot(yy, xx) - r) <= 1.0).astype(np.float32)
        k = np.zeros_like(ink)
        kh, kw = ring.shape
        k[:kh, :kw] = ring
        k = np.roll(np.roll(k, -(kh // 2), 0), -(kw // 2), 1)
        score = np.fft.irfft2(F * np.conj(np.fft.rfft2(k)), s=ink.shape) / ring.sum()
        score[: y0, :] = 0
        score[y1:, :] = 0
        score[:, : x0] = 0
        score[:, x1:] = 0
        while True:
            j, i = np.unravel_index(np.argmax(score), score.shape)
            if score[j, i] < min_score:
                break
            cx, cy = (i - x0) * sx, (y1 - j) * sy
            if not any(abs(cx - c["x"]) < 1 and abs(cy - c["y"]) < 1 and c["d"] > d for c in found):
                found.append({"x": round(float(cx), 1), "y": round(float(cy), 1), "d": d,
                              "score": round(float(score[j, i]), 2)})
            rr = int(r * 0.8)
            score[max(0, j - rr): j + rr + 1, max(0, i - rr): i + rr + 1] = 0
    return found


def dashed_segments(path: str, bbox, size_mm, upscale: int = 4) -> dict:
    """Hidden (dashed) lines of a view as segments in mm: {"h": [(z, x0, x1)], "v": [(x, z0, z1)]}.

    Dashes are thin strokes; a row/column of the thin-line mask counts as a dashed line where short
    ink runs alternate with short gaps over a few millimetres (solid thin lines — dimension and
    extension lines — have no gaps; dash-dot axes have long dashes)."""
    sil = silhouette(path, upscale)
    # dashes of hidden lines are often drawn as heavy as outlines, so all ink is scanned; crossings of
    # perpendicular lines (short runs) are told apart from dashes by the dash length
    thin = sil["ink"]
    x0, y0, x1, y1 = bbox
    sx, sy = size_mm[0] / (x1 - x0), size_mm[1] / (y1 - y0)
    out = {"h": [], "v": []}
    for orient, arr, scale_along, scale_across in (("h", thin, sx, sy), ("v", thin.T, sy, sx)):
        lo, hi = (y0, y1) if orient == "h" else (x0, x1)
        a0, a1 = (x0, x1) if orient == "h" else (y0, y1)
        rows = []
        for r in range(lo, hi):
            line = arr[r, a0:a1]
            d = np.diff(np.concatenate(([0], line.astype(np.int8), [0])))
            starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
            if len(starts) < 2:
                continue
            # group runs into chains with gaps shorter than 2 mm
            chain = [(starts[0], ends[0])]
            for s, e in zip(starts[1:], ends[1:]):
                if (s - chain[-1][1]) * scale_along < 2.0:
                    chain.append((s, e))
                else:
                    rows.append((r, chain))
                    chain = [(s, e)]
            rows.append((r, chain))
        for r, chain in rows:
            if len(chain) < 3:
                continue
            length = (chain[-1][1] - chain[0][0]) * scale_along
            ink = sum(e - s for s, e in chain) * scale_along
            dashes = sorted((e - s) * scale_along for s, e in chain)
            longest, median = dashes[-1], dashes[len(dashes) // 2]
            if length >= 3 and 0.35 < ink / length < 0.85 and longest < 6 and median >= 1.0:
                c0, c1 = chain[0][0] + a0, chain[-1][1] + a0
                if orient == "h":
                    out["h"].append(((y1 - r) * sy, (c0 - x0) * sx, (c1 - x0) * sx))
                else:
                    out["v"].append(((r - x0) * sx, (y1 - c1) * sy, (y1 - c0) * sy))
    # a drawn line is several pixels thick: merge neighbouring rows
    for k in ("h", "v"):
        segs = sorted(out[k])
        merged = []
        for s in segs:
            if merged and abs(s[0] - merged[-1][0]) < 0.8 and s[1] < merged[-1][2] and s[2] > merged[-1][1]:
                m = merged[-1]
                merged[-1] = ((m[0] + s[0]) / 2, min(m[1], s[1]), max(m[2], s[2]))
            else:
                merged.append(s)
        out[k] = [tuple(round(float(v), 1) for v in s) for s in merged]
    return out


def ring_score(thick: np.ndarray, cx: float, cy: float, r: float, a0: float = 0, a1: float = 360) -> float:
    """Share of points of an arc (pixel coords, angles in degrees, y up) that lie on thick lines."""
    ink = _dilate(thick, 1)
    angles = np.radians(np.arange(a0, a1, 2.0))
    xs = np.round(cx + r * np.cos(angles)).astype(int)
    ys = np.round(cy - r * np.sin(angles)).astype(int)
    ok = (xs >= 0) & (ys >= 0) & (xs < ink.shape[1]) & (ys < ink.shape[0])
    return float(ink[ys[ok], xs[ok]].mean()) if ok.any() else 0.0


def segment_score(thick: np.ndarray, p0, p1) -> float:
    ink = _dilate(thick, 1)
    n = int(max(abs(p1[0] - p0[0]), abs(p1[1] - p0[1]))) + 1
    xs = np.round(np.linspace(p0[0], p1[0], n)).astype(int)
    ys = np.round(np.linspace(p0[1], p1[1], n)).astype(int)
    return float(ink[ys, xs].mean())


def _snap(v: float, cands: list[float], tol: float = 1.5) -> float:
    best = min(cands, key=lambda c: (round(abs(c - v), 2), cands.index(c)), default=None)
    return float(best) if best is not None and abs(best - v) <= tol else round(v * 2) / 2


def _profile_span(profile: list[list[float]], x: float) -> tuple[float, float]:
    """Lowest and highest Z of the front profile at a given X (where the vertical line x crosses it)."""
    zs = []
    n = len(profile)
    for k in range(n):
        (ax, az), (bx, bz) = profile[k], profile[(k + 1) % n]
        if az == bz and min(ax, bx) <= x <= max(ax, bx):
            zs.append(az)
    return (min(zs), max(zs)) if zs else (0.0, 0.0)


def view_grid(path: str, size_mm: tuple[float, float], step: float = 0.5, upscale: int = 4,
              min_feature_mm: float = 2.0, inset_stroke: bool = False) -> np.ndarray:
    """Silhouette of a view sampled on a mm grid: grid[v, u] with v up (row 0 = lowest)."""
    s = silhouette(path, upscale, inset_stroke)
    x0, y0, x1, y1 = s["bbox"]
    nx, ny = int(round(size_mm[0] / step)), int(round(size_mm[1] / step))
    us = (x0 + (np.arange(nx) + 0.5) * (x1 - x0) / nx).astype(int)
    vs = (y1 - (np.arange(ny) + 0.5) * (y1 - y0) / ny).astype(int)
    grid = s["mask"][np.clip(vs, y0, y1 - 1)][:, np.clip(us, x0, x1 - 1)]
    r = max(1, int(round(min_feature_mm / step / 2)))
    return _erode(_dilate(_dilate(_erode(grid, r), r), r), r)


def transfer_side_view(front: np.ndarray, top: np.ndarray) -> np.ndarray:
    """«Метод переноса»: the left view implied by the front view F[z, x] and the top view T[y, x].
    A point (y, z) of the left view is material when some x is material in both views — exactly what
    the horizontal transfer lines (heights) and the 45° line (depths) construct on paper."""
    # F[z, x] & T[y, x] → any over x
    return (front[:, None, :] & top[None, :, :]).any(axis=2)  # [z, y]


def draw_transfer(front: np.ndarray, top: np.ndarray, side: np.ndarray, step: float, out: str,
                  drawn_side: np.ndarray | None = None) -> str:
    """Draws the construction like a textbook: front view top-left, top view below it, the left view
    to the right, horizontal transfer lines for heights and the 45° constant line for depths."""
    from PIL import ImageDraw, ImageFont
    k = 4                                   # px per grid cell
    nz, nx = front.shape
    ny = top.shape[0]
    gap = 40
    Wd = 40 + nx * k + gap + max(ny * k, 340) + 40
    Hd = 50 + nz * k + gap + ny * k + 50
    img = Image.new("RGB", (Wd, Hd), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    ox, oy = 40, 50                         # front view: top-left corner of its box
    tx, ty = ox, oy + nz * k + gap          # top view (below the front view)
    sx, sy = ox + nx * k + gap, oy          # left view (to the right of the front view)
    ink, grey, red, fill = (20, 60, 120), (150, 150, 150), (210, 60, 42), (220, 233, 245)

    def poly(grid, flip_u=False):
        g = grid[:, ::-1] if flip_u else grid
        return [(u / step, v / step) for u, v in trace_grid(g, step)], g

    def draw_view(grid, x0, y0, flip_u=False, color=ink):
        pts, g = poly(grid, flip_u)
        rows = g.shape[0]
        if len(pts) > 2:
            xy = [(x0 + u * k, y0 + (rows - v) * k) for u, v in pts]
            d.polygon(xy, fill=fill, outline=color)
            d.line(xy + [xy[0]], fill=color, width=2)
        return pts

    fpts = draw_view(front, ox, oy)
    tpts = draw_view(top, tx, ty)
    # the left view is seen from the left: depth (Y) grows to the LEFT in the picture
    spts = draw_view(side, sx, sy, flip_u=True)
    if drawn_side is not None:
        diff = (drawn_side != side)[:, ::-1]
        for v, u in zip(*np.nonzero(diff)):
            X, Y = sx + u * k, sy + (nz - 1 - v) * k
            d.rectangle([X, Y, X + k - 1, Y + k - 1], fill=(245, 170, 150))
    # 45° constant line through the corner between the top view and the left view
    d.line([sx, ty, sx + ny * k, ty + ny * k], fill=red, width=2)
    # heights: horizontal lines from every corner of the front view to the left view
    for z in sorted({v for _, v in fpts}):
        Y = oy + (nz - z) * k
        d.line([ox + nx * k + 2, Y, sx + ny * k, Y], fill=grey, width=1)
    # depths: from every corner of the top view across to the 45° line, then up into the left view
    for y in sorted({v for _, v in tpts}):
        Yt = ty + (ny - y) * k
        Xc = sx + (ny - y) * k            # point on the 45° line
        d.line([tx + nx * k + 2, Yt, Xc, Yt], fill=grey, width=1)
        d.line([Xc, Yt, Xc, sy + nz * k + 2], fill=grey, width=1)
        d.ellipse([Xc - 3, Yt - 3, Xc + 3, Yt + 3], fill=red)
    d.text((ox, oy - 30), "Главный вид", fill=ink, font=font)
    d.text((tx, ty + ny * k + 10), "Вид сверху", fill=ink, font=font)
    d.text((sx, sy - 44), "Вид слева, построенный переносом", fill=ink, font=font)
    if drawn_side is not None:
        d.text((sx, sy - 26), "красным — отличия от вида слева на чертеже", fill=red, font=font)
    d.text((sx + ny * k * 0.55, ty + ny * k * 0.35), "45°", fill=red, font=font)
    img.save(out)
    return out


def estimate_sizes(front: str, top: str, length: float, upscale: int = 4) -> dict:
    """Height and width implied by the proportions of the views (the drawing is to scale), given the
    length: a check on numbers the model read, and a fallback when an overall size was missed."""
    f, t = silhouette(front, upscale), silhouette(top, upscale)
    fx0, fy0, fx1, fy1 = f["bbox"]
    tx0, ty0, tx1, ty1 = t["bbox"]
    return {"height": length * (fy1 - fy0) / (fx1 - fx0), "width": length * (ty1 - ty0) / (tx1 - tx0),
            "length_top": (tx1 - tx0) / (fx1 - fx0)}


def reconstruct(front: str, top: str, d: dict, upscale: int = 4, answers: dict | None = None,
                side: str | None = None, construction_png: str | None = None, exact: bool = False,
                top_section: bool = False, side_section: bool = False) -> dict:
    """Builds a build_part plan from a front view and a top view (first-angle projection).

    d: {"length", "width", "height"} overall sizes and lists of numbers read from the drawing:
    "x" (lengths), "y" (widths), "z" (heights), "diameters", "radii".
    Shapes come from the pictures; numbers only scale and snap them. Whatever the pictures cannot
    settle is returned as questions with a proposed answer."""
    L, W, H = d["length"], d["width"], d["height"]
    answers = answers or {}

    def ask(qid: str, text: str, guess: float, found: float | None) -> float:
        """A value read from the picture is used as is; otherwise the user is asked (with a guess).
        An answer the user already gave always wins."""
        if qid in answers:
            return float(answers[qid])
        if found is not None:
            return found
        questions.append({"id": qid, "text": text, "default": guess, "unit": "мм"})
        return guess

    xs = candidates(L, d.get("x", []))
    ys = candidates(W, d.get("y", []))
    zs = candidates(H, d.get("z", []))
    notes, questions = [], []

    # 1. front silhouette → profile on XZ, extruded through the width
    f = silhouette(front, upscale, exact)
    profile = snap_profile(orthogonal_profile(f["mask"], f["bbox"], (L, H)), xs, zs)
    ops = [{"op": "extrude", "plane": "XZ", "depth": W, "shapes": [{"type": "polyline", "points": profile}],
            "comment": "контур главного вида"}]

    # 2. top silhouette: if it is not the full rectangle, cut away what lies outside it
    t = silhouette(top, upscale, exact)
    outline = snap_profile(orthogonal_profile(t["mask"], t["bbox"], (L, W)), xs, ys)
    # a section in place of the top view shows the part only where it is cut: its outline is not
    # the part's outline along the whole height (the cavities come from the main view instead)
    if not top_section and sorted(map(tuple, outline)) != sorted([(0, 0), (L, 0), (L, W), (0, W)]):
        ops.append({"op": "cut", "plane": "XY", "through_all": True, "comment": "контур вида сверху",
                    "shapes": [{"type": "rect", "x": -1, "y": -1, "w": L + 2, "h": W + 2},
                               {"type": "polyline", "points": outline}]})

    # 2b. the left view: built from the two others by transfer (45° line); if the drawing has its own
    #     left view, features seen only there (a slot along X, for instance) are cut by intersection
    step = 0.5
    gf = view_grid(front, (L, H), step, upscale, inset_stroke=exact)
    gt = view_grid(top, (L, W), step, upscale, inset_stroke=exact)
    built = transfer_side_view(gf, gt)
    drawn = None
    if side:
        g = view_grid(side, (W, H), step, upscale, inset_stroke=exact)          # [z, u], u grows to the right in the picture
        drawn = g[:, ::-1]                                   # → [z, y]: depth grows to the left
        agree = (drawn == built).mean()
        extra = built & ~drawn
        if extra.mean() > 0.005 and not side_section:
            sprof = snap_profile(trace_grid(drawn, step), ys, zs)  # (y, z) polyline
            ops.append({"op": "cut", "plane": "YZ", "through_all": True, "comment": "контур вида слева",
                        "shapes": [{"type": "rect", "x": -1, "y": -1, "w": W + 2, "h": H + 2},
                                   {"type": "polyline", "points": sprof}]})
            notes.append(f"На виде слева есть элементы, которых нет на главном и виде сверху "
                         f"(совпадение с построенным {agree:.0%}): учтены пересечением трёх видов")
        else:
            notes.append(f"Вид слева на чертеже совпадает с построенным переносом ({agree:.0%})")
    if construction_png:
        draw_transfer(gf, gt, built, step, construction_png, drawn)

    dashed = dashed_segments(front, f["bbox"], (L, H), upscale)
    tx0, ty0, tx1, ty1 = t["bbox"]
    tsx, tsy = L / (tx1 - tx0), W / (ty1 - ty0)
    to_px = lambda x, y: (tx0 + x / tsx, ty1 - y / tsy)  # noqa: E731
    thick_top = t["thick"]

    def dashed_v(x, tol=1.0):
        return [(z0, z1) for vx, z0, z1 in dashed["v"] if abs(vx - x) <= tol]

    # 3. holes seen end-on in the top view
    # a vector drawing has its circles as entities (taken from the file elsewhere); finding them on
    # the picture of a view full of lines only invents holes
    holes = [] if top_section or exact else \
        find_circles(top, t["bbox"], (L, W), d.get("diameters", []), upscale, min_score=0.75)
    for h in holes:
        cx, cy, dia = _snap(h["x"], xs), _snap(h["y"], ys), h["d"]
        r = dia / 2
        zlo, ztop = _profile_span(profile, cx)
        # U-shaped recess of a radius from the drawing around the hole, open towards the nearer end
        side = 1 if cx > L / 2 else -1
        end = L if side > 0 else 0
        pocket = None
        for R in sorted(d.get("radii", []), reverse=True):
            if R <= r:
                continue
            px, py = to_px(cx, cy)
            a0, a1 = (90, 270) if side > 0 else (-90, 90)
            arc = ring_score(thick_top, px, py, R / tsx, a0, a1)
            lines = (segment_score(thick_top, to_px(cx, cy - R), to_px(end, cy - R)) +
                     segment_score(thick_top, to_px(cx, cy + R), to_px(end, cy + R))) / 2
            if arc > 0.6 and lines > 0.6:
                floors = [z for z, hx0, hx1 in dashed["h"]
                          if min(hx0, hx1) <= min(end, cx - side * R) + 1 and max(hx0, hx1) >= max(end, cx - side * R) - 1]
                floor = ask(f"pocket_{cx:g}", f"Подрезка R{R:g} у отверстия Ø{dia:g} (X={cx:g}): на какой высоте её дно?",
                            zlo + (ztop - zlo) / 2, _snap(min(floors), zs) if floors else None)
                pocket = {"R": R, "floor": floor}
                ops.append({"op": "cut", "plane": "XY", "offset": ztop, "depth": ztop - floor, "reverse": True,
                            "comment": f"U-подрезка R{R:g} до высоты {floor:g}",
                            "shapes": [{"type": "line", "x1": end, "y1": cy - R, "x2": cx, "y2": cy - R},
                                       {"type": "arc", "x": cx, "y": cy, "r": R, "a1": a0, "a2": a1},
                                       {"type": "line", "x1": cx, "y1": cy + R, "x2": end, "y2": cy + R},
                                       {"type": "line", "x1": end, "y1": cy + R, "x2": end, "y2": cy - R}]})
                notes.append(f"Ø{dia:g} в X={cx:g}: U-подрезка R{R:g}, дно на высоте {floor:g} (по штриховой линии на главном виде)")
                break
        top_z = pocket["floor"] if pocket else ztop
        # a square inscribed in the circle, drawn as a diamond, means a stepped hole: circle on top, square below
        px, py = to_px(cx, cy)
        rp = r / tsx
        diamond = [(px + rp, py), (px, py - rp), (px - rp, py), (px, py + rp)]
        dscore = sum(segment_score(thick_top, diamond[k], diamond[(k + 1) % 4]) for k in range(4)) / 4
        # control: the same square turned 45° (axis-aligned) must NOT be on the lines, otherwise the
        # match only comes from the circle itself (small holes) or from crossing lines
        q = rp / np.sqrt(2)
        square = [(px + q, py + q), (px + q, py - q), (px - q, py - q), (px - q, py + q)]
        control = sum(segment_score(thick_top, square[k], square[(k + 1) % 4]) for k in range(4)) / 4
        # hidden edges of this hole: vertical dashes at its sides, inside the body below its top
        spans = [(z0, z1) for z0, z1 in dashed_v(cx - r) + dashed_v(cx + r)
                 if z0 >= zlo - 0.5 and z1 <= top_z + 0.5 and z1 - z0 > 1]
        if dscore > 0.6 and dscore - control > 0.3:
            steps = [z for z, hx0, hx1 in dashed["h"] if abs(hx0 - (cx - r)) < 1.5 and abs(hx1 - (cx + r)) < 1.5
                     and zlo < z < top_z]
            step = ask(f"step_{cx:g}", f"Ø{dia:g} в X={cx:g}: до какой высоты идёт круглая часть (ниже — квадрат)?",
                       (zlo + top_z) / 2, _snap(max(steps), zs) if steps else None)
            ops.append({"op": "cut", "plane": "XY", "offset": top_z, "depth": top_z - step, "reverse": True,
                        "comment": f"отверстие Ø{dia:g} до высоты {step:g}",
                        "shapes": [{"type": "circle", "x": cx, "y": cy, "d": dia}]})
            ops.append({"op": "cut", "plane": "XY", "offset": step, "reverse": True, "through_all": True,
                        "comment": "квадрат, вписанный в отверстие",
                        "shapes": [{"type": "polyline", "points": [[cx + r, cy], [cx, cy + r], [cx - r, cy], [cx, cy - r]]}]})
            notes.append(f"Ø{dia:g} в X={cx:g}: ступенчатое — окружность до {step:g}, ниже вписанный квадрат")
            continue
        bottom = min((z0 for z0, _ in spans), default=None)
        if not spans:  # no hidden edges found: through is the usual case, but let the user say
            bottom = ask(f"hole_{cx:g}_{cy:g}", f"Ø{dia:g} в X={cx:g}, Y={cy:g}: до какой высоты идёт отверстие "
                         f"({zlo:g} — насквозь)?", zlo, None)
        if bottom is not None and zlo + 0.5 < bottom < top_z - 0.5:
            depth = top_z - _snap(bottom, zs)
            ops.append({"op": "cut", "plane": "XY", "offset": top_z, "depth": depth, "reverse": True,
                        "comment": f"глухое отверстие Ø{dia:g} глубиной {depth:g}",
                        "shapes": [{"type": "circle", "x": cx, "y": cy, "d": dia}]})
            notes.append(f"Ø{dia:g} в X={cx:g}: глухое, глубина {depth:g}")
        else:
            ops.append({"op": "cut", "plane": "XY", "offset": top_z, "reverse": True, "through_all": True,
                        "comment": f"сквозное отверстие Ø{dia:g}",
                        "shapes": [{"type": "circle", "x": cx, "y": cy, "d": dia}]})
            notes.append(f"Ø{dia:g} в X={cx:g}: сквозное")
    return {"operations": ops, "notes": notes, "questions": questions, "profile": profile,
            "dashed": dashed, "holes": holes, "construction": construction_png}


def circles(path: str, bbox, size_mm, upscale: int = 4) -> list[dict]:
    """Closed round outlines inside a view (holes seen end-on): centre and diameter in mm."""
    sil = silhouette(path, upscale)
    thick = sil["thick"]
    x0, y0, x1, y1 = bbox
    sx, sy = size_mm[0] / (x1 - x0), size_mm[1] / (y1 - y0)
    # background inside the silhouette that is enclosed by thick lines = candidate holes
    inner = ~_dilate(thick, 1) & sil["mask"]
    from collections import deque
    h, w = inner.shape
    seen = np.zeros_like(inner)
    found = []
    for yy, xx in zip(*np.nonzero(inner)):
        if seen[yy, xx]:
            continue
        q = deque([(yy, xx)])
        seen[yy, xx] = True
        pix = []
        while q:
            y, x = q.popleft()
            pix.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and inner[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        ys = np.array([p[0] for p in pix])
        xs = np.array([p[1] for p in pix])
        bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
        fill = len(pix) / (bw * bh)
        # a disc fills π/4 ≈ 0.785 of its bounding square
        if 0.6 < bw / bh < 1.6 and 0.65 < fill < 0.9 and bw > 6:
            # centre and size from the bounding box: the area is unreliable when lines cross the hole
            cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
            line = 2 * (upscale // 2 + 1)  # the thick outline eats into the inner region on both sides
            d_mm = ((bw + line) * sx + (bh + line) * sy) / 2
            found.append({"x": round(float((cx - x0) * sx), 1), "y": round(float((y1 - cy) * sy), 1),
                          "d": round(float(d_mm), 1), "fill": round(float(fill), 2)})
    return found
