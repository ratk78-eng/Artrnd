"""Bodies of revolution drawn as one longitudinal section (shafts, bushings, fittings).

Such a drawing has a single view: a horizontal axis (dash-dot line) and the section of the part
around it — metal hatched, the bore white. The profile above the axis, revolved about it, is the
part. Steps:
  1. the axis: the long dash-dot row through the view;
  2. the metal: the outline (thick lines) splits the view into closed areas; the hatched ones are
     metal (a section shows the cut material hatched), the bore and the outside are not;
  3. per column along the axis: outer and inner radius of the metal above the axis;
  4. the profile, simplified, scaled by the overall length and snapped to the drawing's numbers
     (diameters → radii, lengths → positions along the axis);
  5. one revolve operation about the axis.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

import vision


def find_axis(ink: np.ndarray) -> int | None:
    """Row of the axis: long, and made of many separate dashes (a dash-dot line)."""
    h, w = ink.shape
    best = None
    for y in range(h):
        row = ink[y]
        n = int(row.sum())
        if n < 0.3 * w:
            continue
        runs = int(np.count_nonzero(np.diff(row.astype(np.int8)) == 1))
        if runs < 8:
            continue  # a solid line (an edge), not an axis
        score = n * min(runs, 40)
        if best is None or score > best[0]:
            best = (score, y)
    return best[1] if best else None


def _runs(ink: np.ndarray) -> np.ndarray:
    """Per ink pixel: length of the horizontal ink run it belongs to."""
    out = np.zeros(ink.shape, dtype=np.int32)
    for y, row in enumerate(ink):
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        s, e = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        for a, b in zip(s.tolist(), e.tolist()):
            out[y, a:b] = b - a
    return out


def _stroke(ink: np.ndarray) -> int:
    """Width of the outline: per ink pixel the shorter of its horizontal and vertical run is the
    local stroke width; lines come in two weights, the outline is the heavier common one."""
    t = np.minimum(_runs(ink), _runs(ink.T).T)[ink]
    hist = np.bincount(np.clip(t, 0, 60))
    hist[:2] = 0
    if hist.sum() == 0:
        return 3
    # the two weights are the two highest peaks of the widths (hatching and thin lines; outline);
    # the outline is the wider of them — bold text and arrowheads make smaller peaks further out
    peaks = [i for i in range(2, len(hist) - 1) if hist[i] > 0 and hist[i] >= hist[i - 1] and hist[i] >= hist[i + 1]]
    peaks.sort(key=lambda i: -hist[i])
    if not peaks:
        return 3
    if len(peaks) > 1 and hist[peaks[1]] >= 0.1 * hist[peaks[0]]:
        return int(max(peaks[0], peaks[1]))
    return int(peaks[0])


def label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected components of a boolean image (run-length union-find: no scipy needed)."""
    h, w = mask.shape
    parent = [0]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    lab = np.zeros((h, w), dtype=np.int32)
    prev = []  # runs of the previous row: (x0, x1, label)
    for y in range(h):
        row = mask[y]
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        cur = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            lb = 0
            while j < len(prev) and prev[j][1] <= s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < e:
                pl = find(prev[k][2])
                if lb == 0:
                    lb = pl
                elif pl != lb:
                    parent[max(pl, lb)] = min(pl, lb)
                    lb = min(pl, lb)
                k += 1
            if lb == 0:
                lb = len(parent)
                parent.append(lb)
            cur.append((s, e, lb))
            lab[y, s:e] = lb
        prev = cur
    roots = np.array([find(i) for i in range(len(parent))], dtype=np.int32)
    lab = roots[lab]
    uniq, lab = np.unique(lab, return_inverse=True)
    return lab.reshape(h, w), len(uniq)


