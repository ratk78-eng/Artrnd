"""Parts cut from a rolled profile, read off a drawing picture (PNG/JPG, a scan of a print).

What a KOMPAS / Tekla / SOLIDWORKS sheet of such a part shows, and how it is read here:
- the title block names the stock, «Труба 50х50х3 ГОСТ 8639-82» (read with Windows' own OCR,
  rastertext as a fallback): the section is that profile, not traced;
- one view across the profile (the tube's square, often a section «А-А»);
- one or more long views of the faces, drawn with breaks: along them the picture is not to
  scale, so every position comes from the dimensions (rastertext.dimensions: a number and the
  two extension lines its dimension line runs between), the ends and each unbroken piece of a
  view by the view's own scale (its height is the profile's side);
- the ends cut to shape (a tongue 3 × 20, a mitre at 45°): what lies outside a view's outline
  at its ends is cut away through the part;
- holes and slots inside a long view, in the face towards the viewer (a double circle: the far
  wall has the smaller hole too).

The plan is for build_part: X along the part, the profile in the YZ plane."""
from __future__ import annotations

import contextlib
import math
import os
import re
import subprocess

import numpy as np
from PIL import Image

import drawing
import rastertext as rt

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- the stock named in the title block ----------------

_TUBE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[хxХX×]\s*(\d+(?:[.,]\d+)?)\s*[хxХX×]\s*(\d+(?:[.,]\d+)?)\s*(?:ГОСТ|гост|ГОСТ\s*Р)?\s*(\d{4,5})?")


def ocr_lines(path: str, box=None) -> list[str]:
    """Text lines of the picture (or a part of it) by Windows' built-in OCR (Windows.Media.Ocr;
    nothing to install, nothing leaves the computer). [] when it is not available."""
    return lines_of(ocr(path, box))


def lines_of(words: list[dict]) -> list[str]:
    out: dict[int, list[str]] = {}
    for w in words:
        out.setdefault(w["line"], []).append(w["text"])
    return [" ".join(v) for _, v in sorted(out.items())]


def ocr(path: str, box=None) -> list[dict]:
    """The words OCR finds: {text, x, y, w, h, line} in pixels of the picture."""
    src = path
    if box:
        tmp = os.path.join(os.environ.get("TEMP", "."), "kompasmcp_ocr.png")
        with Image.open(path) as im:
            im.crop(box).save(tmp)
        src = tmp
    for _attempt in range(3):   # a busy machine now and then fails to start PowerShell
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                os.path.join(HERE, "ocr.ps1"), "-Image", src],
                               capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            continue
        words = []
        for ln in (r.stdout or "").splitlines():
            p = ln.split("\t")
            if len(p) == 6 and not ln.startswith("#"):
                with contextlib.suppress(ValueError):
                    words.append({"text": p[0], "x": int(p[1]), "y": int(p[2]), "w": int(p[3]), "h": int(p[4]),
                                  "line": int(p[5])})
        if words:
            return words
    return []


def stamp_mass(words: list[dict], glyph_words: list[dict] | None = None) -> float | None:
    """The mass in the title block, kg: the number in the cell under the word «Масса» (OCR; a
    lone digit OCR skips is taken from rastertext's words, glyph_words)."""
    num = re.compile(r"^\d+(?:[.,]\d+)?$")
    for m in (w for w in words if re.fullmatch(r"[МM]асса", w["text"], re.I)):
        cx = m["x"] + m["w"] / 2
        below = lambda y: m["y"] + 0.5 * m["h"] < y < m["y"] + 5 * m["h"]  # noqa: E731
        cand = [w for w in words if num.match(w["text"]) and below(w["y"]) and abs(w["x"] + w["w"] / 2 - cx) < 1.2 * m["w"]]
        if cand:
            best = min(cand, key=lambda w: w["y"])
            with contextlib.suppress(ValueError):
                return float(best["text"].replace(",", "."))
        for g in sorted(glyph_words or [], key=lambda g: g["y0"]):
            n = rt.number_of(g)
            if n and not g["vertical"] and below(g["y0"]) and abs((g["x0"] + g["x1"]) / 2 - cx) < 1.2 * m["w"]:
                return n["value"]
    return None


