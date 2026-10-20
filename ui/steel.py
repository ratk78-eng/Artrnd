"""Steel structure detail drawings (КМД, e.g. exported from Tekla Structures): a rolled profile or a
plate whose size is written in the specification ("Двутавр 20К1 ГОСТ 26020-83", "100х6 ГОСТ 30245",
"16x200 ГОСТ 19903", "L= 653 мм"), and views of the part that show only what was done to it —
end cuts, holes, slots.

So the body is not traced from the views at all: the cross-section comes from the profile table,
the length from the specification, and every view contributes cuts — whatever lies outside its
outline is cut off through the part, and closed contours inside it (holes, slots) are cut out.
Long parts are drawn with breaks; the views keep their true length (the geometry is 1:1), the break
only leaves a gap, which is bridged here. The mass in the specification checks the result.
"""
from __future__ import annotations

import math
import os
import re

import dxf

STEEL_DENSITY = 7.85e-6  # kg/mm³

# Hot-rolled I-beams with parallel flanges (ГОСТ 26020-83 / ГОСТ Р 57837-2017): h, b, s, t, r, mm.
# The drawing itself gives h and b exactly; s, t and r come from here.
I_BEAMS = {
    "10Б1": (100, 55, 4.1, 5.7, 7), "12Б1": (117.6, 64, 3.8, 5.1, 7), "12Б2": (120, 64, 4.4, 6.3, 7),
    "14Б1": (137.4, 73, 3.8, 5.6, 7), "14Б2": (140, 73, 4.7, 6.9, 7), "16Б1": (157, 82, 4, 5.9, 9),
    "16Б2": (160, 82, 5, 7.4, 9), "18Б1": (177, 91, 4.3, 6.5, 9), "18Б2": (180, 91, 5.3, 8, 9),
    "20Б1": (200, 100, 5.6, 8.5, 12), "25Б1": (248, 124, 5, 8, 12), "25Б2": (250, 125, 6, 9, 12),
    "30Б1": (296, 140, 5.8, 8.5, 15), "30Б2": (300, 140, 6.5, 10.5, 15), "35Б1": (346, 155, 6.2, 8.5, 18),
    "35Б2": (349, 155, 6.5, 10, 18), "40Б1": (392, 165, 7, 9.5, 21), "40Б2": (396, 165, 7.5, 11.5, 21),
    "20Ш1": (193, 150, 6, 9, 13), "23Ш1": (226, 155, 6.5, 10, 14), "26Ш1": (251, 180, 7, 10, 16),
    "30Ш1": (291, 200, 8, 11, 18), "35Ш1": (338, 250, 9.5, 12.5, 20), "40Ш1": (388, 300, 9.5, 14, 22),
    "20К1": (195, 200, 6.5, 10, 13), "20К2": (198, 200, 7, 11.5, 13), "23К1": (227, 240, 7, 10.5, 14),
    "23К2": (230, 240, 8, 12, 14), "26К1": (255, 260, 8, 12, 16), "26К2": (258, 260, 9, 13.5, 16),
    "30К1": (296, 300, 9, 13.5, 18), "30К2": (300, 300, 10, 15.5, 18), "35К1": (343, 350, 10, 15, 20),
    "40К1": (393, 400, 11, 16.5, 22),
}
_LAT = str.maketrans("BKSHbksh", "БКШШбкшш")

FRAME_WORDS = re.compile(r"tekla|утв\.|разраб|масштаб|формат|инв\.|подп\.|н\.контр|кол\.уч", re.I)


# ---------------- specification ----------------

def texts(doc: dict) -> list[dict]:
    return [e for e in doc["entities"] if e["type"] in ("TEXT", "MTEXT") and e.get("text")]


def _clean(t: str) -> str:
    return re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", t).replace("%%c", "Ø").replace("%%C", "Ø").strip()