def metal_mask(ink: np.ndarray, axis_y: int) -> tuple[np.ndarray, int]:
    """Metal of the section: closed areas between the thick outline lines that are hatched."""
    stroke = _stroke(ink)
    thick = vision.thick_lines(ink, max(1, int(stroke * 0.35)))
    thick = vision._dilate(thick, 1)
    lab, n = label(~thick)
    thin = (ink & ~thick).astype(np.int64)
    area = np.bincount(lab.ravel(), minlength=n)
    hatch = np.bincount(lab.ravel(), weights=thin.ravel(), minlength=n)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])).tolist())
    good = np.zeros(n, dtype=bool)
    for i in range(n):
        # hatching: thin ink spread over a closed area (not the outside, not one leader line)
        if i not in border and area[i] >= 50 and hatch[i] / area[i] > 0.04:
            good[i] = True
    good[0] = False  # label 0: the strokes themselves
    metal = good[lab] & ~thick
    # the outline strokes between hatched areas are metal too
    metal = vision._erode(vision._dilate(metal, stroke + 2), stroke + 2)
    # the part is one piece in the section's plane on each side of the axis: what is not joined to
    # the biggest piece on its side (text inside leader frames, dimension arrows) is not metal
    keep = np.zeros_like(metal)
    for half in (slice(0, axis_y), slice(axis_y + 1, None)):
        m = np.zeros_like(metal)
        m[half] = metal[half]
        lab2, n2 = label(m)
        if n2 < 2:
            continue
        sizes = np.bincount(lab2.ravel())
        sizes[0] = 0
        keep |= lab2 == int(np.argmax(sizes))
    return keep, stroke


def _load(path: str, min_width: int = 2000) -> np.ndarray:
    """Ink of a view, enlarged when small: at a page rendered 2400 px wide thick and thin lines
    differ by a pixel, too little to tell outline from hatching; enlarged smoothly they separate."""
    im = Image.open(path).convert("L")
    if im.width < min_width:
        k = max(2, int(math.ceil(min_width / im.width)))
        im = im.resize((im.width * k, im.height * k), Image.BICUBIC)
    return np.asarray(im) < 150


def profile(path: str, length_mm: float | None = None, px_per_mm: float | None = None) -> dict:
    """Outer and inner radius along the axis, in mm, from a picture of the section."""
    ink = _load(path)
    axis = find_axis(ink)
    if axis is None:
        raise ValueError("на виде не найдена осевая линия")
    metal, stroke = metal_mask(ink, axis)
    up = metal[:axis][::-1]           # rows going up from the axis
    cols = np.nonzero(up.any(axis=0))[0]
    if not len(cols):
        raise ValueError("не найдена заштрихованная часть разреза над осью")
    x0, x1 = int(cols.min()), int(cols.max())
    # the metal ends on the inner side of the outline stroke (thickened by 1 px): the edge itself
    # runs along the middle of the stroke, half a stroke further out
    out = stroke / 2 + 1
    if px_per_mm is None:
        if not length_mm:
            raise ValueError("нужна общая длина детали, чтобы узнать масштаб")
        px_per_mm = (x1 - x0 + 1 + 2 * out - 1) / length_mm
    zs, ro, ri = [], [], []
    for x in range(x0, x1 + 1):
        col = np.nonzero(up[:, x])[0]
        if not len(col):
            continue
        zs.append((x - x0 + out - 0.5) / px_per_mm)
        ro.append((col.max() + 1 + out) / px_per_mm)   # row i above the axis is i+1 px from it
        ri.append(max(0.0, (col.min() + 1 - out) / px_per_mm) if col.min() > stroke + 2 else 0.0)
    return {"z": np.array(zs), "r_out": np.array(ro), "r_in": np.array(ri), "px_per_mm": px_per_mm,
            "axis_px": axis, "x_px": (x0, x1), "stroke_px": stroke, "metal": metal}


def has_axis(path: str) -> bool:
    """Cheap first look: a dash-dot axis across the view, somewhere in its middle part."""
    try:
        ink = np.asarray(Image.open(path).convert("L")) < 150
    except OSError:
        return False
    y = find_axis(ink)
    return y is not None and 0.15 * ink.shape[0] < y < 0.85 * ink.shape[0]