def stock(lines: list[str]) -> dict | None:
    """The profile named in the texts: {kind: tube, a, b, t, gost, name}. Square / rectangular
    tubes (ГОСТ 8639, 8645, 30245, 13663 for the steel grade line is skipped)."""
    text = " ".join(lines)
    for m in _TUBE.finditer(text):
        a, b, t = (float(g.replace(",", ".")) for g in m.groups()[:3])
        if not (5 <= a <= 1000 and 5 <= b <= 1000 and 0.5 <= t <= 0.3 * min(a, b)):
            continue
        return {"kind": "tube", "a": a, "b": b, "t": t, "gost": m.group(4) or "",
                "name": f"Труба {m.group(1)}×{m.group(2)}×{m.group(3)}" + (f" ГОСТ {m.group(4)}" if m.group(4) else "")}
    return None


def tube_section(a: float, b: float, t: float, y0=0.0, z0=0.0) -> list[dict]:
    """A bent tube's section (ГОСТ 8639/8645: outer corner radius 2t, inner t) as sketch shapes in
    the YZ plane: outer and inner rounded rectangles."""
    def rrect(x, y, w, h, r):
        r = max(0.0, min(r, w / 2 - 1e-3, h / 2 - 1e-3))
        s = []
        pts = [(x + r, y), (x + w - r, y), (x + w, y + r), (x + w, y + h - r), (x + w - r, y + h), (x + r, y + h),
               (x, y + h - r), (x, y + r)]
        s.append({"type": "line", "x1": pts[0][0], "y1": pts[0][1], "x2": pts[1][0], "y2": pts[1][1]})
        s.append({"type": "arc", "x": x + w - r, "y": y + r, "r": r, "a1": 270, "a2": 360,
                  "x1": pts[1][0], "y1": pts[1][1], "x2": pts[2][0], "y2": pts[2][1]})
        s.append({"type": "line", "x1": pts[2][0], "y1": pts[2][1], "x2": pts[3][0], "y2": pts[3][1]})
        s.append({"type": "arc", "x": x + w - r, "y": y + h - r, "r": r, "a1": 0, "a2": 90,
                  "x1": pts[3][0], "y1": pts[3][1], "x2": pts[4][0], "y2": pts[4][1]})
        s.append({"type": "line", "x1": pts[4][0], "y1": pts[4][1], "x2": pts[5][0], "y2": pts[5][1]})
        s.append({"type": "arc", "x": x + r, "y": y + h - r, "r": r, "a1": 90, "a2": 180,
                  "x1": pts[5][0], "y1": pts[5][1], "x2": pts[6][0], "y2": pts[6][1]})
        s.append({"type": "line", "x1": pts[6][0], "y1": pts[6][1], "x2": pts[7][0], "y2": pts[7][1]})
        s.append({"type": "arc", "x": x + r, "y": y + r, "r": r, "a1": 180, "a2": 270,
                  "x1": pts[7][0], "y1": pts[7][1], "x2": pts[0][0], "y2": pts[0][1]})
        return s
    return rrect(y0, z0, a, b, 2 * t) + rrect(y0 + t, z0 + t, a - 2 * t, b - 2 * t, t)


# ---------------- the views ----------------

def _main_mask(path: str):
    """Full-size mask of the main lines (their colour on a CAD export, else the dark ink)."""
    img = np.asarray(Image.open(path).convert("RGB"))
    ink = img.astype(np.int16).mean(axis=2) < 175
    main = drawing._main_colour(img, ink)
    return img, (main if main is not None else ink)