def parse_spec(doc: dict) -> dict | None:
    """Profile, length, steel and mass from the specification / title block texts."""
    ts = [_clean(e["text"]) for e in texts(doc)]
    spec: dict = {}
    for i, t in enumerate(ts):
        m = re.search(r"L\s*=\s*(\d+(?:[.,]\d+)?)", t)
        if m and "length" not in spec:
            spec["length"] = float(m.group(1).replace(",", "."))
            # Tekla's specification row: position | mass of one piece | L= … | name | quantity | total
            for back in (1, 2):
                if i - back >= 0 and re.fullmatch(r"\d+(?:[.,]\d+)?", ts[i - back]):
                    spec["mass_kg"] = float(ts[i - back].replace(",", "."))
                    break
            if i + 2 < len(ts) and re.fullmatch(r"\d+", ts[i + 2]):
                spec["quantity"] = int(ts[i + 2])
            if i + 1 < len(ts) and re.fullmatch(r"[А-Яа-яЁё ]{3,}", ts[i + 1]):
                spec["name"] = ts[i + 1]
        m = re.fullmatch(r"[СC](\d{3})(?:\s.*)?", t)
        if m and "steel" not in spec:
            spec["steel"] = "С" + m.group(1)
        m = re.search(r"Деталь\s*(\S+)", t)
        if m and "title" not in spec:
            spec["title"] = t
        m = re.search(r"(\d{2,3})\s*([БКШBKSH])\s*(\d)\b.*ГОСТ", t)
        if m and "profile" not in spec:
            name = f"{m.group(1)}{m.group(2).upper().translate(_LAT)}{m.group(3)}"
            spec["profile"] = {"kind": "I", "name": name, "gost": t[t.find("ГОСТ"):]}
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*[хx×Х]\s*(\d+(?:[.,]\d+)?)(?:\s*[хx×Х]\s*(\d+(?:[.,]\d+)?))?\s*ГОСТ\s*(30245|8639|8645)", t)
        if m and "profile" not in spec:
            a, b2, c = (float(g.replace(",", ".")) if g else None for g in m.groups()[:3])
            w, h, wall = (a, a, b2) if c is None else (a, b2, c)
            spec["profile"] = {"kind": "tube", "name": f"{m.group(0).split('ГОСТ')[0].strip()}", "gost": t[t.find("ГОСТ"):],
                               "a": w, "b": h, "wall": wall}
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*[хx×Х]\s*(\d+(?:[.,]\d+)?)\s*ГОСТ\s*(19903|82|103)", t) or \
            re.fullmatch(r"(?:—|-|PL)(\d+(?:[.,]\d+)?)(?:\s*[хx×Х]\s*(\d+(?:[.,]\d+)?))?", t) or \
            re.fullmatch(r"Лист\s*(\d+(?:[.,]\d+)?)\s*[хx×Х]\s*(\d+(?:[.,]\d+)?)", t)  # not the title block's «Лист 1»
        if m and "profile" not in spec and float(m.group(1).replace(",", ".")) >= 1:
            # (a tolerance written apart, «-0,1» of «45 -0,1», is no plate 0.1 thick)
            th = float(m.group(1).replace(",", "."))
            spec["profile"] = {"kind": "plate", "name": f"лист {th:g}", "gost": t[t.find("ГОСТ"):] if "ГОСТ" in t else "",
                               "t": th, "b": float(m.group(2).replace(",", ".")) if m.group(2) else None}
    return spec if "profile" in spec else None


# ---------------- layers and views ----------------

def _bbox(es):
    pts = [p for e in es for p in dxf._points(e)]
    pts += [(e.get(10, 0.0), e.get(20, 0.0)) for e in es if e["type"] in ("TEXT", "MTEXT")]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _length(e) -> float:
    return sum(math.dist(a, b) for a, b in dxf._segments(e))


def layer_roles(doc: dict) -> dict:
    """Which layer holds what: the frame/title block, dimensions, notes, the part outline, holes."""
    by: dict[str, list] = {}
    for e in doc["entities"]:
        by.setdefault(e.get("layer", ""), []).append(e)
    frame = {lay for lay, es in by.items() if any(FRAME_WORDS.search(e.get("text") or "") for e in es)}
    rest = {lay: es for lay, es in by.items() if lay not in frame}

    def numeric_texts(es):
        return sum(1 for e in es if e["type"] in ("TEXT", "MTEXT") and re.fullmatch(r"[\d.,°]+", _clean(e.get("text") or "")))
    dims = max(rest, key=lambda lay: numeric_texts(rest[lay]), default=None)
    notes = {lay for lay, es in rest.items() if lay != dims and any(
        re.search(r"отв|паз|шт", e.get("text") or "", re.I) for e in es)}
    geo = {lay: es for lay, es in rest.items() if lay != dims and lay not in notes}
    outline = max(geo, key=lambda lay: sum(_length(e) for e in geo[lay] if e["type"] in ("LINE", "LWPOLYLINE")),
                  default=None)
    holes = {lay for lay in geo if lay != outline}  # arcs are often exploded into lines: any geometry
    return {"frame": frame, "dims": dims, "notes": notes, "outline": outline, "holes": holes}