def detect(path: str) -> dict | None:
    """Is this view a longitudinal section of a body of revolution? An axis across it, hatched
    metal on both sides mirroring each other. Returns the profile in pixels (scale still unknown)
    or None."""
    try:
        ink = _load(path)
    except OSError:
        return None
    h, w = ink.shape
    if w < 3 * 40 or h < 40:
        return None
    axis = find_axis(ink)
    if axis is None or not (0.15 * h < axis < 0.85 * h):
        return None
    metal, stroke = metal_mask(ink, axis)
    n = min(axis, h - axis - 1)
    up = metal[axis - n:axis][::-1]
    down = metal[axis + 1:axis + 1 + n]
    if up.sum() < 200 or down.sum() < 200:
        return None
    inter = (up & down).sum()
    union = (up | down).sum()
    if inter / union < 0.75:
        return None  # not symmetric about the line: some other part with a centre line
    return {"axis_px": axis, "symmetry": float(inter / union), "stroke_px": stroke}


def flats(texts: str) -> list[dict]:
    """«2 лыски S11,4 … на Ø12,93, длина 9,29» → [{"n": 2, "s": 11.4, "d": 12.93, "l": 9.29}]."""
    import re
    out = []
    for m in re.finditer(r"(\d+)\s*лыск\w*\s*(?:«?под ключ»?\s*)?S\s*(\d+(?:[.,]\d+)?)", texts, re.I):
        rest = texts[m.end():m.end() + 80]
        d = re.search(r"(?:Ø|на\s*Ø?)\s*(\d+(?:[.,]\d+)?)", rest)
        ln = re.search(r"длин\w*\s*(\d+(?:[.,]\d+)?)", rest)
        dv = d.group(1).replace(",", ".") if d else None
        if dv and re.match(r"[03]\d\d[.]", dv):
            dv = dv[1:]           # text recognition reads the sign Ø as 0 or 3: «на 312,93»
        out.append({"n": int(m.group(1)), "s": float(m.group(2).replace(",", ".")),
                    "d": float(dv) if dv else None,
                    "l": float(ln.group(1).replace(",", ".")) if ln else None})
    return out


def flat_cuts(profile_pts: list, flat: dict) -> list[dict]:
    """Two flats «под ключ» S across a diameter: cut away |Y| > S/2 along the stretch of the
    profile where its outer radius is that diameter (the longest such stretch)."""
    r = flat["d"] / 2 if flat.get("d") else None
    if r is None:
        return []
    runs, cur = [], None
    for (z0, r0), (z1, r1) in zip(profile_pts, profile_pts[1:]):
        # (within 0.06: the profile may have snapped to a near number of the table, Ø12,92 for 12,93)
        if abs(r0 - r) < 0.06 and abs(r1 - r) < 0.06 and z1 > z0:
            cur = [cur[0], z1] if cur and abs(cur[1] - z0) < 1e-6 else [z0, z1]
            runs.append(cur)
    if not runs:
        return []
    z0, z1 = max(runs, key=lambda c: c[1] - c[0])
    if flat.get("l") and flat["l"] < z1 - z0:
        z1 = z0 + flat["l"]
    s = flat["s"] / 2
    big = r + 2
    ops = []
    for sign in (1, -1):
        y0 = s if sign > 0 else -big
        ops.append({"op": "cut", "plane": "XY", "offset": -big, "through_all": True,
                    "shapes": [{"type": "rect", "x": z0, "y": y0, "w": z1 - z0, "h": big - s}],
                    "comment": f"лыска S{flat['s']:g} на Ø{flat['d']:g}"})
    return ops


