"""Exact areas of a vector view: the drawing's own lines and arcs cut at every crossing and walked
round, so an area's boundary is the very lines and arcs that bound it (true radii, ends meeting
exactly), not a traced picture of them snapped back onto nearby points.

The picture-based areas (dxf.faces) stay the authority on *which* areas there are — they tolerate
lines that only nearly meet — and this module supplies the boundary of each when the file's lines
close it: exact(face) finds the walked area holding a point inside the traced one."""
from __future__ import annotations

import math

# ends this close are one point: PDF and DXF exports leave gaps of up to a tenth or so, and the
# traced areas (3 px strokes at 20 px/mm) close gaps up to 0.15 mm — the same tolerance here makes the
# exact areas agree with the traced ones (0.05 left a groove's turn on the plate open, 0.16 closes it)
TOL = 0.16


def _prims(doc: dict, box: tuple) -> list:
    """("L", a, b) and ("A", cx, cy, r, t0, t1) (counter-clockwise, radians) for the view's main lines."""
    out = []
    x0, y0, x1, y1 = box[0] - 0.1, box[1] - 0.1, box[2] + 0.1, box[3] + 0.1
    inside = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1  # noqa: E731

    def arc(cx, cy, r, t0, t1):
        if r <= 1e-6 or t1 - t0 <= 1e-9:
            return
        pts = [(cx + r * math.cos(t0 + (t1 - t0) * k / 8), cy + r * math.sin(t0 + (t1 - t0) * k / 8)) for k in range(9)]
        if all(map(inside, pts)):
            out.append(("A", cx, cy, r, t0, t1))
    for e in doc["entities"]:
        if e["kind"] != "solid" or e.get("from_dimension"):
            continue
        t = e["type"]
        if t == "LINE":
            a, b = (e.get(10, 0.0), e.get(20, 0.0)), (e.get(11, 0.0), e.get(21, 0.0))
            if inside(a) and inside(b) and math.dist(a, b) > 1e-6:
                out.append(("L", a, b))
        elif t == "CIRCLE":
            arc(e.get(10, 0.0), e.get(20, 0.0), e.get(40, 0.0), 0.0, 2 * math.pi)
        elif t == "ARC":
            t0, t1 = math.radians(e.get(50, 0.0)), math.radians(e.get(51, 0.0))
            while t1 <= t0:
                t1 += 2 * math.pi
            arc(e.get(10, 0.0), e.get(20, 0.0), e.get(40, 0.0), t0, t1)
        elif t == "LWPOLYLINE":
            raw = e["pts"]
            bulges = e.get("bulge") or [0.0] * len(raw)
            verts = [(tuple(q), bulges[i] if i < len(bulges) else 0.0) for i, q in enumerate(raw) if q[1] is not None]
            pairs = list(zip(verts, verts[1:]))
            if e.get("closed") and len(verts) > 2:
                pairs.append((verts[-1], verts[0]))
            for (a, bulge), (b, _) in pairs:
                if math.dist(a, b) < 1e-9:
                    continue
                if abs(bulge) < 1e-9:
                    if inside(a) and inside(b):
                        out.append(("L", a, b))
                    continue
                dx, dy = b[0] - a[0], b[1] - a[1]
                c = math.hypot(dx, dy)
                sweep = 4 * math.atan(bulge)
                off = c / 2 * (1 - bulge * bulge) / (2 * bulge)
                cx, cy = (a[0] + b[0]) / 2 - dy / c * off, (a[1] + b[1]) / 2 + dx / c * off
                r = math.hypot(a[0] - cx, a[1] - cy)
                ta = math.atan2(a[1] - cy, a[0] - cx)
                if sweep > 0:
                    arc(cx, cy, r, ta, ta + sweep)
                else:
                    tb = math.atan2(b[1] - cy, b[0] - cx)
                    arc(cx, cy, r, tb, tb - sweep)
    return _join_collinear(out)


# pieces of one straight edge with gaps up to this become one line. Only pieces that touch or overlap:
# bridging real gaps (0.6 tried) closed openings the traced areas keep open and split them
GAP = 0.0