def _cluster(es: list, gap: float = 60.0) -> list[list]:
    """Groups entities whose boxes (grown by `gap`) touch: one group per view."""
    boxes = [_bbox([e]) for e in es]
    parent = list(range(len(es)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    order = sorted(range(len(es)), key=lambda i: boxes[i][0])
    active: list[int] = []
    for i in order:
        bi = boxes[i]
        active = [j for j in active if boxes[j][2] + gap >= bi[0] - gap]
        for j in active:
            bj = boxes[j]
            if bi[1] - gap <= bj[3] + gap and bj[1] - gap <= bi[3] + gap:
                parent[find(i)] = find(j)
        active.append(i)
    groups: dict[int, list] = {}
    for i in range(len(es)):
        groups.setdefault(find(i), []).append(es[i])
    return sorted(groups.values(), key=lambda g: (_bbox(g)[0], -_bbox(g)[3]))


def _levels(segs, width):
    """Top and bottom edges of a view: the extreme y levels carrying long horizontal lines."""
    cover: dict[float, float] = {}
    for (ax, ay), (bx, by) in segs:
        if abs(ay - by) < 0.01 and abs(bx - ax) >= 3:
            y = round((ay + by) / 2, 2)
            cover[y] = cover.get(y, 0.0) + abs(bx - ax)
    good = [y for y, c in cover.items() if c >= 0.03 * width]
    return (min(good), max(good)) if good else (None, None)


def view_profile(outline: list, step: float = 0.5) -> dict:
    """The part's outline in one view as a polygon, with breaks bridged.

    For every column along the view: the lowest and highest outline crossing (clamped to the view's
    top/bottom edges). Columns inside a break (neither edge line present, with edge lines on both
    sides) take the shape from the left side of the break."""
    segs = [s for e in outline for s in dxf._segments(e) if math.dist(*s) > 1e-6]
    xs = [p[0] for s in segs for p in s]
    bot, top = _levels(segs, max(xs) - min(xs))
    if bot is None:
        raise ValueError("на виде нет прямых кромок")
    # the part ends where lines meeting its top or bottom edge end: axis lines stick out beyond the
    # ends and break marks beyond the edges, but neither ends on an edge
    on_edge = [p[0] for s in segs for p in s if min(abs(p[1] - bot), abs(p[1] - top)) < 0.05]
    x0, x1 = min(on_edge), max(on_edge)
    mid = (bot + top) / 2
    segs = [s for s in segs if not (abs(s[0][1] - s[1][1]) < 0.01 and abs(s[0][1] - mid) < 0.5)]  # axis line
    segs = [s for s in segs if min(s[0][0], s[1][0]) >= x0 - 0.05 and max(s[0][0], s[1][0]) <= x1 + 0.05
            or abs(s[0][0] - s[1][0]) < 0.01]
    edge = [s for s in segs if abs(s[0][1] - s[1][1]) < 0.01 and min(abs(s[0][1] - bot), abs(s[0][1] - top)) < 0.05]
    # the real extent along the view: where the edge lines and the outline end (break marks stick out
    # beyond top/bottom only, so x extent is safe)
    n = max(1, int(math.ceil((x1 - x0) / step)))
    cx = [x0 + (k + 0.5) * (x1 - x0) / n for k in range(n)]
    covered = [any(min(a[0], b[0]) - 1e-6 <= x <= max(a[0], b[0]) + 1e-6 for a, b in edge) for x in cx]
    gaps = []
    k = 0
    while k < n:
        if not covered[k]:
            j = k
            while j < n and not covered[j]:
                j += 1
            if k > 0 and j < n:
                gaps.append((cx[k] - step, cx[j - 1] + step))
            k = j
        else:
            k += 1
    in_gap = lambda x: any(g0 <= x <= g1 for g0, g1 in gaps)  # noqa: E731
    slanted = [s for s in segs if abs(s[1][0] - s[0][0]) > 0.01 and not in_gap((s[0][0] + s[1][0]) / 2)]
    lo, hi = [None] * n, [None] * n
    for k, x in enumerate(cx):
        if in_gap(x):
            continue
        ys = []
        for (ax, ay), (bx, by) in slanted:
            if min(ax, bx) <= x <= max(ax, bx):
                ys.append(ay + (by - ay) * (x - ax) / (bx - ax))
        if ys:
            lo[k], hi[k] = max(bot, min(ys)), min(top, max(ys))
    for k in range(n):  # bridge breaks (and any empty column) with the shape on their left
        if lo[k] is None and k > 0 and lo[k - 1] is not None:
            lo[k], hi[k] = lo[k - 1], hi[k - 1]
    for k in range(n - 1, -1, -1):
        if lo[k] is None and k < n - 1 and lo[k + 1] is not None:
            lo[k], hi[k] = lo[k + 1], hi[k + 1]
    pts = [(x0, lo[0])] + [(cx[k], lo[k]) for k in range(n)] + [(x1, lo[-1]), (x1, hi[-1])] + \
          [(cx[k], hi[k]) for k in range(n - 1, -1, -1)] + [(x0, hi[0])]
    # sampling leaves sub-millimetre crumbs at the ends and edges: snap them onto the edges
    snap = lambda v, refs: next((r for r in refs if abs(v - r) < 0.3), v)  # noqa: E731
    pts = [(snap(x, (x0, x1)), snap(y, (bot, top))) for x, y in pts]
    poly = _simplify(pts, 0.05)
    # interior long horizontal lines (flange or wall inner faces) and their offsets from the edges
    inner = {}
    for (ax, ay), (bx, by) in segs:
        if abs(ay - by) < 0.01 and bot + 0.05 < ay < top - 0.05 and abs(bx - ax) >= 3:
            y = round(ay, 2)
            inner[y] = inner.get(y, 0.0) + abs(bx - ax)
    inner_lines = sorted(y for y, c in inner.items() if c >= 0.1 * (x1 - x0))
    return {"x0": x0, "x1": x1, "bot": bot, "top": top, "polygon": poly, "gaps": gaps, "inner": inner_lines}


def _simplify(pts, tol):
    """Douglas–Peucker on a closed outline given as an open list (first..last), split at the point
    farthest from the first one so both halves have a real base line."""
    def rdp(p):
        if len(p) < 3:
            return p
        (ax, ay), (bx, by) = p[0], p[-1]
        L = math.hypot(bx - ax, by - ay)
        if L < 1e-9:
            d = [math.hypot(x - ax, y - ay) for x, y in p[1:-1]]
        else:
            d = [abs((by - ay) * (x - ax) - (bx - ax) * (y - ay)) / L for x, y in p[1:-1]]
        i = max(range(len(d)), key=d.__getitem__)
        if d[i] <= tol:
            return [p[0], p[-1]]
        return rdp(p[:i + 2])[:-1] + rdp(p[i + 1:])
    far = max(range(len(pts)), key=lambda k: math.dist(pts[0], pts[k]))
    out = rdp(pts[:far + 1])[:-1] + rdp(pts[far:])
    clean = []
    for q in out:
        if not clean or math.dist(clean[-1], q) > 1e-6:
            clean.append((round(q[0], 3), round(q[1], 3)))
    return clean


def closed_loops(es: list, tol: float = 0.05) -> list[list[tuple]]:
    """Closed contours (circles, and chains of lines/arcs such as slots) as point lists."""
    loops, open_segs = [], []
    for e in es:
        if e["type"] == "CIRCLE":
            loops.append([a for a, _ in dxf._segments(e, 10)])
        elif e["type"] == "LWPOLYLINE" and e.get("closed"):
            loops.append([a for a, _ in dxf._segments(e)])
        elif e["type"] in ("LINE", "ARC", "LWPOLYLINE"):
            pts = [dxf._segments(e, 10)[0][0]] + [b for _, b in dxf._segments(e, 10)]
            open_segs.append(pts)
    used = [False] * len(open_segs)
    for i, s in enumerate(open_segs):
        if used[i]:
            continue
        chain, used[i] = list(s), True
        grown = True
        while grown and math.dist(chain[0], chain[-1]) > tol:
            grown = False
            for j, t in enumerate(open_segs):
                if used[j]:
                    continue
                if math.dist(chain[-1], t[0]) <= tol:
                    chain += t[1:]
                elif math.dist(chain[-1], t[-1]) <= tol:
                    chain += t[-2::-1]
                else:
                    continue
                used[j] = grown = True
                break
        if len(chain) > 3 and math.dist(chain[0], chain[-1]) <= tol:
            loops.append(chain[:-1])
    return loops


def _area(poly) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def _inside(p, poly) -> bool:
    x, y = p
    c = False
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            c = not c
    return c


def _describe_loop(loop) -> dict:
    xs = [p[0] for p in loop]
    ys = [p[1] for p in loop]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    c = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
    round_ = abs(w - h) < 0.1 and abs(_area(loop) - math.pi * w * w / 4) < 0.03 * math.pi * w * w / 4
    return {"center": c, "w": w, "h": h, "round": round_, "points": loop}


# ---------------- the whole drawing ----------------

def analyze(doc: dict) -> dict | None:
    """Everything the steel path needs, independent of the values the user may still correct."""
    spec = parse_spec(doc)
    if spec is None:
        return None
    roles = layer_roles(doc)
    if roles["outline"] is None:
        return None
    outline = [e for e in doc["entities"] if e.get("layer") == roles["outline"] and e["type"] in ("LINE", "LWPOLYLINE", "ARC")]
    holes_src = [e for e in doc["entities"] if e.get("layer") in roles["holes"] | {roles["outline"]}
                 and e["type"] in ("LINE", "ARC", "CIRCLE", "LWPOLYLINE")]
    note_text = " ".join(_clean(e["text"]) for e in texts(doc) if e.get("layer") in roles["notes"])
    # pieces of one view separated by a break have the same top and bottom edges: join them
    groups = []
    for g in _cluster(outline):
        try:
            p = view_profile(g)
        except ValueError:
            continue
        if groups:
            q_es, q = groups[-1]
            same = abs(p["bot"] - q["bot"]) < 0.3 and abs(p["top"] - q["top"]) < 0.3
            fits = p["x1"] - q["x0"] <= (spec.get("length") or math.inf) + 1.0
            if same and fits and p["x0"] > q["x1"]:
                merged = q_es + g
                groups[-1] = (merged, view_profile(merged))
                continue
        groups.append((g, p))
    views = []
    for group, prof in groups:
        if prof["x1"] - prof["x0"] < 20:
            continue
        box = (prof["x0"] - 1, prof["bot"] - 1, prof["x1"] + 1, prof["top"] + 1)
        mine = [e for e in holes_src if (lambda b: b and box[0] <= b[0] and b[2] <= box[2] and box[1] <= b[1] and b[3] <= box[3])(_bbox([e]))]
        loops = [_describe_loop(lp) for lp in closed_loops(mine)]
        outer_area = _area(prof["polygon"])
        loops = [lp for lp in loops if _area(lp["points"]) < 0.5 * outer_area and _inside(lp["center"], prof["polygon"])]
        # one contour per position (a circle and its centre-mark chain could both close)
        uniq = []
        for lp in sorted(loops, key=lambda q: -_area(q["points"])):
            if not any(math.dist(lp["center"], u["center"]) < 0.5 for u in uniq):
                uniq.append(lp)
        views.append({**prof, "holes": uniq, "bbox": _bbox(group)})
    if not views:
        return None
    dim_numbers = sorted({float(_clean(e["text"]).replace(",", ".")) for e in texts(doc)
                          if e.get("layer") == roles["dims"] and re.fullmatch(r"\d+(?:[.,]\d+)?", _clean(e["text"]))})
    return {"spec": spec, "roles": {k: sorted(v) if isinstance(v, set) else v for k, v in roles.items()},
            "notes_text": note_text, "views": views, "dim_numbers": dim_numbers}


# ---------------- sections ----------------

def _arc_pts(cx, cy, r, a0, a1, n=8):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * k / n))) for k in range(n + 1)]


