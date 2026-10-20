"""Writes task №13 as a vector DXF (front view + top view, hidden lines with the HIDDEN linetype,
dimension values as text) — a stand-in for a drawing exported from KOMPAS/AutoCAD."""
import sys

out = sys.argv[1]
ents = []


def line(x1, y1, x2, y2, lt=None):
    ents.append(("LINE", lt, {10: x1, 20: y1, 11: x2, 21: y2}))


def circle(x, y, r, lt=None):
    ents.append(("CIRCLE", lt, {10: x, 20: y, 40: r}))


def arc(x, y, r, a0, a1):
    ents.append(("ARC", None, {10: x, 20: y, 40: r, 50: a0, 51: a1}))


def text(x, y, s):
    ents.append(("TEXT", None, {10: x, 20: y, 40: 3.5, 1: s}))


def poly(pts, dy=0):
    for (a, b), (c, d) in zip(pts, pts[1:] + pts[:1]):
        line(a, b + dy, c, d + dy)


# front view: X right, Z up, at the sheet origin
poly([(0, 0), (29, 0), (29, 15), (71, 15), (71, 0), (100, 0), (100, 20), (75, 20), (75, 45), (68, 45),
      (68, 35), (32, 35), (32, 45), (25, 45), (25, 20), (0, 20)])
H = "HIDDEN"
for x in (40, 60):
    line(x, 15, x, 35, H)                    # Ø20 (25..35) and the square under it (15..25)
line(40, 25, 60, 25, H)
for x0, x1, xr in ((0, 21, 21), (79, 100, 79)):
    line(x0, 5, x1, 5, H)                    # floor of the U-recess
    line(xr, 5, xr, 20, H)
for x in (4, 14, 86, 96):
    line(x, 0, x, 5, H)                      # Ø10 through the 5 mm floor
# top view below it (first-angle): X right, Y up, Y = 0 at the bottom
dy = -80
poly([(0, 0), (100, 0), (100, 50), (0, 50)], dy)
for x in (25, 32, 68, 75):
    line(x, dy, x, dy + 50)
circle(50, dy + 25, 10)
poly([(60, 25), (50, 35), (40, 25), (50, 15)], dy)
for cx, end, a0, a1 in ((9, 0, 270, 90), (91, 100, 90, 270)):
    circle(cx, dy + 25, 5)
    arc(cx, dy + 25, 12, a0, a1)
    line(cx, dy + 13, end, dy + 13)
    line(cx, dy + 37, end, dy + 37)
# dimension values, as KOMPAS writes them when dimensions are exploded into text
for x, y, s in ((50, -8, "100"), (108, 22, "45"), (50, 50, "36"), (20, 40, "10"), (50, 8, "42"),
                (35, 7, "15"), (104, 10, "20"), (106, dy + 25, "50"), (50, dy - 8, "82"), (50, dy + 38, "%%c20"),
                (95, dy + 5, "%%c10"), (5, dy + 45, "R12")):
    text(x, y, s)

with open(out, "w", encoding="utf-8") as f:
    w = lambda c, v: f.write(f"{c}\n{v}\n")  # noqa: E731
    w(0, "SECTION"); w(2, "TABLES"); w(0, "TABLE"); w(2, "LAYER")
    w(0, "LAYER"); w(2, "0"); w(6, "CONTINUOUS"); w(0, "ENDTAB"); w(0, "ENDSEC")
    w(0, "SECTION"); w(2, "ENTITIES")
    for kind, lt, props in ents:
        w(0, kind)
        w(8, "0")
        if lt:
            w(6, lt)
        for k, v in props.items():
            w(k, v)
    w(0, "ENDSEC"); w(0, "EOF")
print("written", out, len(ents), "entities")
