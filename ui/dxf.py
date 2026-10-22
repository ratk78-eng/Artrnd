"""Vector drawings: read an ASCII DXF (what KOMPAS, AutoCAD and most CAD systems export), render it
to a clean picture at an exact scale and collect the dimension numbers written on it.

With a vector source nothing has to be guessed: the scale is known (drawing units are millimetres),
hidden lines carry their own linetype and the dimension values are text in the file — so the views
go through the same reconstruction as a scan, but without a language model and without OCR errors.
"""
from __future__ import annotations

import math
import os
import re

from PIL import Image, ImageDraw

import arrange

HIDDEN = re.compile(r"hidden|dash|штрих|скрыт", re.I)
CENTER = re.compile(r"center|dashdot|осев|штрихпункт", re.I)


def _pairs(path: str):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        try:
            yield int(lines[i].strip()), lines[i + 1].strip()
        except ValueError:
            continue


def read_dxf(path: str) -> dict:
    """Entities of the ENTITIES section (and of blocks inserted at 0,0 scale 1 — dimension blocks):
    lines, circles, arcs, polylines and texts, with the linetype resolved through the layer."""
    layers_lt: dict[str, str] = {}
    blocks: dict[str, list] = {}
    entities: list[dict] = []
    section = None
    cur = None
    block_name = None
    target = entities
    table = None

    def flush():
        nonlocal cur
        if cur is not None:
            if cur["type"] == "LAYER_DEF":
                layers_lt[cur.get("name", "")] = cur.get("lt", "")
            elif cur["type"] in ("LINE", "CIRCLE", "ARC", "LWPOLYLINE", "TEXT", "MTEXT", "INSERT", "DIMENSION"):
                target.append(cur)
        cur = None

    for code, val in _pairs(path):
        if code == 0:
            flush()
            if val == "SECTION":
                section = "?"
                continue
            if val == "ENDSEC":
                section = None
                continue
            if section == "TABLES" and val == "TABLE":
                continue
            if section == "TABLES" and val == "LAYER":
                cur = {"type": "LAYER_DEF"}
                continue
            if section == "BLOCKS" and val == "BLOCK":
                block_name = "?"
                target = []
                continue
            if section == "BLOCKS" and val == "ENDBLK":
                if block_name:
                    blocks[block_name] = target
                target = entities
                block_name = None
                continue
            cur = {"type": val, "lt": "", "layer": "", "pts": []}
            continue
        if section == "?" and code == 2:
            section = val
            continue
        if section == "BLOCKS" and block_name == "?" and cur is None and code == 2:
            block_name = val
            continue
        if cur is None:
            continue
        t = cur["type"]
        if t == "LAYER_DEF":
            if code == 2:
                cur["name"] = val
            elif code == 6:
                cur["lt"] = val
            continue
        if code == 8:
            cur["layer"] = val
        elif code == 6:
            cur["lt"] = val
        elif code == 2:
            cur["block"] = val
        elif code in (1, 3) and t in ("TEXT", "MTEXT", "DIMENSION"):
            cur["text"] = cur.get("text", "") + val
        elif code == 42 and t == "DIMENSION":
            cur["measure"] = float(val)
        elif t == "LWPOLYLINE" and code == 10:
            cur["pts"].append([float(val), None])
            cur.setdefault("bulge", []).append(0.0)
        elif t == "LWPOLYLINE" and code == 20 and cur["pts"]:
            cur["pts"][-1][1] = float(val)
        elif t == "LWPOLYLINE" and code == 42 and cur["pts"]:
            # an arc from this vertex to the next one: tan(sweep/4), positive counter-clockwise
            cur["bulge"][-1] = float(val)
        elif t == "LWPOLYLINE" and code == 70:
            cur["closed"] = int(val) & 1
        elif code in (10, 20, 11, 21, 40, 50, 51):
            cur[code] = float(val)
    flush()
    out = []
    for e in entities:
        if e["type"] in ("INSERT", "DIMENSION") and e.get("block") in blocks:
            for b in blocks[e["block"]]:
                b = dict(b)
                if e["type"] == "INSERT":  # only plain inserts at their own position are supported
                    dx, dy = e.get(10, 0.0), e.get(20, 0.0)
                    for k in (10, 11):
                        if k in b:
                            b[k] += dx
                    for k in (20, 21):
                        if k in b:
                            b[k] += dy
                    b["pts"] = [[p[0] + dx, (p[1] or 0.0) + dy] for p in b.get("pts", [])]
                b["from_dimension"] = e["type"] == "DIMENSION"
                out.append(b)
        if e["type"] != "INSERT":
            out.append(e)
    for e in out:
        lt = e.get("lt") or ""
        if not lt or lt.upper() == "BYLAYER":
            lt = layers_lt.get(e.get("layer", ""), "")
        e["kind"] = "hidden" if HIDDEN.search(lt) else "center" if CENTER.search(lt) else \
            "thin" if e.get("layer", "").upper() == "THIN" else "solid"  # THIN: drawings read from a PDF
    # KOMPAS's own DXF export carries no linetypes or weights: its main (thick) lines come out as
    # LWPOLYLINE and the thin ones (axes, hatching, leaders) as LINE
    kompas = any("KDIMSTYLE" in (e.get("text") or "") for e in out if e["type"] == "DIMENSION")
    if kompas and all(e["kind"] == "solid" for e in out) and any(e["type"] == "LWPOLYLINE" for e in out):
        for e in out:
            if e["type"] in ("LINE", "ARC", "CIRCLE") and not e.get("from_dimension"):
                e["kind"] = "thin"
    _mark_loose_lines(out)
    return {"entities": out}


def _mark_loose_lines(ents: list) -> None:
    """A straight line touching no other line is not an edge of the body: an axis or a centre mark
    (they stick out past the outline). Without linetypes in the file, these would pass for edges."""
    geo = [e for e in ents if e["kind"] == "solid" and not e.get("from_dimension")
           and e["type"] in ("LINE", "LWPOLYLINE", "ARC", "CIRCLE")]
    segs = [(i, s) for i, e in enumerate(geo) for s in _segments(e, 15.0)]

    def on(p, s, tol=0.2):  # drawings converted from other systems leave gaps of a tenth or so
        (ax, ay), (bx, by) = s
        L2 = (bx - ax) ** 2 + (by - ay) ** 2
        if L2 < 1e-12:
            return math.dist(p, s[0]) <= tol
        t = max(0.0, min(1.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2))
        return math.dist(p, (ax + t * (bx - ax), ay + t * (by - ay))) <= tol
    for i, e in enumerate(geo):
        mine = [s for j, s in segs if j == i]
        if len(mine) != 1 or e["type"] in ("CIRCLE", "ARC") or e.get("closed"):
            continue
        a, b = mine[0]
        if not any(j != i and (on(a, s) or on(b, s)) for j, s in segs):
            e["kind"] = "center"
    # a straight line through the centre of a circle or arc that runs on past it on both sides is
    # its axis (hole rows have axes drawn as heavy as the outline in some exports)
    centres = []
    for e in geo:
        for _, _, tag in _segments_tagged(e, 30.0):
            if tag and tag not in centres:
                centres.append(tag)
    for i, e in enumerate(geo):
        mine = [s for j, s in segs if j == i]
        if e["kind"] != "solid" or len(mine) != 1 or e["type"] in ("CIRCLE", "ARC") or e.get("closed") \
                or any(e.get("bulge") or []):
            continue
        (ax, ay), (bx, by) = mine[0]
        L = math.hypot(bx - ax, by - ay)
        if L < 1e-9:
            continue
        ux, uy = (bx - ax) / L, (by - ay) / L
        for cx, cy, r in centres:
            off = abs((cx - ax) * uy - (cy - ay) * ux)        # distance of the centre from the line
            t = (cx - ax) * ux + (cy - ay) * uy               # where along the line it projects
            if off < 0.05 and t > r + 0.3 and L - t > r + 0.3:
                e["kind"] = "center"
                break