def i_section(h, b, s, t, r) -> list[tuple]:
    """I-beam cross-section in (Y, Z): centred on Y, bottom at Z = 0, root fillets r."""
    pts = [(-b / 2, 0), (b / 2, 0), (b / 2, t)]
    pts += _arc_pts(s / 2 + r, t + r, r, 270, 180)
    pts += _arc_pts(s / 2 + r, h - t - r, r, 180, 90)
    pts += [(b / 2, h - t), (b / 2, h), (-b / 2, h), (-b / 2, h - t)]
    pts += _arc_pts(-s / 2 - r, h - t - r, r, 90, 0)
    pts += _arc_pts(-s / 2 - r, t + r, r, 0, -90)
    pts += [(-b / 2, t)]
    return [(round(y, 4), round(z, 4)) for y, z in pts]


def rounded_rect(w, h, r, cy=0.0, cz=0.0) -> list[tuple]:
    """Rectangle w × h centred at (cy, cz + h/2) with corner radius r, in (Y, Z)."""
    y0, y1, z0, z1 = cy - w / 2, cy + w / 2, cz, cz + h
    if r <= 0:
        return [(y0, z0), (y1, z0), (y1, z1), (y0, z1)]
    pts = _arc_pts(y1 - r, z0 + r, r, 270, 360) + _arc_pts(y1 - r, z1 - r, r, 0, 90) + \
        _arc_pts(y0 + r, z1 - r, r, 90, 180) + _arc_pts(y0 + r, z0 + r, r, 180, 270)
    return [(round(y, 4), round(z, 4)) for y, z in pts]