def _join_collinear(prims: list) -> list:
    """SOLIDWORKS draws some edges as a string of short pieces on one line, often twice over: those
    become one line. Lines on one carrier are found by direction and distance from the origin."""
    lines = [p for p in prims if p[0] == "L"]
    rest = [p for p in prims if p[0] != "L"]
    groups = {}
    for p in lines:
        (ax, ay), (bx, by) = p[1], p[2]
        L = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / L, (by - ay) / L
        if ux < -1e-12 or (abs(ux) <= 1e-12 and uy < 0):
            ux, uy = -ux, -uy
        off = ax * uy - ay * ux                   # signed distance of the carrier from the origin
        key = (round(math.degrees(math.atan2(uy, ux)) / 0.01), round(off / 0.005))
        groups.setdefault(key, []).append((p, ux, uy))
    out = []
    for items in groups.values():
        if len(items) == 1:
            out.append(items[0][0])
            continue
        _, ux, uy = items[0]
        spans = []
        for p, _, _ in items:
            t1 = p[1][0] * ux + p[1][1] * uy
            t2 = p[2][0] * ux + p[2][1] * uy
            spans.append((min(t1, t2), max(t1, t2), p))
        spans.sort(key=lambda s: s[0])
        cur = [spans[0]]
        for sp in spans[1:]:
            if sp[0] - max(c[1] for c in cur) <= GAP:
                cur.append(sp)
                continue
            out.append(_merged(cur, ux, uy))
            cur = [sp]
        out.append(_merged(cur, ux, uy))
    return out + rest


def _merged(spans: list, ux: float, uy: float):
    if len(spans) == 1:
        return spans[0][2]
    lo = min(spans, key=lambda s: s[0])
    hi = max(spans, key=lambda s: s[1])
    a = lo[2][1] if abs(lo[2][1][0] * ux + lo[2][1][1] * uy - lo[0]) < 1e-9 else lo[2][2]
    b = hi[2][1] if abs(hi[2][1][0] * ux + hi[2][1][1] * uy - hi[1]) < 1e-9 else hi[2][2]
    return ("L", a, b)


def _at(p, s):
    """Point of primitive p at parameter s (0…1 along a line, an angle on an arc)."""
    if p[0] == "L":
        (ax, ay), (bx, by) = p[1], p[2]
        return ax + (bx - ax) * s, ay + (by - ay) * s
    return p[1] + p[3] * math.cos(s), p[2] + p[3] * math.sin(s)


def _range(p):
    return (0.0, 1.0) if p[0] == "L" else (p[4], p[5])


def _bbox(p):
    if p[0] == "L":
        (ax, ay), (bx, by) = p[1], p[2]
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
    return p[1] - p[3], p[2] - p[3], p[1] + p[3], p[2] + p[3]


def _param_on(p, q, tol):
    """Parameter of the point of p nearest to q, when q is within tol of p (else None)."""
    if p[0] == "L":
        (ax, ay), (bx, by) = p[1], p[2]
        L2 = (bx - ax) ** 2 + (by - ay) ** 2
        s = ((q[0] - ax) * (bx - ax) + (q[1] - ay) * (by - ay)) / L2
        s = max(0.0, min(1.0, s))
    else:
        _, cx, cy, r, t0, t1 = p
        a = math.atan2(q[1] - cy, q[0] - cx)
        while a < t0 - 1e-12:
            a += 2 * math.pi
        while a > t1 + 1e-12 and a - 2 * math.pi >= t0 - 1e-12:
            a -= 2 * math.pi
        if a > t1:   # beyond the arc: its nearer end
            a = t0 if math.dist(q, _at(p, t0)) < math.dist(q, _at(p, t1)) else t1
        s = a
    return s if math.dist(_at(p, s), q) <= tol else None


def _crossings(p, q):
    """Points where two primitives cross (exactly, before the tolerance pass)."""
    out = []
    if p[0] == "L" and q[0] == "L":
        (ax, ay), (bx, by) = p[1], p[2]
        (cx, cy), (dx, dy) = q[1], q[2]
        den = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
        if abs(den) > 1e-12:
            s = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / den
            u = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / den
            if -1e-9 <= s <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
                out.append((ax + (bx - ax) * s, ay + (by - ay) * s))
        return out
    if p[0] == "A" and q[0] == "L":
        p, q = q, p
    if p[0] == "L":
        (ax, ay), (bx, by) = p[1], p[2]
        _, cx, cy, r, _, _ = q
        dx, dy = bx - ax, by - ay
        fx, fy = ax - cx, ay - cy
        A = dx * dx + dy * dy
        B = 2 * (fx * dx + fy * dy)
        C = fx * fx + fy * fy - r * r
        disc = B * B - 4 * A * C
        if disc < 0:
            # a line just touching the circle (a tangent drawn a hair off): its nearest point
            s = -B / (2 * A)
            m = (ax + dx * s, ay + dy * s)
            if 0 <= s <= 1 and abs(math.dist(m, (cx, cy)) - r) <= TOL / 2:
                out.append(m)
            return out
        for s in ((-B - math.sqrt(disc)) / (2 * A), (-B + math.sqrt(disc)) / (2 * A)):
            if -1e-9 <= s <= 1 + 1e-9:
                out.append((ax + dx * s, ay + dy * s))
        return out
    _, x1, y1, r1, _, _ = p
    _, x2, y2, r2, _, _ = q
    d = math.hypot(x2 - x1, y2 - y1)
    if d < 1e-9:
        return out
    if d > r1 + r2 or d < abs(r1 - r2):
        if abs(d - (r1 + r2)) <= TOL / 2 or abs(d - abs(r1 - r2)) <= TOL / 2:
            # touching circles: the touching point on the line of centres
            sgn = 1 if abs(d - (r1 + r2)) <= TOL / 2 or r1 > r2 else -1
            out.append((x1 + sgn * (x2 - x1) / d * r1, y1 + sgn * (y2 - y1) / d * r1))
        return out
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, r1 * r1 - a * a))
    mx, my = x1 + a * (x2 - x1) / d, y1 + a * (y2 - y1) / d
    out += [(mx + h * (y2 - y1) / d, my - h * (x2 - x1) / d), (mx - h * (y2 - y1) / d, my + h * (x2 - x1) / d)]
    return out