def _segments(main: np.ndarray, box) -> list[tuple[int, int]]:
    """The unbroken pieces of a long view, (x0, x1) in sheet px: its top edge line has a gap at
    every break (the break's own wavy lines are thin)."""
    x0, y0, x1, y1 = box
    band = main[max(0, y0 - 2):y0 + 6, x0:x1].any(axis=0)
    band |= main[max(0, y1 - 6):y1 + 2, x0:x1].any(axis=0)
    segs, s = [], None
    gap = 0
    for i, on in enumerate(band):
        if on:
            if s is None:
                s = i
            gap = 0
        else:
            gap += 1
            if s is not None and gap > 6:
                segs.append((x0 + s, x0 + i - gap + 1))
                s = None
    if s is not None:
        segs.append((x0 + s, x1))
    segs = [g for g in segs if g[1] - g[0] > 5]
    if segs:
        # the part's ends are the view's ends (an end tongue sticks out past the edge lines)
        segs[0] = (x0, segs[0][1])
        segs[-1] = (segs[-1][0], x1)
    return segs


def _line_width(main: np.ndarray, box) -> int:
    """The main lines' width (px): the top edge of the view across, in the middle of it."""
    x0, y0, x1, y1 = box
    ws = []
    for x in np.linspace(x0 + 0.1 * (x1 - x0), x1 - 0.1 * (x1 - x0), 15).astype(int):
        col = main[y0:y0 + 30, x]
        n = 0
        while n < len(col) and col[n]:
            n += 1
        if 0 < n < 25:
            ws.append(n)
    return int(np.median(ws)) if ws else 3