def tube_sections(a, b, wall) -> tuple[list, list]:
    """Square/rectangular hollow section (ГОСТ 30245): outer corner radius 2t (2.5t over 6 mm), inner t."""
    ro = (2.0 if wall <= 6 else 2.5) * wall
    ri = max(ro - wall, 0.0)
    return rounded_rect(a, b, ro), rounded_rect(a - 2 * wall, b - 2 * wall, ri, 0.0, wall)


# ---------------- reading table, plan ----------------

def reading(st: dict) -> dict:
    """The dimension table shown to the user: the profile's numbers, editable."""
    sp, prof = st["spec"], st["spec"]["profile"]
    dims = []

    def add(i, value, meaning, axis, overall=False):
        dims.append({"id": i, "value": round(float(value), 3), "view": "спецификация", "meaning": meaning,
                     "axis": axis, "overall": overall, "confidence": 1.0})
    main = max(st["views"], key=lambda v: v["x1"] - v["x0"])
    span = main["x1"] - main["x0"]
    length = sp.get("length") or span
    if abs(span - length) <= 1.0:
        # the specification rounds L to whole millimetres and dimensions to 0.5; the view is exact
        length = round(span, 2)
    if prof["kind"] == "I":
        h, b, s, t, r = _i_dims(st)
        add("L", length, "длина балки", "length", True)
        add("h", h, f"высота двутавра {prof['name']}", "height", True)
        add("b", b, "ширина полки", "width", True)
        add("s", s, "толщина стенки", "other")
        add("t", t, "толщина полки", "other")
        add("r", r, "радиус сопряжения стенки с полкой", "radius")
    elif prof["kind"] == "tube":
        add("L", length, "длина трубы", "length", True)
        add("a", prof["a"], "ширина профиля", "width", True)
        add("b", prof["b"], "высота профиля", "height", True)
        add("t", prof["wall"], "толщина стенки", "other")
    else:
        w = main["top"] - main["bot"]
        add("L", main["x1"] - main["x0"], "длина листа (по виду)", "length", True)
        add("b", w, "ширина листа (по виду)", "width", True)
        add("t", prof["t"], "толщина листа", "height", True)
    if prof["kind"] == "I":
        h, b, _, t, _ = _i_dims(st)
        names = ["главный" if _web_view(v, h, b, t) else "сверху" for v in st["views"]]
    else:
        names = [("главный", "сверху", "слева")[min(k, 2)] for k in range(len(st["views"]))]
    out = {"views": [{"image": k + 1, "name": n} for k, n in enumerate(names)],
           "dimensions": dims, "overall": {}, "features": [], "title": sp.get("title") or prof["name"]}
    for d in dims:
        if d["overall"]:
            out["overall"][d["axis"]] = d["id"]
    # no "mass_kg" here: the specification's mass is the blank's, the built part with its cuts is
    # lighter — plan() compares the section with it instead
    return out


