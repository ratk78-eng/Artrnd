"""Depth map of a plate's front side from a STEP file: every planar face square to Y is drawn at its
height (seen from +Y, the higher face wins). Writes <name>_depth.npy (mm, 0.1 mm grid, rows = Z)."""
import math, re, sys
import numpy as np
from PIL import Image, ImageDraw

path = sys.argv[1]
axis = sys.argv[2] if len(sys.argv) > 2 else "y"   # the plate's thickness direction
sign = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0  # which way the front face looks
txt = open(path, encoding="latin-1").read()
txt = re.sub(r"\s+", " ", txt)
ents = {}
for chunk in txt.split(";"):
    m = re.match(r"\s*#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)\s*$", chunk)
    if m:
        ents[int(m.group(1))] = (m.group(2), m.group(3))


def args(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def ref(s):
    return int(s.strip()[1:])


def point(n):
    t, a = ents[n]
    v = re.findall(r"[-+0-9.Ee]+", a.split(",", 1)[1])
    return np.array([float(x) for x in v[:3]])


def direction(n):
    t, a = ents[n]
    v = re.findall(r"[-+0-9.Ee]+", a.split(",", 1)[1])
    return np.array([float(x) for x in v[:3]])


def placement(n):
    t, a = ents[n]
    p = args(a)
    loc = point(ref(p[1]))
    z = direction(ref(p[2])) if p[2] != "$" else np.array([0, 0, 1.0])
    x = direction(ref(p[3])) if len(p) > 3 and p[3] != "$" else np.array([1.0, 0, 0])
    return loc, z / np.linalg.norm(z), x / np.linalg.norm(x)


def vertex(n):
    t, a = ents[n]
    return point(ref(args(a)[1]))


def edge_points(oe):
    """Points along an ORIENTED_EDGE in its loop direction."""
    t, a = ents[oe]
    p = args(a)
    ec = ref(p[3])
    same = p[4].strip() == ".T."
    t2, a2 = ents[ec]
    q = args(a2)
    v0, v1 = vertex(ref(q[1])), vertex(ref(q[2]))
    curve = ref(q[3])
    sense = q[4].strip() == ".T."
    ct, ca = ents[curve]
    pts = [v0, v1]
    if ct == "CIRCLE":
        c = args(ca)
        loc, zz, xx = placement(ref(c[1]))
        r = float(c[2])
        yy = np.cross(zz, xx)
        ang = lambda v: math.atan2(np.dot(v - loc, yy), np.dot(v - loc, xx))  # noqa: E731
        a0, a1 = ang(v0), ang(v1)
        if sense:
            sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
        else:
            sweep = -((a0 - a1) % (2 * math.pi) or 2 * math.pi)
        n = max(2, int(abs(sweep) / math.radians(3)))
        pts = [loc + r * (math.cos(a0 + sweep * k / n) * xx + math.sin(a0 + sweep * k / n) * yy) for k in range(n + 1)]
    elif ct not in ("LINE",):
        pass  # other curves: straight between the ends (good enough for a map)
    return pts if same else pts[::-1]


def loop_points(lp):
    t, a = ents[lp]
    out = []
    for oe in re.findall(r"#(\d+)", a):
        out += edge_points(int(oe))[:-1]
    return out


faces = []
for n, (t, a) in ents.items():
    if t != "ADVANCED_FACE":
        continue
    p = args(a)
    surf = ref(p[2])
    if surf not in ents:
        continue
    st, sa = ents[surf]
    if st != "PLANE":
        continue
    loc, zz, _ = placement(ref(args(sa)[1]))
    k = "xyz".index(axis)
    if abs(abs(zz[k]) - 1) > 1e-6:
        continue
    loops = []
    for b in re.findall(r"#(\d+)", p[1]):
        bt, ba = ents[int(b)]
        lp = ref(args(ba)[1])
        loops.append((bt, loop_points(lp)))
    flip = 1.0 if p[3].strip() == ".T." else -1.0  # the face looks against its plane's normal
    faces.append((loc[k], zz[k] * flip, loops))

k = "xyz".index(axis)
u, v = [i for i in range(3) if i != k]
allp = np.array([pt for _, _, ls in faces for _, l in ls for pt in l])
mn, mx = allp.min(axis=0), allp.max(axis=0)
step = 0.1
W = int(round((mx[u] - mn[u]) / step)) + 1
H = int(round((mx[v] - mn[v]) / step)) + 1
top = mx[k] if sign > 0 else mn[k]
depth = np.full((H, W), np.nan)
# faces looking towards the front: the one nearest the front wins, so it is drawn last
for lvl, nz, loops in sorted(faces, key=lambda f: f[0] * sign):
    if nz * sign < 0:
        continue
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    def area(l):
        return abs(sum(a[u] * b[v] - b[u] * a[v] for a, b in zip(l, l[1:] + l[:1]))) / 2
    loops = sorted(loops, key=lambda bl: -area(bl[1]))  # the outer loop is the biggest
    for n_, (bt, l) in enumerate(loops):
        poly = [((pt[u] - mn[u]) / step, (mx[v] - pt[v]) / step) for pt in l]
        if len(poly) >= 3:
            d.polygon(poly, fill=255 if n_ == 0 else 0)
    m = np.array(img) > 0
    depth[m] = abs(top - lvl)
np.save(path.rsplit(".", 1)[0] + "_depth.npy", depth)
print("faces", len(faces), "grid", W, H, "u", "xyz"[u], mn[u], mx[u], "v", "xyz"[v], mn[v], mx[v], "top", top)
vals, cnt = np.unique(np.round(depth[~np.isnan(depth)], 3), return_counts=True)
print("depth levels (mm: area mm2):", {float(a): round(float(c) * step * step, 1) for a, c in zip(vals, cnt)})
print("unknown area", round(float(np.isnan(depth).sum()) * step * step, 1))