def view_boxes(doc: dict, gap: float = 3.0, min_size: float = 8.0) -> list[tuple]:
    """Boxes (mm: x0, y0, x1, y1) of the views: groups of visible main lines that touch each other.
    The sheet frame with its title block is one big group and is dropped, as are small marks
    (section arrows, roughness signs)."""
    main = [e for e in doc["entities"] if e["kind"] == "solid" and not e.get("from_dimension")
            and e["type"] in ("LINE", "LWPOLYLINE", "ARC", "CIRCLE")]
    boxes = []  # per segment: a frame drawn as one polyline must not swallow everything inside it
    for e in main:
        # arcs finely enough that the box reaches their top (15° chords fall short by tenths, and
        # the parts beyond the box are then left out when the view is traced)
        for (ax, ay), (bx, by) in _segments(e, 2.0):
            boxes.append([min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)])
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    active: list[int] = []
    for i in order:
        b = boxes[i]
        active = [j for j in active if boxes[j][2] + gap >= b[0]]
        for j in active:
            c = boxes[j]
            if b[1] - gap <= c[3] and c[1] - gap <= b[3]:
                parent[find(i)] = find(j)
        active.append(i)
    groups: dict[int, list] = {}
    for i, b in enumerate(boxes):
        g = groups.setdefault(find(i), [math.inf, math.inf, -math.inf, -math.inf, 0])
        g[0], g[1], g[2], g[3] = min(g[0], b[0]), min(g[1], b[1]), max(g[2], b[2]), max(g[3], b[3])
        g[4] += 1
    gs = [tuple(g[:4]) for g in groups.values()
          if max(g[2] - g[0], g[3] - g[1]) >= min_size and min(g[2] - g[0], g[3] - g[1]) >= 1.0]
    if not gs:
        return []
    inside = lambda a, b: b[0] <= a[0] and a[2] <= b[2] and b[1] <= a[1] and a[3] <= b[3]  # noqa: E731
    big = max(gs, key=lambda g: (g[2] - g[0]) * (g[3] - g[1]))
    rest = [g for g in gs if g is not big]
    # a frame contains every view; otherwise the biggest group is a view itself
    if rest and all(inside(g, big) for g in rest):
        gs = rest
    # what lies inside a view's box (holes, inner contours) belongs to that view
    gs = [g for g in gs if not any(h is not g and inside(g, h) for h in gs)]
    return sorted(gs, key=lambda b: (-b[3], b[0]))


def hatch_segments(doc: dict, box: tuple) -> list:
    """The hatching inside a view: thin slanted lines at least a millimetre long, all at the angle
    most of them share (or square to it, cross-hatching). Arrowheads, leaders and the slanted bits
    of dimension marks are short or at other angles and are left out: a point of theirs in an empty
    area would make it look like metal."""
    x0, y0, x1, y1 = box
    segs = []
    for e in doc["entities"]:
        if e["kind"] != "thin" or e.get("from_dimension") or e["type"] not in ("LINE", "LWPOLYLINE"):
            continue
        for (ax, ay), (bx, by) in _segments(e, 15.0):
            if not (x0 - 0.1 <= min(ax, bx) and max(ax, bx) <= x1 + 0.1 and y0 - 0.1 <= min(ay, by) and max(ay, by) <= y1 + 0.1):
                continue
            a = math.degrees(math.atan2(by - ay, bx - ax)) % 180
            if (15 <= a <= 75 or 105 <= a <= 165) and math.hypot(bx - ax, by - ay) >= 1.0:
                segs.append((a, ((ax, ay), (bx, by))))
    if not segs:
        return []
    count: dict[int, int] = {}
    for a, _ in segs:
        count[round(a)] = count.get(round(a), 0) + 1
    main = max(count, key=lambda k: sum(v for kk, v in count.items() if abs(kk - k) <= 2))
    ok = lambda a: min(abs(a - main), 180 - abs(a - main)) <= 3 or abs(abs(a - main) - 90) <= 3  # noqa: E731
    return [s for a, s in segs if ok(a)]