def render_views(doc: dict, st: dict, folder: str, max_w: int = 2000) -> tuple[list[str], str]:
    """One picture per view (outline heavy, holes, dimensions with their numbers) and the sheet made
    of them stacked — Tekla places views thousands of millimetres apart, so the raw sheet is useless."""
    from PIL import Image, ImageDraw, ImageFont
    roles = st["roles"]
    skip = set(roles["frame"])
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    files, images = [], []
    for k, v in enumerate(st["views"]):
        m = 90.0
        x0, y0, x1, y1 = v["x0"] - m, v["bot"] - m, v["x1"] + m, v["top"] + m
        s = min(8.0, max_w / (x1 - x0))
        img = Image.new("RGB", (int((x1 - x0) * s) + 1, int((y1 - y0) * s) + 1), "white")
        dr = ImageDraw.Draw(img)
        to = lambda x, y: ((x - x0) * s, (y1 - y) * s)  # noqa: E731
        for e in doc["entities"]:
            lay = e.get("layer")
            if lay in skip:
                continue
            b = _bbox([e])
            if not b or b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
                continue
            if e["type"] in ("TEXT", "MTEXT"):
                dr.text(to(e.get(10, 0.0), e.get(20, 0.0) + 4 / s), _clean(e["text"]), fill=(20, 110, 40), font=font, anchor="ls")
                continue
            col, w = ((0, 0, 0), 3) if lay == roles["outline"] else ((30, 60, 200), 2) if lay in roles["holes"] else ((110, 150, 110), 1)
            for a, bb in dxf._segments(e):
                dr.line([*to(*a), *to(*bb)], fill=col, width=w)
        name = f"steel_view{k + 1}.png"
        img.save(os.path.join(folder, name))
        files.append(name)
        images.append(img)
    W = max(im.width for im in images)
    sheet = Image.new("RGB", (W, sum(im.height for im in images) + 20 * (len(images) - 1)), "white")
    y = 0
    for im in images:
        sheet.paste(im, (0, y))
        y += im.height + 20
    sheet.save(os.path.join(folder, "source_vector.png"))
    return files, "source_vector.png"


def _i_dims(st):
    prof = st["spec"]["profile"]
    tab = I_BEAMS.get(prof["name"])
    hs = sorted(v["top"] - v["bot"] for v in st["views"])
    h, b, s, t, r = tab if tab else (hs[0], hs[-1], 0, 0, 0)
    # the views are 1:1: their heights are the real h and b when close to the table
    for v in st["views"]:
        H = v["top"] - v["bot"]
        if abs(H - h) <= 0.03 * h and abs(H - h) <= abs(H - b):
            h = H
        elif abs(H - b) <= 0.03 * b:
            b = H
    if not tab:
        # no table entry: flange thickness from the inner lines of the web view, web as for similar sizes
        web = next((v for v in st["views"] if v["inner"]), None)
        t = (web["top"] - web["inner"][-1]) if web else round(h / 20, 1)
        s, r = round(max(4.0, h / 30), 1), round(h / 15)
    return h, b, s, t, r


def _web_view(v, h, b, t) -> bool:
    """A view of an I-beam showing the web (height h, flange inner lines at t from the edges)."""
    H = v["top"] - v["bot"]
    flange_lines = any(abs((v["top"] - y) - t) < 1.5 or abs((y - v["bot"]) - t) < 1.5 for y in v["inner"])
    if flange_lines:
        return True
    if abs(h - b) > 2:
        return abs(H - h) < abs(H - b)
    return False