def _features(main: np.ndarray, box, scale: float, lw: int = 3) -> list[dict]:
    """Closed contours inside a long view: circles (holes) and rectangles (slots), in sheet px,
    their sizes between the lines' middles. An axis crossing a circle breaks its colour: the
    lines are closed up by 2 px first."""
    x0, y0, x1, y1 = box
    m = drawing._box_filter_max(main[y0:y1, x0:x1], 2)
    lab, comps = rt._components(m)
    for c in comps:     # back to the drawn size: the closing grew each side by 2 px
        c["x0"] += 2
        c["y0"] += 2
        c["x1"] -= 2
        c["y1"] -= 2
    out = []
    H = y1 - y0
    for c in comps:
        bw, bh = c["x1"] - c["x0"], c["y1"] - c["y0"]
        # inside the outline: not touching the top and bottom edges, smaller than the side
        if c["y0"] <= 0.06 * H or c["y1"] >= 0.94 * H or max(bw, bh) > 0.9 * H or max(bw, bh) < 3 * scale \
                or min(bw, bh) < 1.5 * scale:
            continue
        reg = lab[c["y0"]:c["y1"], c["x0"]:c["x1"]] == c["id"]
        cx, cy = x0 + (c["x0"] + c["x1"]) / 2, y0 + (c["y0"] + c["y1"]) / 2
        fill = reg.sum() / max(1, bw * bh)
        # a drawn contour is solid line all round; a few tinted pixels at the edges of black text
        # (a number written inside the view) only join up after the closing
        drawn = (main[y0 + c["y0"]:y0 + c["y1"], x0 + c["x0"]:x0 + c["x1"]] & reg).sum()
        if drawn < 0.3 * 2.6 * (bw + bh) * lw:
            continue
        if abs(bw - bh) <= 0.15 * max(bw, bh) and fill < 0.6:
            # a circle; a second one inside it (a hole through the far wall)
            inner = [d for d in comps if d is not c and d["x0"] > c["x0"] and d["x1"] < c["x1"]
                     and d["y0"] > c["y0"] and d["y1"] < c["y1"] and (d["x1"] - d["x0"]) > 0.5 * bw]
            # the diameter at the line's middle: the outermost ring of the drawn pixels' distances
            # from the centre (a tinted fringe and a second, close circle do not shift it)
            ys, xs = np.nonzero(main[y0 + c["y0"]:y0 + c["y1"], x0 + c["x0"]:x0 + c["x1"]] & reg)
            d = (bw + bh) / 2 - lw
            if len(xs) > 20:
                r = np.hypot(xs + c["x0"] + x0 - cx + 0.5, ys + c["y0"] + y0 - cy + 0.5)
                hist = np.bincount(r.astype(int))
                top = int(np.nonzero(hist >= 0.4 * hist.max())[0][-1])
                q = [i for i in range(max(0, top - lw), min(len(hist), top + lw + 1)) if hist[i] >= 0.3 * hist.max()]
                ring = hist[q]
                # (−0.8 lw: on the KOMPAS exports the rings come out that much wider than drawn,
                # measured on Ø16 / Ø20 holes of known size)
                d = 2 * (float((np.array(q) * ring).sum() / ring.sum()) + 0.5) - 0.8 * lw
            out.append({"kind": "circle", "cx": cx, "cy": cy, "d": d,
                        "inner": ((inner[0]["x1"] - inner[0]["x0"] - lw) if inner else None)})
        elif fill < 0.7:
            # between the sides' middles: the strongest column / row in either half (a black
            # extension line drawn over a side leaves a tinted fringe outside it)
            raw = main[y0 + c["y0"]:y0 + c["y1"], x0 + c["x0"]:x0 + c["x1"]] & reg
            px, py = raw.sum(axis=0), raw.sum(axis=1)
            def mid(p):    # the line's middle: the centroid of its strong columns
                # (well above the other two sides' share: on a narrow slot they come close)
                q = np.where(p >= 0.6 * p.max(), p, 0).astype(float)
                return float((q * np.arange(len(q))).sum() / max(1.0, q.sum()))
            w, h = bw - lw, bh - lw
            if bw > 6:
                la, lb = mid(px[:bw // 2]), mid(px[bw // 2:]) + bw // 2
                w, cx = lb - la, x0 + c["x0"] + (la + lb) / 2
            if bh > 6:
                la, lb = mid(py[:bh // 2]), mid(py[bh // 2:]) + bh // 2
                h, cy = lb - la, y0 + c["y0"] + (la + lb) / 2
            out.append({"kind": "rect", "cx": cx, "cy": cy, "w": w, "h": h})
    # a circle inside another one is that one's inner circle, not a hole of its own
    circ = [f for f in out if f["kind"] == "circle"]
    keep = []
    for f in out:
        if f["kind"] == "circle" and any(g is not f and g["d"] > f["d"] and math.hypot(g["cx"] - f["cx"], g["cy"] - f["cy"]) < 0.2 * g["d"]
                                         for g in circ):
            continue
        keep.append(f)
    return keep


def _envelope(main: np.ndarray, box, xa: int, xb: int):
    """Top and bottom of the outline in each column xa…xb of a view (sheet px), None where empty."""
    x0, y0, x1, y1 = box
    # closed along x first: a thin black line drawn over a main line (an extension line over the
    # end) leaves a gap in it
    r = 3
    a0 = max(0, xa - 2 * r)
    m = main[y0 - 3:y1 + 3, a0:xb + 2 * r]
    n = m.shape[1]
    dil = np.zeros_like(m)
    for k in range(-r, r + 1):
        dil[:, max(0, k):n + min(0, k)] |= m[:, max(0, -k):n - max(0, k)]
    ero = dil.copy()
    for k in range(-r, r + 1):
        sh = np.zeros_like(dil)
        sh[:, max(0, k):n + min(0, k)] = dil[:, max(0, -k):n - max(0, k)]
        ero &= sh
    closed = ero | m
    out = []
    for x in range(xa, xb):
        col = closed[:, x - a0]
        ys = np.nonzero(col)[0]
        out.append((x, (y0 - 3 + ys.min(), y0 - 3 + ys.max()) if len(ys) else None))
    return out


def plan(path: str, views: list[dict] | None = None, lines: list[str] | None = None,
         length: float | None = None, ocr_words: list[dict] | None = None) -> dict | None:
    """The build plan for a profile part drawn on the picture, or None when the sheet names no
    profile or shows no long view. {operations, notes, questions, stock, length}.
    length: the part's length as the user confirmed it (else the dimension read)."""
    if lines is None:
        lines = ocr_lines(path)
    st = stock(lines)
    if not st:
        return None
    img, main = _main_mask(path)
    found = drawing._main_line_views(path) or []
    cores = [f[4] for f in found]
    a, b, t = st["a"], st["b"], st["t"]
    # the view across the profile: both through its middle row and its middle column the lines
    # cross four times (outer and inner wall on either side); a long view's middle row crosses
    # only its two ends
    def crossings(line):
        d = np.diff(np.concatenate(([0], drawing._box_filter_max(line[None, :], 1)[0].astype(np.int8), [0])))
        return int((d == 1).sum())
    def is_section(c):
        x0, y0, x1, y1 = c
        # not the very middle: the axes run there, drawn over the walls in their own colour
        rows = max(crossings(main[int(y0 + f * (y1 - y0)), x0 - 2:x1 + 2]) for f in (0.35, 0.42, 0.6, 0.67))
        cols = max(crossings(main[y0 - 2:y1 + 2, int(x0 + f * (x1 - x0))]) for f in (0.35, 0.42, 0.6, 0.67))
        asp = (x1 - x0) / max(1, y1 - y0)
        return rows >= 4 and cols >= 4 and 0.7 * min(a / b, b / a) <= asp <= 1.3 * max(a / b, b / a)
    longs = [c for c in cores if not is_section(c) and (c[2] - c[0]) >= 0.9 * (c[3] - c[1])]
    if not longs:
        return None
    longs.sort(key=lambda c: (c[1], c[0]))
    words = rt.read_words(img)
    dims = rt.dimensions(img, words)
    # every number read on the sheet (diameters written on a leader, sizes of a detail view…)
    allnums = [{"value": n["value"]} for n in (rt.number_of(w) for w in words) if n and n["value"] > 0] + dims
    notes = [f"Заготовка — {st['name']} (по штампу); сечение по ГОСТ: R наружный {2 * t:g}, внутренний {t:g}"]
    questions = []
    # what was found where on the sheet (px), for the review window: views, dimensions, features
    marks = [{"kind": "view", "box": [int(v) for v in c],
              "label": (f"вид {longs.index(c) + 1}" if c in longs else "сечение / другой вид")} for c in cores]
    for d in dims:
        w = d["word"]
        if d["vertical"]:
            box = [min(w["x0"], d["line"]), d["a"], max(w["x1"], d["line"]), d["b"]]
        else:
            box = [d["a"], min(w["y0"], d["line"]), d["b"], max(w["y1"], d["line"])]
        marks.append({"kind": "dim", "box": [int(v) for v in box], "label": f"{d['prefix']}{d['value']:g}",
                      "text": [int(w["x0"]), int(w["y0"]), int(w["x1"]), int(w["y1"])]})
    # the length: the longest dimension along a long view spanning (nearly) all of it
    L, Ldim = None, None
    for dm in sorted(dims, key=lambda d: -d["value"]):
        if dm["vertical"]:
            continue
        for c in longs:
            if dm["a"] <= c[0] + 0.03 * (c[2] - c[0]) and dm["b"] >= c[2] - 0.03 * (c[2] - c[0]) \
                    and c[1] - 0.6 * (c[2] - c[0]) < dm["line"] < c[3] + 0.6 * (c[2] - c[0]):
                L, Ldim = dm["value"], dm
                break
        if L:
            break
    if length:
        L = float(length)
    if not L:
        return None
    notes.append(f"Длина {L:g} мм" + (" — по размеру на виде" if not length else ""))
    if Ldim is not None:
        lt = [int(Ldim["word"][q]) for q in ("x0", "y0", "x1", "y1")]
        for mk in marks:
            if mk["kind"] == "dim" and mk["text"] == lt:
                mk["dim_id"] = "L"
    ops = [{"op": "extrude", "plane": "YZ", "offset": 0, "depth": L, "shapes": tube_section(a, b, t),
            "comment": f"{st['name']}, длина {L:g}"}]
    # each long view: a face of the tube; going down the sheet the faces turn round the axis
    faces = [("XZ", "y", 0.0, a), ("XY", "z", b, -b), ("XZ", "y", a, -a), ("XY", "z", 0.0, b)]
    for k, c in enumerate(longs[:4]):
        x0, y0, x1, y1 = c
        side = b if faces[k][1] == "y" else a      # the view's height is this side of the profile
        lw = _line_width(main, c)
        segs = _segments(main, c)
        if not segs:
            continue
        # px → mm along: by the ends, and through the dimensions over the breaks
        mine = [d for d in dims if not d["vertical"] and x0 - 5 <= d["a"] <= x1 + 5 and x0 - 5 <= d["b"] <= x1 + 5
                and (y0 - 0.25 * (x1 - x0) < d["line"] < y1 + 0.25 * (x1 - x0))]
        seg_of = lambda p: next((i for i, g in enumerate(segs) if g[0] - 6 <= p <= g[1] + 6), None)  # noqa: E731
        # the view's scale: dimensions within one unbroken piece, else its height (between the
        # edge lines' middles) against the profile's side
        # (all of them together: a long one fixes the scale better than a short one spoils it)
        within = [(d["b"] - d["a"], d["value"]) for d in mine
                  if d["value"] > 0 and d["b"] - d["a"] > 40 and seg_of(d["a"]) is not None and seg_of(d["a"]) == seg_of(d["b"])]
        s = sum(p for p, _ in within) / sum(v for _, v in within) if within else (y1 - y0 - lw) / side
        # the ends: where the overall dimension's extension lines stand, else the end lines' middles
        ea, eb = x0 + lw / 2, x1 - lw / 2
        over = [d for d in mine if abs(d["value"] - L) < 0.01 and abs(d["a"] - x0) <= 8 and abs(d["b"] - x1) <= 8]
        if over:
            ea, eb = over[0]["a"], over[0]["b"]
        off = {0: -(ea - segs[0][0]) / s, len(segs) - 1: L - (eb - segs[-1][0]) / s}
        if os.environ.get("PROFILE_DEBUG"):
            print("view", k + 1, "lw", lw, "s", round(s, 3), "segs", segs, "off", off, "within", within, flush=True)
        for _ in range(4):
            for d in mine:
                ia, ib = seg_of(d["a"]), seg_of(d["b"])
                if ia is None or ib is None:
                    continue
                if ia in off and ib not in off:
                    ma = off[ia] + (d["a"] - segs[ia][0]) / s
                    off[ib] = ma + d["value"] - (d["b"] - segs[ib][0]) / s
                elif ib in off and ia not in off:
                    mb = off[ib] + (d["b"] - segs[ib][0]) / s
                    off[ia] = mb - d["value"] - (d["a"] - segs[ia][0]) / s
        def mm(p):
            i = seg_of(p)
            if i is None or i not in off:
                return None
            return off[i] + (p - segs[i][0]) / s
        plane, axis, base, sign = faces[k]
        dep = a if axis == "y" else b              # the profile across the view (towards the viewer)
        near = -1.0 if sign > 0 else dep - t - 0.5  # the near wall: depth t + 1.5 from here
        # the ends: what lies outside the outline within the end piece is cut through
        for end in (0, 1):
            g = segs[0] if end == 0 else segs[-1]
            env = _envelope(main, c, g[0], g[1])
            span = [(x, e) for x, e in env if e]
            if not span:
                continue
            full = (y1 - y0) * 0.97
            # columns from the end inwards until the outline is full height
            cols = span if end == 0 else span[::-1]
            shape_pts = []
            for x, (ta, tb) in cols:
                xm = mm(x)
                if xm is None:
                    break
                shape_pts.append((xm, (y1 - lw / 2 - tb) / s, (y1 - lw / 2 - ta) / s))
                if tb - ta >= full and len(shape_pts) > 2 and abs(xm - (0 if end == 0 else L)) > 1.0:
                    break
            if len(shape_pts) < 3:
                continue
            depth_in = abs(shape_pts[-1][0] - (0 if end == 0 else L))
            # (the end line's own first pixels are left out: its rounded caps and an extension
            # line drawn over it make them look short)
            if all(p[2] - p[1] >= 0.97 * side for p in shape_pts[min(len(shape_pts) - 1, lw + 2):]) or depth_in < 0.5:
                continue                                    # a square end
            # the material reaches the part's end exactly (the end line's middle is the end)
            if os.environ.get("PROFILE_DEBUG"):
                print("   end", end, [tuple(round(float(v), 2) for v in p) for p in shape_pts[::max(1, len(shape_pts) // 12)]],
                      flush=True)
            # (an extension line drawn over the end line hides its outer pixels)
            e_x = 0.0 if end == 0 else L
            gap = shape_pts[0][0] - e_x if end == 0 else e_x - shape_pts[0][0]
            if 0 < gap < 1.0:
                shape_pts.insert(0, (e_x,) + tuple(shape_pts[0][1:]))
            # what is cut: the box over the end piece minus the material — round its outline:
            # out along the bottom edge from the inner end to the part's end, back along the top
            ext = -1.0 if end == 0 else L + 1.0
            inner = shape_pts[-1][0]
            bot = [(p[0], max(0.0, p[1]) if p[1] > 0.02 * side else 0.0) for p in shape_pts]
            top = [(p[0], min(side, p[2]) if p[2] < 0.98 * side else side) for p in shape_pts]
            region = [(ext, -1.0), (inner, -1.0), (inner, 0.0)] + bot[::-1] + top + [(inner, side), (inner, side + 1.0), (ext, side + 1.0)]
            region = _simplify(region)
            xs = [x for x, _ in cols[:len(shape_pts)]]
            marks.append({"kind": "end", "box": [int(min(xs)), int(y0), int(max(xs)), int(y1)], "op": len(ops),
                          "label": f"торец {'левый' if end == 0 else 'правый'}"})
            ops.append({"op": "cut", "plane": plane, "offset": -1, "through_all": True,
                        "shapes": [_face_poly(region, plane, sign, side)],
                        "comment": f"торец {'левый' if end == 0 else 'правый'} по виду {k + 1}"})
            notes.append(f"Вид {k + 1}: торец {'левый' if end == 0 else 'правый'} фигурный (вырез/скос) — срезано по контуру вида")
        # holes and slots in the face towards the viewer
        for f in _features(main, c, s, lw):
            xm = mm(f["cx"])
            if xm is None:
                questions.append(f"Вид {k + 1}: элемент у X≈? (за разрывом, размеров к нему не нашлось) — пропущен")
                continue
            zc = (y1 - lw / 2 - f["cy"]) / s
            hw, hh = (f["d"] / 2, f["d"] / 2) if f["kind"] == "circle" else (f["w"] / 2, f["h"] / 2)
            fbox = [int(f["cx"] - hw - lw), int(f["cy"] - hh - lw), int(f["cx"] + hw + lw), int(f["cy"] + hh + lw)]
            if os.environ.get("PROFILE_DEBUG"):
                print("   feature", f["kind"], {q: round(v / s, 2) for q, v in f.items() if q in ("d", "inner", "w", "h") and v},
                      "x", round(xm, 2), flush=True)
            if f["kind"] == "circle":
                dmm = _snap(f["d"] / s, allnums)
                marks.append({"kind": "hole", "box": fbox, "op": len(ops), "label": f"Ø{dmm:g}"})
                ops.append({"op": "cut", "plane": plane, "offset": near,
                            "depth": t + 1.5, "shapes": [_face_circle(xm, zc, dmm, plane, sign, side)],
                            "comment": f"отверстие Ø{dmm:g} в стенке, вид {k + 1}, X {xm:.1f}"})
                if f["inner"]:
                    di = _snap(f["inner"] / s, allnums)
                    ops.append({"op": "cut", "plane": plane, "offset": -1, "through_all": True,
                                "shapes": [_face_circle(xm, zc, di, plane, sign, side)],
                                "comment": f"отверстие Ø{di:g} в дальней стенке, вид {k + 1}, X {xm:.1f}"})
            else:
                wmm, hmm = _snap(f["w"] / s, allnums), _snap(f["h"] / s, allnums)
                marks.append({"kind": "slot", "box": fbox, "op": len(ops), "label": f"паз {wmm:g}×{hmm:g}"})
                ops.append({"op": "cut", "plane": plane, "offset": near,
                            "depth": t + 1.5,
                            "shapes": [_face_poly([(xm - wmm / 2, zc - hmm / 2), (xm + wmm / 2, zc - hmm / 2),
                                                   (xm + wmm / 2, zc + hmm / 2), (xm - wmm / 2, zc + hmm / 2)], plane, sign, side)],
                            "comment": f"паз {wmm:g}×{hmm:g} в стенке, вид {k + 1}, X {xm:.1f}"})
    return {"operations": ops, "notes": notes, "questions": questions, "stock": st, "length": L, "marks": marks,
            "longs": [[int(v) for v in c] for c in longs],
            "mass": _plausible_mass(stamp_mass(ocr_words, words) if ocr_words else None, a, b, t, L),
            "size": [int(img.shape[1]), int(img.shape[0])]}


def _plausible_mass(m: float | None, a: float, b: float, t: float, L: float) -> float | None:
    """The title block's mass against the blank's (steel, the tube's section × length): OCR now and
    then loses the comma («4,02» → 402, «0,64» → 64) — the decimal point goes where the number
    makes sense (the part weighs 50…105 % of its blank), else the mass is not trusted."""
    if not m:
        return None
    area = a * b - (a - 2 * t) * (b - 2 * t) - (4 - math.pi) * (2 * t) ** 2 + (4 - math.pi) * t ** 2
    blank = area * L * 7.85e-6
    for k in (1, 10, 100, 1000):
        if 0.5 * blank <= m / k <= 1.05 * blank:
            return round(m / k, 4)
    return None


def _snap(v: float, dims: list[dict]) -> float:
    # sizes are whole or half millimetres (or tenths); 4,02 is the mass in the title block
    vals = [d["value"] for d in dims if abs(d["value"] * 10 - round(d["value"] * 10)) < 1e-6]
    near = min(vals, key=lambda q: abs(q - v), default=None)
    return near if near is not None and abs(near - v) <= max(0.6, 0.06 * v) else round(v * 2) / 2


def _simplify(pts, closed=False, tol=0.15):
    """Drop points in line with their neighbours (the outline sampled every pixel)."""
    out = []
    for p in pts:
        if out and math.dist(out[-1], p) < 1e-6:
            continue
        while len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            L = math.hypot(p[0] - ax, p[1] - ay) or 1e-9
            dist = abs((p[0] - ax) * (by - ay) - (p[1] - ay) * (bx - ax)) / L
            # a point where the outline turns back on itself (the tip of a spike) stays
            ahead = (bx - ax) * (p[0] - bx) + (by - ay) * (p[1] - by) >= 0
            if dist < tol and ahead:
                out.pop()
            else:
                break
        out.append(p)
    return out


def _face_poly(pts, plane, sign, side):
    """A polygon given as (x along, h up the view) in the face's plane coordinates."""
    return {"type": "polyline", "closed": True, "points": [[x, (h if sign > 0 else side - h)] for x, h in pts]}


def _face_circle(x, h, d, plane, sign, side):
    return {"type": "circle", "x": x, "y": (h if sign > 0 else side - h), "d": d}


def reading(res: dict) -> dict:
    """The dimension table shown to the user (as steel.reading): the stock and the length, editable."""
    st = res["stock"]
    dims = [{"id": i, "value": round(float(v), 3), "view": "штамп" if i != "L" else "вид", "meaning": m,
             "axis": ax, "overall": ov, "confidence": 1.0}
            for i, v, m, ax, ov in (("L", res["length"], "длина трубы", "length", True),
                                     ("a", st["a"], "ширина профиля", "width", True),
                                     ("b", st["b"], "высота профиля", "height", True),
                                     ("t", st["t"], "толщина стенки", "other", False))]
    return {"views": [], "dimensions": dims, "features": [], "title": st["name"],
            "overall": {"length": "L", "width": "a", "height": "b"}}