def hatched(doc: dict, box: tuple, min_lines: int = 8) -> bool:
    """Is the view a section: thin parallel slanted lines (hatching) inside its box, at even steps?
    Leader lines of dimensions are slanted too, but few and at random distances."""
    segs = hatch_segments(doc, box)
    if len(segs) < min_lines:
        return False
    (ax, ay), (bx, by) = segs[0]
    L = math.hypot(bx - ax, by - ay) or 1.0
    nx, ny = -(by - ay) / L, (bx - ax) / L        # across the hatching
    offs = sorted({round(p[0] * nx + p[1] * ny, 2) for s in segs for p in s[:1]
                   if abs(((s[1][0] - s[0][0]) * nx + (s[1][1] - s[0][1]) * ny)) < 0.05 * L + 0.05})
    steps = [b - a for a, b in zip(offs, offs[1:]) if b - a > 0.2]
    if len(steps) < min_lines - 1:
        return False
    steps.sort()
    med = steps[len(steps) // 2]
    even = sum(1 for s in steps if abs(s - med) <= 0.3 * med or abs(s / med - round(s / med)) <= 0.15)
    return 0.5 <= med <= 8 and even >= 0.6 * len(steps)


LABEL_RE = re.compile(r"^\s*([A-Za-zА-ЯЁа-яё])(?:\s*[-–—]\s*\1)?\s*(?:\(\s*\d+(?:[.,]\d+)?\s*:\s*\d+(?:[.,]\d+)?\s*\))?\s*$")


def view_label(doc: dict, box: tuple) -> str | None:
    """The label over a view — «Б», «А-А», «В (1 : 1)» — or None for a view in its place by the
    projection rules (the main view and the views lined up with it carry no label)."""
    best = None
    for e in doc["entities"]:
        if e["type"] not in ("TEXT", "MTEXT") or not e.get("text"):
            continue
        t = clean_text(e["text"])
        if not LABEL_RE.match(t):
            continue
        x, y = e.get(10, 0.0), e.get(20, 0.0)
        if box[0] - 15 <= x <= box[2] + 5 and box[3] - 1 <= y <= box[3] + 25:
            if best is None or y - box[3] < best[0]:
                best = (y - box[3], t.strip())
    return best[1] if best else None


def cut_front(doc: dict, main: tuple, sec: tuple, axis: str) -> str:
    """Which edge of a section is the main view's front face, from the arrows of its cutting line:
    looking the way the arrows point, the near face shows on the far side. A horizontal cut whose
    arrows point down (as for a view from above) has the front at the section's bottom edge, one
    pointing up at its top; a vertical cut pointing right has it at the right edge, pointing left
    at the left. Without arrows found: bottom / right."""
    default = "bottom" if axis == "y" else "right"
    labels = []
    for e in doc["entities"]:
        if e["type"] in ("TEXT", "MTEXT") and e.get("text"):
            m = re.match(r"^\s*([A-Za-zА-ЯЁа-яё])\s*[-–—]\s*\1\b", clean_text(e["text"]))
            x, y = e.get(10, 0.0), e.get(20, 0.0)
            if m and sec[0] - 15 <= x <= sec[2] + 15 and sec[3] - 2 <= y <= sec[3] + 45:
                labels.append((y - sec[3], m.group(1)))
    if not labels:
        return default
    letter = min(labels)[1]
    marks = [(e.get(10, 0.0), e.get(20, 0.0)) for e in doc["entities"] if e["type"] in ("TEXT", "MTEXT")
             and clean_text(e.get("text") or "") == letter]
    votes = 0
    for e in doc["entities"]:
        if e["kind"] not in ("thin", "solid") or e["type"] != "LWPOLYLINE" or not e.get("closed"):
            continue
        pts = [tuple(p) for p in e["pts"]]
        if not 3 <= len(pts) <= 16:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if max(max(xs) - min(xs), max(ys) - min(ys)) > 6:
            continue  # an arrowhead is a few millimetres long
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        if not any(math.dist((cx, cy), m) < 15 for m in marks):
            continue
        if main[0] < cx < main[2] and main[1] < cy < main[3]:
            continue
        tip = max(pts, key=lambda p: math.dist(p, (cx, cy)))
        dx, dy = tip[0] - cx, tip[1] - cy
        if axis == "y" and abs(dy) > 2 * abs(dx):
            votes += 1 if dy > 0 else -1
        elif axis == "x" and abs(dx) > 2 * abs(dy):
            votes += 1 if dx > 0 else -1
    if votes == 0:
        return machined_side(doc, sec, axis) or default
    if axis == "y":
        return "top" if votes > 0 else "bottom"
    return "right" if votes > 0 else "left"


def machined_side(doc: dict, sec: tuple, axis: str, ppm: float = 10.0) -> str | None:
    """Without arrows to go by: the section's edge the pockets open to — the side where the
    metal's edge goes up and down the most (the back face is usually flat)."""
    import numpy as np
    sides = ("bottom", "top") if axis == "y" else ("right", "left")
    spreads = []
    for s in sides:
        f = section_depths(doc, sec, s, ppm)
        if f is None:
            return None
        lo, hi = (sec[0], sec[2]) if axis == "y" else (sec[1], sec[3])
        vals = [f(lo + (hi - lo) * k / 200) for k in range(1, 200)]
        vals = [v for v in vals if v is not None]
        spreads.append(float(np.std(vals)) if vals else 0.0)
    if abs(spreads[0] - spreads[1]) < 0.05:
        return None
    return sides[0] if spreads[0] > spreads[1] else sides[1]


def cut_position(doc: dict, main: tuple, sec: tuple, axis: str) -> float:
    """Where a section's cutting plane crosses the main view (sheet mm): x for a section shown to the
    right (vertical cut), y for one below (horizontal cut). Found from the section's label («А-А»
    over it), the same letter at the main view and the short thick strokes of the cutting line next
    to that letter; without them the middle of the main view (sections usually go through it)."""
    mid = (main[0] + main[2]) / 2 if axis == "x" else (main[1] + main[3]) / 2
    texts = [e for e in doc["entities"] if e["type"] in ("TEXT", "MTEXT") and e.get("text")]
    labels = []
    for e in texts:
        m = re.match(r"^\s*([A-Za-zА-ЯЁа-яё])\s*[-–—]\s*\1\b", clean_text(e["text"]))
        x, y = e.get(10, 0.0), e.get(20, 0.0)
        if m and sec[0] - 15 <= x <= sec[2] + 15 and sec[3] - 2 <= y <= sec[3] + 45:
            labels.append((y - sec[3], m.group(1)))
    if not labels:
        return mid
    letter = min(labels)[1]  # the label right over the section
    marks = [(e.get(10, 0.0), e.get(20, 0.0)) for e in texts if clean_text(e["text"]) == letter
             and not (sec[0] - 1 <= e.get(10, 0.0) <= sec[2] + 1 and sec[1] - 1 <= e.get(20, 0.0) <= sec[3] + 30)]
    strokes = []
    for e in doc["entities"]:
        # the cutting line's strokes are heavy by the standard, but some systems print them thinner
        if e["kind"] not in ("solid", "thin") or e.get("from_dimension") or e["type"] not in ("LINE", "LWPOLYLINE"):
            continue
        for (ax, ay), (bx, by) in _segments(e, 15.0):
            L = math.hypot(bx - ax, by - ay)
            if not 2.0 <= L <= 15.0:
                continue
            vertical = abs(ax - bx) < 0.01
            horizontal = abs(ay - by) < 0.01
            if (axis == "x" and not vertical) or (axis == "y" and not horizontal):
                continue
            c = ((ax + bx) / 2, (ay + by) / 2)
            inside = main[0] + 0.5 < c[0] < main[2] - 0.5 and main[1] + 0.5 < c[1] < main[3] - 0.5
            if not inside and any(math.dist(c, p) < 15 for p in marks):
                strokes.append(ax if axis == "x" else ay)
    if not strokes:
        return mid
    strokes.sort()
    return strokes[len(strokes) // 2]


def section_depths(doc: dict, box: tuple, front: str, ppm: float = 20.0):
    """A section's metal (the hatched areas inside its main lines) as a function: coordinate along
    the cut (mm on the sheet: y when the front face is the view's right edge, x when it is the
    bottom edge) → depth from the front face to the first metal (mm, None outside the view)."""
    import numpy as np
    from PIL import ImageDraw as _D
    ctx = _draw_main_lines(doc, box, ppm)
    if ctx is None:
        return None
    img, W, H, x0, y1 = ctx["img"], ctx["W"], ctx["H"], ctx["x0"], ctx["y1"]
    bx0, by0, bx1, by1 = box
    for (ax, ay), (bx, by) in hatch_segments(doc, box):
        for t in (0.25, 0.5, 0.75):  # hatch lines: their points lie in the metal
            mx, my = ax + t * (bx - ax), ay + t * (by - ay)
            if not (bx0 < mx < bx1 and by0 < my < by1):
                continue
            c, r = int((mx - x0) * ppm), int((y1 - my) * ppm)
            if 0 <= c < W and 0 <= r < H and img.getpixel((c, r)) == 255:
                keep = img.copy()
                _D.floodfill(img, (c, r), 60)
                if img.getpixel((0, 0)) == 60:  # a hatch line past the outline: not metal
                    img = keep
    metal = np.array(img) == 60
    if not metal.any():
        return None
    half = 1.5 / ppm  # the metal stops at the inner side of the 3 px floor line, its middle is further

    def depth(s: float):
        if front in ("right", "left"):
            r = int(round((y1 - s) * ppm))
            if not (0 <= r < H) or not (by0 - 0.05 <= s <= by1 + 0.05):
                return None
            cols = np.nonzero(metal[r, :])[0]
            if not len(cols):
                return bx1 - bx0  # no metal on this line: a through opening
            if front == "right":
                return max(0.0, bx1 - (x0 + (cols.max() + 1) / ppm) - half)
            return max(0.0, (x0 + cols.min() / ppm) - bx0 - half)
        c = int(round((s - x0) * ppm))
        if not (0 <= c < W) or not (bx0 - 0.05 <= s <= bx1 + 0.05):
            return None
        rows = np.nonzero(metal[:, c])[0]
        if not len(rows):
            return by1 - by0
        if front == "bottom":
            return max(0.0, (y1 - (rows.max() + 1) / ppm) - by0 - half)
        return max(0.0, by1 - (y1 - rows.min() / ppm) - half)
    return depth


def _draw_main_lines(doc: dict, box: tuple, ppm: float):
    """The view's visible main lines drawn 3 px wide at ppm, with the drawing's own points (and
    the arc each belongs to) for snapping traced corners back onto the geometry."""
    from PIL import ImageDraw as _D
    m = 1.0
    x0, y0, x1, y1 = box[0] - m, box[1] - m, box[2] + m, box[3] + m
    W, H = int((x1 - x0) * ppm) + 3, int((y1 - y0) * ppm) + 3
    if W * H > 40_000_000:
        return None
    img = Image.new("L", (W, H), 255)
    d = _D.Draw(img)
    to = lambda p: ((p[0] - x0) * ppm, (y1 - p[1]) * ppm)  # noqa: E731
    ref_pts, ref_tags = [], []
    for e in doc["entities"]:
        if e["kind"] != "solid" or e.get("from_dimension") or e["type"] not in ("LINE", "LWPOLYLINE", "ARC", "CIRCLE"):
            continue
        for a, b, tag in _segments_tagged(e, 2.0):
            if all(box[0] - 0.1 <= p[0] <= box[2] + 0.1 and box[1] - 0.1 <= p[1] <= box[3] + 0.1 for p in (a, b)):
                d.line([to(a), to(b)], fill=0, width=3)
                ref_pts += [a, b]
                ref_tags += [tag, tag]
    return {"img": img, "x0": x0, "y1": y1, "W": W, "H": H, "ppm": ppm, "ref": ref_pts, "tags": ref_tags}


def _moore(inside, W: int, H: int, start) -> list | None:
    """Moore-neighbour tracing of the boundary of the region containing `start` (its top-left pixel)."""
    nb = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
    pts, cur, back = [start], start, 4  # we came from the left (west)
    for _ in range(4 * (W + H) * 8):
        for k in range(8):
            dk = (back + 1 + k) % 8
            nx, ny = cur[0] + nb[dk][0], cur[1] + nb[dk][1]
            if inside(nx, ny):
                back = (dk + 4) % 8
                cur = (nx, ny)
                break
        else:
            return pts if len(pts) == 1 else None
        if cur == start:
            return pts
        pts.append(cur)
    return None


def _polish(px_pts: list, ctx: dict, box: tuple | None, shift_out: bool) -> dict | None:
    """Traced pixels → simplified polygon in mm, corners snapped onto the drawing's own points,
    arc runs ending on the arcs' real end points. {"points": [...], "arcs": [[arc tags] per point]}"""
    import numpy as np
    ppm, x0, y1 = ctx["ppm"], ctx["x0"], ctx["y1"]
    mm = [(x0 + (x + 0.5) / ppm, y1 - (y + 0.5) / ppm) for x, y in px_pts]
    if shift_out and box:
        # the outer boundary runs along the outside of the 3 px stroke: move it to the line centre
        half = 1.0 / ppm
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        mm = [(x + (half if x < cx else -half) * (abs(x - cx) > 1e-9),
               y + (half if y < cy else -half) * (abs(y - cy) > 1e-9)) for x, y in mm]
    if len(mm) < 3:
        return None
    poly = _rdp_closed(mm, 0.06)
    ref = np.array(ctx["ref"]) if ctx["ref"] else np.zeros((0, 2))
    snapped, tags = [], []
    for x, y in poly:
        tag = set()
        if len(ref):
            dd = np.hypot(ref[:, 0] - x, ref[:, 1] - y)
            k = int(dd.argmin())
            if dd[k] < 0.3:
                x, y = float(ref[k, 0]), float(ref[k, 1])
                # every arc passing through this very point (a tangent point belongs to line and arc)
                tag = {ctx["tags"][j] for j in np.nonzero(dd <= dd[k] + 1e-6)[0] if ctx["tags"][j] is not None}
        if not tag and box:  # points of arcs stay exactly on their circle
            x = next((r for r in (box[0], box[2]) if abs(x - r) < 0.08), x)
            y = next((r for r in (box[1], box[3]) if abs(y - r) < 0.08), y)
        if not snapped or math.dist(snapped[-1], (x, y)) > 1e-6:
            snapped.append((round(x, 4), round(y, 4)))
            tags.append([list(t) for t in tag])
    if len(snapped) > 1 and math.dist(snapped[0], snapped[-1]) <= 1e-6:
        snapped.pop()
        tags.pop()
    # simplification may cut a little off an arc's ends: a vertex that ends a run on an arc goes to
    # that arc's real end point, so the arc and the next line meet exactly where the drawing has it
    n = len(snapped)
    if n < 3:
        return None
    on = lambda i, arc: arc in tags[i % n]  # noqa: E731
    for i in range(n):
        for arc in map(list, list(tags[i])):
            if on(i - 1, arc) == on(i + 1, arc):
                continue  # inside a run, or a lone point
            ends = ARC_ENDS.get(tuple(arc), ())
            near = min(ends, key=lambda p: math.dist(p, snapped[i]), default=None)
            if near is not None and math.dist(near, snapped[i]) < 0.6:
                snapped[i] = (round(near[0], 4), round(near[1], 4))
    return {"points": snapped, "arcs": tags}


def outer_loop(doc: dict, box: tuple, ppm: float = 20.0) -> dict | None:
    """The outline of a view, accurate to ~0.05 mm, with its arcs: the main lines of the view are
    drawn at 20 px/mm, the outside is flooded from the border, and the boundary of what is left is
    traced and simplified. Robust to lines that only nearly meet or cross each other."""
    from PIL import ImageDraw as _D
    ctx = _draw_main_lines(doc, box, ppm)
    if ctx is None:
        return None
    img, W, H = ctx["img"], ctx["W"], ctx["H"]
    _D.floodfill(img, (0, 0), 128)
    px = img.load()
    inside = lambda x, y: 0 <= x < W and 0 <= y < H and px[x, y] != 128  # noqa: E731
    start = next(((x, y) for y in range(H) for x in range(W) if inside(x, y)), None)
    trace = _moore(inside, W, H, start) if start else None
    out = _polish(trace, ctx, box, True) if trace else None
    if not out:
        return None
    xs, ys = [p[0] for p in out["points"]], [p[1] for p in out["points"]]
    if abs(min(xs) - box[0]) > 0.3 or abs(max(xs) - box[2]) > 0.3 \
            or abs(min(ys) - box[1]) > 0.3 or abs(max(ys) - box[3]) > 0.3:
        return None
    # the exact outline: the drawing's own lines and arcs round the view, when they close it
    oc = arrange.outer(arrange.build(doc, box))
    if oc:
        bx, by = [p[0] for p in oc["poly"]], [p[1] for p in oc["poly"]]
        per = sum(math.dist(out["points"][i], out["points"][i - 1]) for i in range(len(out["points"])))
        if max(abs(min(bx) - min(xs)), abs(max(bx) - max(xs)), abs(min(by) - min(ys)), abs(max(by) - max(ys))) <= 0.3 \
                and abs(arrange.area_of(out["points"]) - oc["area"]) <= 0.15 * per + 0.5:
            return arrange.outline(oc)
    return out


def faces(doc: dict, box: tuple, ppm: float = 20.0, min_area: float = 1.0, probe_x: tuple = (),
          probe_y: tuple = ()) -> list[dict]:
    """The closed areas the main lines divide a view into, each with its outer boundary (as
    outer_loop gives it), area, box and whether it lies against the view's outline (the body's
    own face) — inside a view these are pockets, steps, holes and bosses seen end-on.
    probe_x / probe_y: lines (x = const / y = const, mm) along which each face reports where it
    lies on them ("on_x": {x: [y, …]}, "on_y": {y: [x, …]}, every 0.25 mm) — a section's cut."""
    import numpy as np
    from PIL import ImageDraw as _D
    ctx = _draw_main_lines(doc, box, ppm)
    if ctx is None:
        return []
    img, W, H = ctx["img"], ctx["W"], ctx["H"]
    _D.floodfill(img, (0, 0), 128)
    arr = np.array(img)
    outside = arr == 128
    # what touches the outside, one stroke away: the body's face behind its own outline
    near_out = outside.copy()
    for _ in range(4):
        n2 = near_out.copy()
        n2[1:, :] |= near_out[:-1, :]
        n2[:-1, :] |= near_out[1:, :]
        n2[:, 1:] |= near_out[:, :-1]
        n2[:, :-1] |= near_out[:, 1:]
        near_out = n2
    out, label = [], 1
    cycles = None
    work = img.copy()
    while True:
        free = np.argwhere(np.array(work) == 255)
        if not len(free) or label > 250:
            break
        y, x = int(free[0][0]), int(free[0][1])          # top-left pixel of a new area (scan order)
        if label == 128:                                  # 128 marks the outside
            label += 1
        _D.floodfill(work, (x, y), label)
        reg = np.array(work) == label
        label += 1
        area = reg.sum() / ppm / ppm
        if area < min_area:
            continue
        rows, cols = np.nonzero(reg)
        inside = lambda xx, yy: 0 <= xx < W and 0 <= yy < H and reg[yy, xx]  # noqa: E731
        trace = _moore(inside, W, H, (x, y))
        poly = _polish(trace, ctx, None, False) if trace else None
        if not poly:
            continue
        x0, y1 = ctx["x0"], ctx["y1"]
        # the exact boundary: the drawing's own lines and arcs round the area, when they close it
        if cycles is None:
            cycles = arrange.build(doc, box)
        pt = _deep_pixel(reg, rows, cols)
        if pt is not None:
            c = arrange.match(cycles, (x0 + (pt[1] + 0.5) / ppm, y1 - (pt[0] + 0.5) / ppm), poly["points"])
            if c:
                poly = arrange.outline(c)
        step = max(1, int(ppm / 4))
        on_x, on_y = {}, {}
        for px in probe_x:
            c = int(round((px - x0) * ppm))
            if 0 <= c < W:
                on_x[px] = [y1 - r / ppm for r in np.nonzero(reg[:, c])[0][::step]]
        for py in probe_y:
            r = int(round((y1 - py) * ppm))
            if 0 <= r < H:
                on_y[py] = [x0 + c / ppm for c in np.nonzero(reg[r, :])[0][::step]]
        out.append({"outline": poly, "area": float(area),
                    "box": (x0 + cols.min() / ppm, y1 - rows.max() / ppm, x0 + cols.max() / ppm, y1 - rows.min() / ppm),
                    "body": bool((reg & near_out).any()), "on_x": on_x, "on_y": on_y,
                    "label": label - 1, "px": (int(rows.min()), int(rows.max()), int(cols.min()), int(cols.max()))})
    # which areas border on which (across the line between them): {index in out: contact pixels}
    lab = np.array(work)
    index = {f["label"]: i for i, f in enumerate(out)}
    for f in out:
        r0, r1, c0, c1 = f["px"]
        r0, c0 = max(0, r0 - 6), max(0, c0 - 6)
        win = lab[r0:r1 + 7, c0:c1 + 7]
        m = win == f["label"]
        g = m.copy()
        for _ in range(6):  # the line between two areas is 3 px wide
            h = g.copy()
            h[1:, :] |= g[:-1, :]
            h[:-1, :] |= g[1:, :]
            h[:, 1:] |= g[:, :-1]
            h[:, :-1] |= g[:, 1:]
            g = h
        vals, counts = np.unique(win[g & ~m], return_counts=True)
        f["touches"] = {index[int(v)]: int(c) for v, c in zip(vals, counts) if int(v) in index}
        f.pop("px")
    return out


def _deep_pixel(reg, rows, cols):
    """(row, col) of a pixel of the area a few pixels away from its edge (any pixel of a thin one)."""
    import numpy as np
    r0, r1, c0, c1 = rows.min(), rows.max(), cols.min(), cols.max()
    m = reg[r0:r1 + 1, c0:c1 + 1].copy()
    last = m
    for _ in range(4):
        e = m.copy()
        e[1:, :] &= m[:-1, :]
        e[:-1, :] &= m[1:, :]
        e[:, 1:] &= m[:, :-1]
        e[:, :-1] &= m[:, 1:]
        e[0, :] = e[-1, :] = False
        e[:, 0] = e[:, -1] = False
        if not e.any():
            break
        last = m = e
    nz = np.argwhere(last)
    if not len(nz):
        return None
    r, c = nz[len(nz) // 2]
    return int(r + r0), int(c + c0)


def outline_shapes(outline: dict, fn) -> list[dict]:
    """Sketch primitives for an outline from outer_loop()/faces(): runs of points on one circle
    become a real arc, the rest lines. fn maps drawing mm to sketch coordinates (shift + scale,
    possibly mirrored)."""
    pts, arcs = outline["points"], outline["arcs"]
    n = len(pts)
    o = fn((0.0, 0.0))
    ux = fn((1.0, 0.0))
    scale = math.hypot(ux[0] - o[0], ux[1] - o[1])
    if outline.get("segs"):
        return _segs_shapes(outline["segs"], fn, scale)
    sets = [{tuple(t) for t in a} for a in arcs]
    common = lambda a, b: sets[a] & sets[b]  # noqa: E731
    # start at a vertex that is not inside an arc run, so runs are not split at the list's end
    start = next((k for k in range(n) if not common((k - 1) % n, k)), 0)
    order = [(start + k) % n for k in range(n)] + [start]
    shapes = []
    k = 0
    while k < n:
        a, b = order[k], order[k + 1]
        c = common(a, b)
        if c:
            arc = next(iter(c))
            j = k + 1
            while j < n and arc in sets[order[j + 1]] and arc in sets[order[j]]:
                j += 1
            p, q = pts[order[k]], pts[order[j]]
            if math.dist(p, q) < 0.02 and j < n:
                # a sliver of an arc (two snapped points on one spot): KOMPAS fails on a zero-length
                # arc, and a few hundredths are a line as well
                if math.dist(p, q) > 1e-9:
                    shapes.append({"type": "line", "_from": fn(p), "_to": fn(q)})
                k = j
                continue
            mid = pts[order[(k + j) // 2]] if j - k >= 2 else None
            cx, cy, r = arc
            pc, qc, cc = fn(p), fn(q), fn((cx, cy))
            a1 = math.degrees(math.atan2(pc[1] - cc[1], pc[0] - cc[0]))
            a2 = math.degrees(math.atan2(qc[1] - cc[1], qc[0] - cc[0]))
            if mid is not None:
                mc = fn(mid)
                ccw = (pc[0] - cc[0]) * (mc[1] - cc[1]) - (pc[1] - cc[1]) * (mc[0] - cc[0]) > 0
            else:
                ccw = ((a2 - a1) % 360) <= 180
            R = r * scale
            # the arc's own end points (the drawing's scale may differ a hair between X and Y, so
            # the mapped vertices are not exactly on the mapped circle): lines will meet these.
            # Full precision on purpose: KOMPAS closes a contour only when the ends coincide to
            # ~1e-7, and rounding the centre, radius or angles leaves gaps bigger than that.
            first, last = [(cc[0] + R * math.cos(math.radians(ang)), cc[1] + R * math.sin(math.radians(ang)))
                           for ang in (a1, a2)]          # in the chain's order: from p to q
            s0, s1 = (first, last) if ccw else (last, first)  # counter-clockwise start and end
            if not ccw:
                a1, a2 = a2, a1
            shapes.append({"type": "arc", "x": cc[0], "y": cc[1], "r": R, "a1": a1, "a2": a2,
                           "x1": s0[0], "y1": s0[1], "x2": s1[0], "y2": s1[1], "_from": first, "_to": last})
            k = j
        else:
            shapes.append({"type": "line", "_from": fn(pts[a]), "_to": fn(pts[b])})
            k += 1
    return _close_chain(shapes)


def _arc_shape(p, q, c, r, ccw, fn, scale) -> dict:
    """An arc from p to q round c (drawing mm, ccw in the drawing) as a sketch arc after fn."""
    pc, qc, cc = fn(p), fn(q), fn(c)
    o, ux, uy = fn((0.0, 0.0)), fn((1.0, 0.0)), fn((0.0, 1.0))
    mirrored = (ux[0] - o[0]) * (uy[1] - o[1]) - (ux[1] - o[1]) * (uy[0] - o[0]) < 0
    if mirrored:
        ccw = not ccw
    a1 = math.degrees(math.atan2(pc[1] - cc[1], pc[0] - cc[0]))
    a2 = math.degrees(math.atan2(qc[1] - cc[1], qc[0] - cc[0]))
    R = r * scale
    first, last = [(cc[0] + R * math.cos(math.radians(ang)), cc[1] + R * math.sin(math.radians(ang)))
                   for ang in (a1, a2)]
    s0, s1 = (first, last) if ccw else (last, first)
    if not ccw:
        a1, a2 = a2, a1
    return {"type": "arc", "x": cc[0], "y": cc[1], "r": R, "a1": a1, "a2": a2,
            "x1": s0[0], "y1": s0[1], "x2": s1[0], "y2": s1[1], "_from": first, "_to": last}


def _segs_shapes(segs: list, fn, scale: float) -> list[dict]:
    """Sketch primitives for an exact outline (arrange.outline): its own lines and arcs."""
    shapes = []
    for s in segs:
        c = math.dist(s["a"], s["b"])
        # an arc bulging less than a micron off its chord is a line (a nearly straight Bézier fitted
        # as a huge circle): KOMPAS will not close a contour round such an arc
        flat = s["type"] == "arc" and s["r"] - math.sqrt(max(0.0, s["r"] ** 2 - c * c / 4)) < 1e-3 \
            and arrange._sweep(s) < math.pi
        if s["type"] == "arc" and c >= 0.02 and not flat:
            shapes.append(_arc_shape(s["a"], s["b"], s["c"], s["r"], s["ccw"], fn, scale))
        elif math.dist(s["a"], s["b"]) > 1e-9:
            # a sliver of an arc is a line: KOMPAS fails on a zero-length arc
            shapes.append({"type": "line", "_from": fn(s["a"]), "_to": fn(s["b"])})
    return _close_chain(shapes)


SNAP_RADII: list = []   # radii the drawing gives (R and Ø/2), set per drawing by the app
SNAPPED: dict = {}      # radius → arcs made exactly that radius; "касание" → fillets made exactly tangent


def _snap_r(R: float) -> float | None:
    """The drawing's radius this arc was meant to have (drawn arcs are off by a few hundredths)."""
    best = min(SNAP_RADII, key=lambda v: abs(v - R), default=None)
    return best if best is not None and abs(best - R) <= max(0.03, 0.004 * best) else None


def _true_fillets(shapes: list) -> None:
    """An arc between two lines that it (nearly) touches is a fillet: it is rebuilt exactly tangent
    to both lines, with the drawing's radius where one fits — the lines keep their direction and
    end where the new arc starts. Arcs that do not fit the pattern are left alone."""
    m = len(shapes)
    if m < 3:
        return
    for i, s in enumerate(shapes):
        prev, nxt = shapes[i - 1], shapes[(i + 1) % m]
        if s["type"] != "arc" or prev["type"] != "line" or nxt["type"] != "line" or prev is nxt:
            continue
        p0, p1 = prev["_from"], prev["_to"]
        q0, q1 = nxt["_from"], nxt["_to"]
        L1, L2 = math.dist(p0, p1), math.dist(q0, q1)
        if L1 < 1e-6 or L2 < 1e-6:
            continue
        u1 = ((p1[0] - p0[0]) / L1, (p1[1] - p0[1]) / L1)
        u2 = ((q1[0] - q0[0]) / L2, (q1[1] - q0[1]) / L2)
        c, R = (s["x"], s["y"]), s["r"]
        # the arc's direction at its ends in the chain's order (from _from to _to)
        ccw = math.dist(s["_from"], (s["x1"], s["y1"])) < math.dist(s["_from"], (s["x2"], s["y2"]))
        sg = 1 if ccw else -1

        def tangent(pt):
            rx, ry = pt[0] - c[0], pt[1] - c[1]
            r = math.hypot(rx, ry) or 1e-12
            return (-ry / r * sg, rx / r * sg)
        t0, t1 = tangent(s["_from"]), tangent(s["_to"])
        if u1[0] * t0[0] + u1[1] * t0[1] < math.cos(math.radians(0.5)) or \
                u2[0] * t1[0] + u2[1] * t1[1] < math.cos(math.radians(0.5)):
            continue
        cross = u1[0] * u2[1] - u1[1] * u2[0]
        if abs(cross) < 1e-3:            # parallel lines: a half-round end, not a corner fillet
            continue
        Rn = _snap_r(R) or R
        # the centre is on the arc's side of both lines (left of the chain when it turns left)
        n1 = (-u1[1] * sg, u1[0] * sg)
        n2 = (-u2[1] * sg, u2[0] * sg)
        a = (p0[0] + n1[0] * Rn, p0[1] + n1[1] * Rn)      # a point of line 1 moved by Rn
        b = (q0[0] + n2[0] * Rn, q0[1] + n2[1] * Rn)
        # a + u1·t = b + u2·w
        t = ((b[0] - a[0]) * u2[1] - (b[1] - a[1]) * u2[0]) / cross
        C = (a[0] + u1[0] * t, a[1] + u1[1] * t)
        T1 = (C[0] - n1[0] * Rn, C[1] - n1[1] * Rn)
        T2 = (C[0] - n2[0] * Rn, C[1] - n2[1] * Rn)
        if math.dist(C, c) > 0.3 or math.dist(T1, s["_from"]) > 0.3 or math.dist(T2, s["_to"]) > 0.3:
            continue
        # each new end must stay on its line's own length
        if (T1[0] - p0[0]) * u1[0] + (T1[1] - p0[1]) * u1[1] < 1e-6 or (q1[0] - T2[0]) * u2[0] + (q1[1] - T2[1]) * u2[1] < 1e-6:
            continue
        a1 = math.degrees(math.atan2(T1[1] - C[1], T1[0] - C[0]))
        a2 = math.degrees(math.atan2(T2[1] - C[1], T2[0] - C[0]))
        s0, s1 = (T1, T2) if ccw else (T2, T1)
        if not ccw:
            a1, a2 = a2, a1
        s.update({"x": C[0], "y": C[1], "r": Rn, "a1": a1, "a2": a2, "x1": s0[0], "y1": s0[1],
                  "x2": s1[0], "y2": s1[1], "_from": T1, "_to": T2, "_exact": True})
        SNAPPED["касание"] = SNAPPED.get("касание", 0) + 1
        if Rn != R:
            SNAPPED[Rn] = SNAPPED.get(Rn, 0) + 1


def _close_chain(shapes: list) -> list[dict]:
    _true_fillets(shapes)
    # close the chain exactly: each line starts where the previous piece ends and ends where the
    # next one starts
    m = len(shapes)
    for i, s in enumerate(shapes):
        prev = shapes[i - 1]
        if s["type"] == "arc" and prev["type"] == "arc" and m > 1:
            # two arcs meeting: each end was computed on its own circle and they differ by a few
            # millionths — the second one starts exactly where the first ends
            p = prev["_to"]
            s["_from"] = p
            if math.isclose(s["x1"], s["_to"][0], abs_tol=1e-12) and math.isclose(s["y1"], s["_to"][1], abs_tol=1e-12):
                s["x2"], s["y2"] = p     # clockwise in the chain: the chain's start is the arc's end
            else:
                s["x1"], s["y1"] = p
    for i, s in enumerate(shapes):
        if s["type"] == "line":
            prev, nxt = shapes[i - 1], shapes[(i + 1) % m]
            p = prev["_to"] if prev["type"] == "arc" else s["_from"]
            q = nxt["_from"] if nxt["type"] == "arc" else s["_to"]
            s.update({"x1": p[0], "y1": p[1], "x2": q[0], "y2": q[1]})
    for s in shapes:
        s.pop("_from", None)
        s.pop("_to", None)
        if s["type"] != "arc" or s.pop("_exact", False):
            continue
        snap = _snap_r(s["r"])
        if snap is not None and snap != s["r"]:
            r0, keep = s["r"], dict(s)
            s["r"] = snap
            if _recentre(s):
                SNAPPED[snap] = SNAPPED.get(snap, 0) + 1
                continue
            s.clear()
            s.update(keep, r=r0)
        _recentre(s)
    return shapes


def _recentre(s: dict) -> bool:
    """An arc whose end was moved onto its neighbour's: the centre moves the hair needed for both
    ends to lie exactly on the circle again (KOMPAS will not close a contour otherwise)."""
    (x1, y1), (x2, y2), R = (s["x1"], s["y1"]), (s["x2"], s["y2"]), s["r"]
    c = math.hypot(x2 - x1, y2 - y1)
    if c < 1e-9:
        return True
    if c >= 2 * R:
        if c > 2 * R + 0.02:
            return False
        R, h = c / 2, 0.0
    else:
        h = math.sqrt(R * R - c * c / 4)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    nx, ny = -(y2 - y1) / c, (x2 - x1) / c
    # counter-clockwise from 1 to 2: the centre is left of the chord for a sweep under 180°
    cands = [(mx + h * nx, my + h * ny), (mx - h * nx, my - h * ny)]
    cx, cy = min(cands, key=lambda p: math.dist(p, (s["x"], s["y"])))
    if math.dist((cx, cy), (s["x"], s["y"])) > 0.1:
        return False
    s.update(x=cx, y=cy, r=R, a1=math.degrees(math.atan2(y1 - cy, x1 - cx)), a2=math.degrees(math.atan2(y2 - cy, x2 - cx)))
    return True


def _rdp_open(pts, tol):
    """Ramer–Douglas–Peucker for an open polyline."""
    if len(pts) < 3:
        return list(pts)
    (ax, ay), (bx, by) = pts[0], pts[-1]
    L = math.hypot(bx - ax, by - ay) or 1e-12
    d = [abs((by - ay) * (x - ax) - (bx - ax) * (y - ay)) / L for x, y in pts[1:-1]]
    i = max(range(len(d)), key=lambda k: d[k])
    if d[i] <= tol:
        return [pts[0], pts[-1]]
    return _rdp_open(pts[:i + 2], tol)[:-1] + _rdp_open(pts[i + 1:], tol)


def _rdp_closed(pts, tol):
    def rdp(p):
        if len(p) < 3:
            return p
        (ax, ay), (bx, by) = p[0], p[-1]
        L = math.hypot(bx - ax, by - ay)
        dd = [(math.hypot(x - ax, y - ay) if L < 1e-12 else abs((by - ay) * (x - ax) - (bx - ax) * (y - ay)) / L)
              for x, y in p[1:-1]]
        i = max(range(len(dd)), key=dd.__getitem__)
        if dd[i] <= tol:
            return [p[0], p[-1]]
        return rdp(p[:i + 2])[:-1] + rdp(p[i + 1:])
    far = max(range(len(pts)), key=lambda k: math.dist(pts[0], pts[k]))
    out = rdp(pts[:far + 1])[:-1] + rdp(pts[far:] + [pts[0]])[:-1]
    return [(round(x, 3), round(y, 3)) for x, y in out]


def render_box(doc: dict, box: tuple, out_png: str, px_per_mm: float = 8.0, margin_mm: float = 4.0) -> dict:
    """One view drawn on its own, with the same conventions as render()."""
    x0, y0, x1, y1 = box[0] - margin_mm, box[1] - margin_mm, box[2] + margin_mm, box[3] + margin_mm
    sub = []
    for e in doc["entities"]:
        pts = _points(e)
        if pts and all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in pts):
            sub.append(e)
    return render({"entities": sub}, out_png, px_per_mm, margin_mm, bounds=(x0, y0, x1, y1))


_NUM = re.compile(r"(?:(%%c|Ø|∅|ø|%%C)|(R))?\s*(\d+(?:[.,]\d+)?)")
# A dimension text: optional "2 отв." / "4 отв", optional Ø (KOMPAS exports its symbol-font
# diameter sign as "Ж"), R or M, the number, optional tolerance or fit, optional "×45°".
_DIM_TEXT = re.compile(
    r"(?:(\d+)\s*отв\.?\s*)?(?P<pre>Ø|∅|ø|%%c|Ж|R|M)?\s*(?P<num>\d+(?:[.,]\d+)?)"
    r"\s*(?:[A-Za-z]{1,2}\d{1,2}\s*)?\(?(?:±\s*\d+(?:[.,]\d+)?|[+-]\d+(?:[.,]\d+)?(?:\s*[+-]\d+(?:[.,]\d+)?)?)?\)?"
    r"\s*(?:°|[x×х]\s*45°?)?\*?(?:\s*(\d+)\s*отв\.?)?", re.I)   # «11,46*» — a size for reference


def clean_text(t: str) -> str:
    """MTEXT without its formatting codes (\\H3.0; \\W0.9; \\Q15; \\A1; {}), and without the
    'KDIMSTYLE (…)' tail some exporters append to dimension texts."""
    t = re.sub(r"\\[A-Za-z][^;\\]*;|[{}]", "", t)
    t = re.sub(r"\\P", " ", t)
    t = re.sub(r"KDIMSTYLE\s*\(.*?\)", "", t)
    return t.replace("%%C", "%%c").strip()


def text_box(e: dict, t: str) -> list[float] | None:
    """The box a text takes on the sheet, mm [x0, y0, x1, y1] (y up): a dimension's text is centred
    on its text point (11/21), a plain text starts at its insertion point (10/20); turned by 50."""
    h = float(e.get(40) or 3.5)
    w = max(1, len(t)) * 0.62 * h
    if e["type"] == "DIMENSION" and isinstance(e.get(11), (int, float)):
        cx, cy, centred = e[11], e.get(21, 0.0), True
    elif isinstance(e.get(10), (int, float)):
        cx, cy, centred = e[10], e.get(20, 0.0), False
    else:
        return None
    a = math.radians(float(e.get(50) or 0.0))
    ux, uy, vx, vy = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
    if centred:
        cx, cy = cx - ux * w / 2 - vx * h / 2, cy - uy * w / 2 - vy * h / 2
    pts = [(cx + ux * s + vx * q, cy + uy * s + vy * q) for s in (0, w) for q in (0, h)]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


def numbers(doc: dict) -> dict:
    """Dimension numbers written on the drawing: diameters (Ø/%%c), radii (R) and plain values.
    Only texts that are a dimension as a whole count — not the title block, technical requirements
    or tolerances. A dimension with a measured value but no text override also counts."""
    out = {"values": [], "diameters": [], "radii": []}
    where = []      # [kind, value, x, y]: where each number is written (mm), for the review window
    for e in doc["entities"]:
        texts = []
        if e["type"] in ("TEXT", "MTEXT") and e.get("from_dimension"):
            continue  # pieces of a dimension's own picture: the DIMENSION entity carries the whole text
        if e["type"] in ("TEXT", "MTEXT", "DIMENSION") and e.get("text") and e["text"] != "<>":
            texts.append(clean_text(e["text"]))
        elif e["type"] == "DIMENSION" and "measure" in e:
            texts.append(f"{e['measure']:g}")
        for t in texts:
            m = _DIM_TEXT.fullmatch(t)
            if not m:
                continue
            v = float(m.group("num").replace(",", "."))
            if v <= 0 or "°" in t and not re.search(r"[x×х]\s*45", t, re.I):
                continue  # an angle, not a size (a chamfer "2×45°" keeps its 2)
            pre = (m.group("pre") or "").upper()
            key = "radii" if pre == "R" else "values" if pre in ("", "M") else "diameters"
            if t.rstrip().endswith("*") and key == "radii":
                continue  # «R25,66*» — a size for reference, rounded: no arc is made exactly that
            if t.rstrip().endswith("*"):
                key = "values"   # «11,46*» counts as a size, but nothing is snapped to it as a Ø
            out[key].append(v)
            box = text_box(e, t)
            if box:
                where.append([key, v, *box])
    for k in out:
        out[k] = sorted(set(out[k]))
    out["where"] = where
    return out


def render(doc: dict, out_png: str, px_per_mm: float = 8.0, margin_mm: float = 10.0,
           bounds: tuple | None = None, texts: bool = False) -> dict:
    """Draws the drawing to a PNG at an exact scale: visible lines heavy, hidden lines dashed,
    dimension/axis/thin geometry light — the conventions the raster analysis relies on.
    texts: the numbers and notes too (the picture the user looks at; the analysis does without)."""
    xs, ys = [], []
    for e in doc["entities"]:
        for p in _points(e):
            xs.append(p[0])
            ys.append(p[1])
    if not xs:
        raise ValueError("в DXF нет геометрии")
    if bounds:
        x0, y0, x1, y1 = bounds
    else:
        x0, y0, x1, y1 = min(xs) - margin_mm, min(ys) - margin_mm, max(xs) + margin_mm, max(ys) + margin_mm
    W, H = int((x1 - x0) * px_per_mm), int((y1 - y0) * px_per_mm)
    if W * H > 60_000_000:
        px_per_mm = math.sqrt(60_000_000 / ((x1 - x0) * (y1 - y0)))
        W, H = int((x1 - x0) * px_per_mm), int((y1 - y0) * px_per_mm)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    to = lambda x, y: ((x - x0) * px_per_mm, (y1 - y) * px_per_mm)  # noqa: E731
    thick = max(2, int(round(0.5 * px_per_mm)))
    thin = 1
    for e in doc["entities"]:
        if e["type"] in ("TEXT", "MTEXT", "DIMENSION"):
            continue
        light = e.get("from_dimension") or e["kind"] in ("center", "thin")
        width = thin if light else thick
        dash = e["kind"] == "hidden"
        for seg in _segments(e):
            (ax, ay), (bx, by) = to(*seg[0]), to(*seg[1])
            if dash:
                _dashed(d, ax, ay, bx, by, 3 * px_per_mm, 1.5 * px_per_mm, max(1, thick // 2 + 1))
            else:
                d.line([ax, ay, bx, by], fill=0, width=width)
    if texts:
        _draw_texts(img, doc, to, px_per_mm)
    img.save(out_png)
    return {"png": out_png, "px_per_mm": px_per_mm, "origin": (x0, y1)}


def _draw_texts(img, doc: dict, to, ppm: float) -> None:
    from PIL import ImageFont
    font_file = next((f for f in (r"C:\Windows\Fonts\GOST_AU.ttf", r"C:\Windows\Fonts\arial.ttf")
                      if os.path.exists(f)), None)
    fonts = {}
    for e in doc["entities"]:
        if e["type"] not in ("TEXT", "MTEXT", "DIMENSION") or not e.get("text"):
            continue
        if e["type"] == "DIMENSION" and e.get("block"):
            continue        # its block's own text pieces are drawn
        t = clean_text(e["text"])
        if not t or t == "<>":
            continue
        box = text_box(e, t)
        if not box:
            continue
        h = float(e.get(40) or 3.5)
        size = max(6, int(h * ppm))
        if size not in fonts:
            try:
                fonts[size] = ImageFont.truetype(font_file, size) if font_file else ImageFont.load_default()
            except OSError:
                fonts[size] = ImageFont.load_default()
        f = fonts[size]
        tw = int(f.getlength(t)) + 4
        tile = Image.new("L", (max(1, tw), size + size // 3), 255)
        ImageDraw.Draw(tile).text((2, 0), t, fill=0, font=f)
        rot = float(e.get(50) or 0.0)
        if abs(rot) > 0.5:
            tile = tile.rotate(rot, expand=True, fillcolor=255)
        px, py = to(box[0], box[3])
        img.paste(tile, (int(px), int(py)), Image.eval(tile, lambda v: 255 - v))


def view_size_mm(path: str, px_per_mm: float, snap_to: list[float], tol: float = 0.8) -> tuple[float, float]:
    """Exact size of the part in a rendered view: silhouette box minus the outline thickness,
    snapped to a number written on the drawing when one is that close."""
    import vision
    s = vision.silhouette(path, upscale=1, inset_stroke=True)
    x0, y0, x1, y1 = s["bbox"]                     # already moved in to the middle of the outline
    out = []
    for v in ((x1 - x0) / px_per_mm, (y1 - y0) / px_per_mm):
        near = min(snap_to, key=lambda c: abs(c - v), default=None)
        out.append(float(near) if near is not None and abs(near - v) <= tol else round(v * 2) / 2)
    return out[0], out[1]


def _points(e):
    t = e["type"]
    if t == "LINE":
        return [(e.get(10, 0), e.get(20, 0)), (e.get(11, 0), e.get(21, 0))]
    if t in ("CIRCLE", "ARC"):
        r = e.get(40, 0)
        return [(e.get(10, 0) - r, e.get(20, 0) - r), (e.get(10, 0) + r, e.get(20, 0) + r)]
    if t == "LWPOLYLINE":
        if any(e.get("bulge") or []):  # arcs bulge beyond their end points
            return [p for s in _segments(e) for p in s]
        return [tuple(p) for p in e["pts"] if p[1] is not None]
    return []


def _segments(e, step_deg: float = 5.0):
    return [(a, b) for a, b, _ in _segments_tagged(e, step_deg)]


def _segments_tagged(e, step_deg: float = 5.0):
    """Straight pieces (a, b, arc) of an entity; arc = (cx, cy, r) for pieces of a circular arc."""
    t = e["type"]
    if t == "LINE":
        return [((e.get(10, 0), e.get(20, 0)), (e.get(11, 0), e.get(21, 0)), None)]
    if t == "LWPOLYLINE":
        raw = e["pts"]
        bulges = e.get("bulge") or [0.0] * len(raw)
        verts = [(tuple(q), bulges[i] if i < len(bulges) else 0.0) for i, q in enumerate(raw) if q[1] is not None]
        pairs = list(zip(verts, verts[1:]))
        if e.get("closed") and len(verts) > 2:
            pairs.append((verts[-1], verts[0]))
        segs = []
        for (a, bulge), (b, _) in pairs:
            if abs(bulge) < 1e-9:
                segs.append((a, b, None))
                continue
            # arc through a and b: sweep 4·atan(bulge), centre on the chord's perpendicular
            dx, dy = b[0] - a[0], b[1] - a[1]
            c = math.hypot(dx, dy)
            if c < 1e-12:
                continue
            sweep = 4 * math.atan(bulge)
            off = c / 2 * (1 - bulge * bulge) / (2 * bulge)
            cx, cy = (a[0] + b[0]) / 2 - dy / c * off, (a[1] + b[1]) / 2 + dx / c * off
            r = math.hypot(a[0] - cx, a[1] - cy)
            a0 = math.atan2(a[1] - cy, a[0] - cx)
            n = max(2, int(math.ceil(abs(math.degrees(sweep)) / step_deg)))
            pts = [a] + [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n))
                         for k in range(1, n)] + [b]
            tag = (round(cx, 6), round(cy, 6), round(r, 6))
            ARC_ENDS.setdefault(tag, set()).update({a, b})
            segs += [(p, q, tag) for p, q in zip(pts, pts[1:])]
        return segs
    if t in ("CIRCLE", "ARC"):
        cx, cy, r = e.get(10, 0), e.get(20, 0), e.get(40, 0)
        a0, a1 = (0.0, 360.0) if t == "CIRCLE" else (e.get(50, 0), e.get(51, 0))
        if a1 <= a0:
            a1 += 360
        n = max(4, int((a1 - a0) / step_deg))
        pts = [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)),
                cy + r * math.sin(math.radians(a0 + (a1 - a0) * k / n))) for k in range(n + 1)]
        tag = (round(cx, 6), round(cy, 6), round(r, 6))
        if t == "ARC":
            ARC_ENDS.setdefault(tag, set()).update({pts[0], pts[-1]})
        return [(p, q, tag) for p, q in zip(pts, pts[1:])]
    return []


ARC_ENDS: dict = {}  # arc (cx, cy, r) → its end points, filled while entities are cut into pieces


def _dashed(d, ax, ay, bx, by, dash, gap, width):
    length = math.hypot(bx - ax, by - ay)
    if length == 0:
        return
    ux, uy = (bx - ax) / length, (by - ay) / length
    s = 0.0
    while s < length:
        e = min(s + dash, length)
        d.line([ax + ux * s, ay + uy * s, ax + ux * e, ay + uy * e], fill=0, width=width)
        s = e + gap