def plan(st: dict, values: dict, answers: dict | None = None) -> dict:
    """Build operations from the (corrected) values, plus notes and questions for the review window."""
    answers = answers or {}
    sp, prof = st["spec"], st["spec"]["profile"]
    v = lambda k, d=None: float(values.get(k, d))  # noqa: E731
    ops, notes, questions, issues = [], [], [], []
    L = v("L")
    views = st["views"]
    if prof["kind"] == "I":
        h, b, s, t, r = v("h"), v("b"), v("s"), v("t"), v("r")
        section = [i_section(h, b, s, t, r)]
        W, Hh = b, h
        notes.append(f"Двутавр {prof['name']} ({prof['gost'] or 'ГОСТ не указан'}): h={h:g}, b={b:g}, s={s:g}, t={t:g}, r={r:g}; "
                     f"L={L:g} мм" + (f", сталь {sp['steel']}" if sp.get("steel") else ""))
        kinds = ["web" if _web_view(view, h, b, t) else "flange" for view in views]
        wall = t
    elif prof["kind"] == "tube":
        a, bb, t = v("a"), v("b"), v("t")
        outer, inner = tube_sections(a, bb, t)
        section = [outer, inner]
        W, Hh = a, bb
        notes.append(f"Профиль {prof['name']} {prof['gost']}: {a:g}×{bb:g}×{t:g}, L={L:g} мм"
                     + (f", сталь {sp['steel']}" if sp.get("steel") else ""))
        kinds = ["web" if k == 0 else "flange" for k in range(len(views))]
        wall = t
    else:
        t = v("t")
        main = max(views, key=lambda q: q["x1"] - q["x0"])
        x0, y0 = main["x0"], main["bot"]
        shapes = [{"type": "polyline", "closed": True, "points": [[round(x - x0, 3), round(y - y0, 3)] for x, y in main["polygon"]]}]
        for hole in main["holes"]:
            shapes.append(_hole_shape(hole, x0, y0))
        ops.append({"op": "extrude", "plane": "XY", "depth": t, "shapes": shapes})
        notes.append(f"Лист {t:g} мм{(' (' + prof['gost'] + ')') if prof['gost'] else ''}: контур {main['x1'] - main['x0']:g}×"
                     f"{main['top'] - main['bot']:g} мм по виду" + (f", сталь {sp['steel']}" if sp.get("steel") else ""))
        if main["holes"]:
            notes.append(_holes_note(main["holes"]))
        # the specification's mass is the blank's: holes are not subtracted
        return _finish(st, ops, notes, questions, issues, _area(main["polygon"]) * t)
    # rolled profile: section along X, then what each view cuts
    shapes = [{"type": "polyline", "closed": True, "points": [list(p) for p in loop]} for loop in section]
    ops.append({"op": "extrude", "plane": "YZ", "depth": L, "shapes": shapes})
    for view, kind in zip(views, kinds):
        x0 = view["x0"]
        span = view["x1"] - view["x0"]
        if abs(span - L) > 1:
            issues.append({"level": "warning", "dims": ["L"],
                           "text": f"Вид длиной {span:g} мм, а в спецификации L={L:g}: срезы и отверстия взяты от левого торца"})
        base = view["bot"] if kind == "web" else (view["bot"] + view["top"]) / 2
        poly = [[round(x - x0, 3), round(y - base, 3)] for x, y in view["polygon"]]
        full = _area(view["polygon"]) >= (view["x1"] - view["x0"]) * (view["top"] - view["bot"]) - 1.0
        if not full:
            # everything outside this view's outline is cut off through the part
            if kind == "web":   # XZ: (X, Z), the cut goes along +Y from outside the part
                frame = {"type": "rect", "x": -10, "y": -10, "w": L + 20, "h": Hh + 20}
                cut = {"op": "cut", "plane": "XZ", "offset": -W / 2 - 10, "through_all": True}
            else:               # XY: (X, Y), along +Z from below the part
                frame = {"type": "rect", "x": -10, "y": -W / 2 - 10, "w": L + 20, "h": W + 20}
                cut = {"op": "cut", "plane": "XY", "offset": -10, "through_all": True}
            ops.append({**cut, "shapes": [frame, {"type": "polyline", "closed": True, "points": poly}]})
            notes.append(_cut_note(view, kind))
        if view["holes"]:
            hs = [_hole_shape(hh, x0, base) for hh in view["holes"]]
            if kind == "web":
                ops.append({"op": "cut", "plane": "XZ", "offset": -W / 2 - 10, "through_all": True, "shapes": hs})
                notes.append(_holes_note(view["holes"]) + (" в стенке" if prof["kind"] == "I" else " насквозь через обе стенки"))
            else:
                qid = f"walls_{views.index(view)}"
                n_walls = int(answers.get(qid, 1))
                what = "полок" if prof["kind"] == "I" else "стенок"
                questions.append({"id": qid, "text": f"{_holes_note(view['holes'])} на виде сверху: через сколько {what} "
                                                     f"они проходят (1 — только верхняя, 2 — обе)?",
                                  "default": 1, "unit": "шт"})
                if n_walls >= 2:
                    ops.append({"op": "cut", "plane": "XY", "offset": -10, "through_all": True, "shapes": hs})
                else:
                    ops.append({"op": "cut", "plane": "XY", "offset": Hh, "depth": wall, "reverse": True, "shapes": hs})
                notes.append(_holes_note(view["holes"]) + (" в верхней полке" if prof["kind"] == "I" and n_walls < 2 else
                                                           " в обеих полках" if prof["kind"] == "I" else
                                                           " в верхней стенке" if n_walls < 2 else " через обе стенки"))
    area = _area(section[0]) - sum(_area(q) for q in section[1:])
    return _finish(st, ops, notes, questions, issues, area * L)