def _length(p, s0, s1):
    if p[0] == "L":
        return math.dist(p[1], p[2]) * (s1 - s0)
    return p[3] * (s1 - s0)


def build(doc: dict, box: tuple) -> list[dict]:
    """The bounded areas the view's main lines enclose: [{"segs", "area", "poly"}], each boundary
    counter-clockwise. segs: {"type": "line", "a", "b"} / {"type": "arc", "c", "r", "a", "b", "ccw"}."""
    prims = _prims(doc, box)
    if len(prims) > 3000:
        return []
    cuts = [[*_range(p)] for p in prims]
    boxes = [_bbox(p) for p in prims]
    ends = [[_at(p, s) for s in _range(p)] for p in prims]
    for i, p in enumerate(prims):
        if p[0] == "A" and p[5] - p[4] > math.pi:      # no arc piece goes half way round or more
            cuts[i].append((p[4] + p[5]) / 2)
        bi = boxes[i]
        for j in range(i + 1, len(prims)):
            bj = boxes[j]
            if bi[0] > bj[2] + TOL or bj[0] > bi[2] + TOL or bi[1] > bj[3] + TOL or bj[1] > bi[3] + TOL:
                continue
            q = prims[j]
            for x in _crossings(p, q):
                for k, (pp, other) in enumerate(((p, i), (q, j))):
                    s = _param_on(pp, x, TOL)
                    if s is not None:
                        cuts[other].append(s)
            # an end that stops just short of (or a hair past) the other line meets it there
            for (a, b, ia, ib) in ((p, q, i, j), (q, p, j, i)):
                for e in ends[ia]:
                    s = _param_on(b, e, TOL)
                    if s is not None:
                        cuts[ib].append(s)
    # the points where pieces meet, merged within TOL
    verts, grid = [], {}

    def vid(pt):
        gx, gy = int(math.floor(pt[0] / TOL)), int(math.floor(pt[1] / TOL))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k in grid.get((gx + dx, gy + dy), ()):
                    if math.dist(verts[k], pt) <= TOL:
                        return k
        verts.append(pt)
        grid.setdefault((gx, gy), []).append(len(verts) - 1)
        return len(verts) - 1
    edges, seen = [], set()
    for i, p in enumerate(prims):
        ss = sorted(set(round(s, 12) for s in cuts[i]))
        lo, hi = _range(p)
        ss = [s for s in ss if lo - 1e-12 <= s <= hi + 1e-12]
        for s0, s1 in zip(ss, ss[1:]):
            if _length(p, s0, s1) < 1e-4:
                continue
            u, v = vid(_at(p, s0)), vid(_at(p, s1))
            if u == v:
                continue
            mid = _at(p, (s0 + s1) / 2)
            key = (min(u, v), max(u, v), round(mid[0], 2), round(mid[1], 2))
            if key in seen:              # the same line drawn twice
                continue
            seen.add(key)
            edges.append({"p": p, "s0": s0, "s1": s1, "u": u, "v": v})
    # pieces with a loose end bound nothing, nor does a bridge (an area walked along both of its
    # sides): both go, until every piece has an area on each side
    while True:
        edges = _drop_loose(edges)
        walked = _walk(edges, verts)
        bridges = {k for cyc in walked for k, f in cyc if (k, not f) in set(cyc)}
        if not bridges:
            break
        edges = [e for k, e in enumerate(edges) if k not in bridges]
    cycles = []
    for cyc in walked:
        segs, poly = [], []
        for k, f in cyc:
            e = edges[k]
            p = e["p"]
            a, b = (verts[e["u"]], verts[e["v"]]) if f else (verts[e["v"]], verts[e["u"]])
            if p[0] == "L":
                segs.append({"type": "line", "a": a, "b": b})
                poly.append(a)
            else:
                s0, s1 = (e["s0"], e["s1"]) if f else (e["s1"], e["s0"])
                # an arc keeps its own ends on its circle (the merged point can be off it by up to
                # TOL, which can turn a short arc's sweep the long way round); the lines next to
                # it are brought to these ends when the sketch is made
                a, b = _at(p, s0), _at(p, s1)
                segs.append({"type": "arc", "c": (p[1], p[2]), "r": p[3], "a": a, "b": b, "ccw": f})
                n = max(2, int(abs(s1 - s0) / math.radians(5)) + 1)
                poly.append(a)
                poly += [_at(p, s0 + (s1 - s0) * t / n) for t in range(1, n)]
        area = sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
                   for i in range(len(poly))) / 2
        cycles.append({"segs": _tidy(segs), "area": area, "poly": poly})
    return cycles