def _rdp(pts, tol):
    if len(pts) < 3:
        return list(pts)
    (ax, ay), (bx, by) = pts[0], pts[-1]
    L = math.hypot(bx - ax, by - ay) or 1e-12
    d = [abs((by - ay) * (x - ax) - (bx - ax) * (y - ay)) / L for x, y in pts[1:-1]]
    i = int(np.argmax(d))
    if d[i] <= tol:
        return [pts[0], pts[-1]]
    return _rdp(pts[:i + 2], tol)[:-1] + _rdp(pts[i + 1:], tol)


def _snap(v, cands, tol):
    c = min(cands, key=lambda c: abs(c - v), default=None)
    return c if c is not None and abs(c - v) <= tol else v


def _despike(r: np.ndarray, width: int) -> np.ndarray:
    """Removes bumps narrower than `width` samples (arrowheads, leader ends touching the outline):
    a running median."""
    if width < 3:
        return r
    k = width | 1
    pad = np.pad(r, k // 2, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, k)
    return np.median(win, axis=1)


def plan(p: dict, length: float, diameters: list[float], lengths: list[float], tol_r: float = 0.2,
         tol_z: float = 0.2) -> dict:
    """The revolve operation from a profile: outline above the axis as a closed polyline in
    (z, r) — outer edge left to right, inner edge back — and the axis itself."""
    z, ro, ri = p["z"], p["r_out"], p["r_in"]
    scale = length / max(z.max(), 1e-9)
    z = z * scale
    spike = int(0.25 * p["px_per_mm"])  # features under a quarter millimetre are drawing noise
    ro, ri = _despike(ro, spike), _despike(ri, spike)
    radii = sorted({d / 2 for d in diameters if d > 0})
    # positions along the axis: the ends and every running sum of the length chain read
    zc = {0.0, length}
    for v in lengths:
        if 0 < v < length:
            zc |= {v, length - v}
    acc = 0.0
    for v in lengths:
        acc += v
        if acc < length:
            zc.add(acc)
    zc = sorted(zc)
    outer = _rdp(list(zip(z.tolist(), ro.tolist())), 0.05)
    inner = _rdp(list(zip(z.tolist(), ri.tolist())), 0.05)
    snapped = lambda pts: [(round(_snap(a, zc, tol_z), 4), round(_snap(b, radii, tol_r), 4)) for a, b in pts]  # noqa: E731
    outer, inner = snapped(outer), snapped(inner)
    outer[0] = (0.0, outer[0][1])
    outer[-1] = (length, outer[-1][1])
    inner[0] = (0.0, inner[0][1])
    inner[-1] = (length, inner[-1][1])
    # orthogonal steps: a short slanted piece between two nearly equal z is a vertical shoulder
    def squared(pts):
        out = [pts[0]]
        for a in pts[1:]:
            b = out[-1]
            if abs(a[0] - b[0]) < 0.08 and a[1] != b[1]:
                a = (b[0], a[1])
            if a != b:
                out.append(a)
        return out
    outer, inner = squared(outer), squared(inner)
    solid = all(r <= 1e-6 for _, r in inner)
    if solid:
        poly = [(0.0, 0.0)] + outer + [(length, 0.0)]
    else:
        poly = outer + inner[::-1]
    clean = []
    for q in poly:
        if not clean or q != clean[-1]:
            clean.append(q)
    shapes = [{"type": "polyline", "closed": True, "points": [list(q) for q in clean]},
              {"type": "line", "x1": -2, "y1": 0, "x2": length + 2, "y2": 0, "style": 3}]
    # volume by Pappus (for the check against the reference)
    area, cz = 0.0, 0.0
    pts = clean
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        cr = x1 * y2 - x2 * y1
        area += cr
        cz += (y1 + y2) * cr
    area /= 2
    ybar = cz / (6 * area) if area else 0
    volume = abs(2 * math.pi * ybar * area)
    return {"operations": [{"op": "revolve", "plane": "XY", "angle": 360, "shapes": shapes,
                            "comment": "профиль разреза над осью, вращение вокруг оси"}],
            "profile": clean, "volume": volume,
            "max_diameter": 2 * max(r for _, r in clean), "bore": [] if solid else inner}