def _finish(st, ops, notes, questions, issues, volume_estimate):
    sp = st["spec"]
    m = re.search(r"(\d+)\s*отв\.?\s*Ø?\s*(\d+(?:[.,]\d+)?)", st.get("notes_text", ""))
    found = sum(len(v["holes"]) for v in st["views"])
    if m and int(m.group(1)) != sum(1 for v in st["views"] for hh in v["holes"] if hh["round"]):
        issues.append({"level": "warning", "dims": [],
                       "text": f"На чертеже указано {m.group(1)} отв. Ø{m.group(2)}, а найдено круглых отверстий "
                               f"{sum(1 for v in st['views'] for hh in v['holes'] if hh['round'])}"})
    m = re.search(r"(\d+)\s*паз", st.get("notes_text", ""))
    if m and int(m.group(1)) != sum(1 for v in st["views"] for hh in v["holes"] if not hh["round"]):
        issues.append({"level": "warning", "dims": [],
                       "text": f"На чертеже указано {m.group(1)} паз(а), а найдено "
                               f"{sum(1 for v in st['views'] for hh in v['holes'] if not hh['round'])}"})
    if sp.get("mass_kg"):
        est = volume_estimate * STEEL_DENSITY
        notes.append(f"Масса по сечению без вырезов {est:.1f} кг, в спецификации {sp['mass_kg']:g} кг")
        if abs(est - sp["mass_kg"]) > 0.05 * sp["mass_kg"]:
            issues.append({"level": "warning", "dims": [],
                           "text": f"Масса по сечению {est:.1f} кг заметно отличается от спецификации ({sp['mass_kg']:g} кг): "
                                   f"проверьте профиль и толщины"})
    if found == 0 and not any(o["op"] == "cut" for o in ops[1:]):
        notes.append("Срезов и отверстий на видах нет: деталь — профиль, отрезанный в размер")
    return {"operations": ops, "notes": notes, "questions": questions, "issues": issues}


def _hole_shape(hole, x0, base) -> dict:
    cx, cy = hole["center"]
    if hole["round"]:
        return {"type": "circle", "x": round(cx - x0, 3), "y": round(cy - base, 3), "d": round(hole["w"], 3)}
    return {"type": "polyline", "closed": True, "points": [[round(x - x0, 3), round(y - base, 3)] for x, y in hole["points"]]}


def _holes_note(holes) -> str:
    rnd = [hh for hh in holes if hh["round"]]
    slots = [hh for hh in holes if not hh["round"]]
    parts = []
    if rnd:
        ds = sorted({round(hh["w"], 1) for hh in rnd})
        parts.append(f"{len(rnd)} отв. " + ", ".join(f"Ø{d:g}" for d in ds))
    if slots:
        sz = sorted({(round(min(hh['w'], hh['h']), 1), round(max(hh['w'], hh['h']), 1)) for hh in slots})
        parts.append(f"{len(slots)} паз(а) " + ", ".join(f"{a:g}×{b:g}" for a, b in sz))
    return " и ".join(parts)


def _cut_note(view, kind) -> str:
    """'срез 45° на левом торце (195×195 мм)' for each end the outline does not reach in full."""
    poly, bot, top = view["polygon"], view["bot"], view["top"]
    H = top - bot
    ends = []
    for side, xe, sign in (("левом", view["x0"], 1), ("правом", view["x1"], -1)):
        near = [p[1] for p in poly if abs(p[0] - xe) < 1e-3]
        span = (max(near) - min(near)) if near else 0.0
        if span >= H - 0.5:
            continue
        # how far along the part the full top and bottom edges start
        reach = []
        for level in (bot, top):
            xs = [p[0] for p in poly if abs(p[1] - level) < 0.05]
            if xs:
                reach.append(abs((min(xs) if sign > 0 else max(xs)) - xe))
        dx = max(reach) if reach else 0.0
        dz = H - span
        ang = math.degrees(math.atan2(dz, dx)) if dx > 0.5 else 90.0
        ends.append(f"срез {ang:.0f}° на {side} торце ({round(dx * 2) / 2:g}×{round(dz * 2) / 2:g} мм)")
    where = "на виде стенки" if kind == "web" else "на виде сверху"
    return (", ".join(ends) if ends else "фигурный контур") + f" {where}"