def _drop_loose(edges: list) -> list:
    while True:
        deg = {}
        for e in edges:
            deg[e["u"]] = deg.get(e["u"], 0) + 1
            deg[e["v"]] = deg.get(e["v"], 0) + 1
        keep = [e for e in edges if deg[e["u"]] > 1 and deg[e["v"]] > 1]
        if len(keep) == len(edges):
            return edges
        edges = keep


def _walk(edges: list, verts: list) -> list:
    """Closed walks of half-edges (edge index, forward), each keeping its area on the left."""
    out_at = {}
    for k, e in enumerate(edges):
        for fwd in (True, False):
            v0 = e["u"] if fwd else e["v"]
            out_at.setdefault(v0, []).append((_leave_angle(e, fwd, verts), k, fwd))
    for v in out_at:
        out_at[v].sort()
    pos = {(k, f): n for v, hs in out_at.items() for n, (_, k, f) in enumerate(hs)}
    used, walks = set(), []
    for start in pos:
        if start in used:
            continue
        cyc, h = [], start
        for _ in range(len(pos) + 1):
            if h in used:
                break
            used.add(h)
            cyc.append(h)
            k, f = h
            e = edges[k]
            v_end = e["v"] if f else e["u"]
            n = pos[(k, not f)]                         # the twin leaves from v_end
            hs = out_at[v_end]
            _, k2, f2 = hs[(n - 1) % len(hs)]          # the next one clockwise from the twin
            h = (k2, f2)
        if h == start:
            walks.append(cyc)
    return walks


def _leave_angle(e, fwd, verts):
    p = e["p"]
    s0, s1 = (e["s0"], e["s1"]) if fwd else (e["s1"], e["s0"])
    a = verts[e["u"] if fwd else e["v"]]
    L = _length(p, min(s0, s1), max(s0, s1))
    d = min(0.05, L * 0.3)
    if p[0] == "L":
        t = s0 + (s1 - s0) * (d / L)
    else:
        t = s0 + math.copysign(d / p[3], s1 - s0)
    q = _at(p, t)
    return math.atan2(q[1] - a[1], q[0] - a[0])


def _tidy(segs: list) -> list:
    """One line where the drawing has a line in pieces, one arc where an arc was cut at crossings."""
    out = []
    for s in segs:
        if out:
            t = out[-1]
            if s["type"] == t["type"] == "line":
                (ax, ay), (bx, by) = t["a"], s["b"]
                L = math.hypot(bx - ax, by - ay)
                mx, my = t["b"]
                if L > 1e-9 and abs((by - ay) * (mx - ax) - (bx - ax) * (my - ay)) / L < 1e-4:
                    t["b"] = s["b"]
                    continue
            if (s["type"] == t["type"] == "arc" and s["ccw"] == t["ccw"] and math.dist(s["c"], t["c"]) < 1e-6
                    and abs(s["r"] - t["r"]) < 1e-6 and _sweep(t) + _sweep(s) < 2 * math.pi - 0.2):
                t["b"] = s["b"]
                continue
        out.append(dict(s))
    return out


def sample(s: dict, n: int) -> list:
    """n + 1 points along a piece (ends included)."""
    if s["type"] == "line":
        (ax, ay), (bx, by) = s["a"], s["b"]
        return [(ax + (bx - ax) * k / n, ay + (by - ay) * k / n) for k in range(n + 1)]
    cx, cy = s["c"]
    a0 = math.atan2(s["a"][1] - cy, s["a"][0] - cx)
    sw = _sweep(s) * (1 if s["ccw"] else -1)
    return [(cx + s["r"] * math.cos(a0 + sw * k / n), cy + s["r"] * math.sin(a0 + sw * k / n)) for k in range(n + 1)]


def reverse(s: dict) -> dict:
    """The same piece walked the other way."""
    t = dict(s, a=s["b"], b=s["a"])
    if s["type"] == "arc":
        t["ccw"] = not s["ccw"]
    return t


def map_segs(segs: list, fn) -> list:
    """Pieces carried through a map that keeps circles circles (shift, one scale, possibly mirrored)."""
    o, ux, uy = fn((0.0, 0.0)), fn((1.0, 0.0)), fn((0.0, 1.0))
    mirrored = (ux[0] - o[0]) * (uy[1] - o[1]) - (ux[1] - o[1]) * (uy[0] - o[0]) < 0
    scale = (math.dist(o, ux) + math.dist(o, uy)) / 2
    out = []
    for s in segs:
        t = dict(s, a=fn(s["a"]), b=fn(s["b"]))
        if s["type"] == "arc":
            t.update(c=fn(s["c"]), r=s["r"] * scale, ccw=s["ccw"] != mirrored)
        out.append(t)
    return out


def _sweep(s):
    a = math.atan2(s["a"][1] - s["c"][1], s["a"][0] - s["c"][0])
    b = math.atan2(s["b"][1] - s["c"][1], s["b"][0] - s["c"][0])
    return (b - a) % (2 * math.pi) if s["ccw"] else (a - b) % (2 * math.pi)


def _inside(pt, poly) -> bool:
    x, y = pt
    c = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def area_of(pts) -> float:
    return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                   for i in range(len(pts)))) / 2


def match(cycles: list, point, traced: list) -> dict | None:
    """The smallest bounded area holding `point` whose outline agrees with the traced one (same box
    within 0.3 mm, area within the stroke width along the edge)."""
    best = None
    for c in cycles:
        if c["area"] <= 0 or (best and c["area"] >= best["area"]) or not _inside(point, c["poly"]):
            continue
        best = c
    if not best:
        return None
    xs, ys = [p[0] for p in traced], [p[1] for p in traced]
    bx, by = [p[0] for p in best["poly"]], [p[1] for p in best["poly"]]
    if max(abs(min(xs) - min(bx)), abs(max(xs) - max(bx)), abs(min(ys) - min(by)), abs(max(ys) - max(by))) > 0.3:
        return None
    per = sum(math.dist(traced[i], traced[(i + 1) % len(traced)]) for i in range(len(traced)))
    if abs(area_of(traced) - best["area"]) > 0.15 * per + 0.5:
        return None
    return best


def outline(cycle: dict) -> dict:
    """The area as an outline in the format of dxf.faces / outer_loop: points (arcs sampled every 5°)
    with the arc each belongs to, plus the exact pieces ("segs")."""
    pts, tags = [], []
    segs = cycle["segs"]
    tag = lambda s: [round(s["c"][0], 6), round(s["c"][1], 6), round(s["r"], 6)] if s["type"] == "arc" else None  # noqa: E731
    for i, s in enumerate(segs):
        # the start point belongs to this piece and to the one ending there
        own = [t for t in (tag(segs[i - 1]), tag(s)) if t]
        pts.append(s["a"])
        tags.append([list(t) for t in {tuple(t) for t in own}])
        if s["type"] == "arc":
            cx, cy = s["c"]
            sw = _sweep(s) * (1 if s["ccw"] else -1)
            a0 = math.atan2(s["a"][1] - cy, s["a"][0] - cx)
            n = max(2, int(abs(sw) / math.radians(5)) + 1)
            for t in range(1, n):
                pts.append((cx + s["r"] * math.cos(a0 + sw * t / n), cy + s["r"] * math.sin(a0 + sw * t / n)))
                tags.append([tag(s)])
    return {"points": [(round(x, 4), round(y, 4)) for x, y in pts], "arcs": tags,
            "segs": [dict(s) for s in cycle["segs"]]}


def outer(cycles: list) -> dict | None:
    """The boundary of the largest connected drawing (the view's outline), counter-clockwise."""
    neg = [c for c in cycles if c["area"] < 0]
    if not neg:
        return None
    c = min(neg, key=lambda c: c["area"])
    segs = []
    for s in reversed(c["segs"]):
        t = dict(s, a=s["b"], b=s["a"])
        if t["type"] == "arc":
            t["ccw"] = not s["ccw"]
        segs.append(t)
    return {"segs": _tidy(segs), "area": -c["area"], "poly": list(reversed(c["poly"]))}
