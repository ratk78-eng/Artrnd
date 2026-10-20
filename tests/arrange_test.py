"""Exact areas (ui/arrange.py): the drawing's own lines and arcs walked round. A 40×20 plate with
R5 corners, a Ø8 hole, a ring between R6 and R6.5 round it and a line stopping 0.03 short of the
outline (PDF exports leave such gaps); a spur and a bridge must not end up in any boundary."""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import arrange  # noqa: E402
import dxf  # noqa: E402


def line(a, b):
    return {"type": "LINE", 10: a[0], 20: a[1], 11: b[0], 21: b[1], "kind": "solid"}


def arc(c, r, a0, a1):
    return {"type": "ARC", 10: c[0], 20: c[1], 40: r, 50: a0, 51: a1, "kind": "solid"}


def circle(c, r):
    return {"type": "CIRCLE", 10: c[0], 20: c[1], 40: r, "kind": "solid"}


ents = [line((5, 0), (35, 0)), arc((35, 5), 5, 270, 360), line((40, 5), (40, 15)), arc((35, 15), 5, 0, 90),
        line((35, 20), (5, 20)), arc((5, 15), 5, 90, 180), line((0, 15), (0, 5)), arc((5, 5), 5, 180, 270),
        circle((20, 10), 4), circle((20, 10), 6), circle((20, 10), 6.5),
        line((30, 0.03), (30, 20)),                     # a partition stopping just short of the bottom
        line((33, 10), (36, 10)),                       # a spur: one end loose
        line((1, 10), (8, 10)), circle((9, 10), 1)]     # a bridge from the outline to a small loop
doc = {"entities": ents}
cycles = arrange.build(doc, (0, 0, 40, 20))
pos = sorted(c["area"] for c in cycles if c["area"] > 0)
plate = 40 * 20 - (4 - math.pi) * 25
# areas by their outer boundary (what lies inside them is a separate area): the three circles, the
# loop the bridge led to, the right strip and the left part
right = 10 * 20 - (4 - math.pi) * 25 / 2
expect = sorted([math.pi * 16, math.pi * 36, math.pi * 6.5 ** 2, math.pi, right, plate - right])
assert len(pos) == len(expect), (pos, expect)
for a, b in zip(pos, expect):
    assert abs(a - b) < 0.02 * b + 0.05, (pos, expect)
for c in cycles:
    keys = [(round(s["a"][0], 6), round(s["a"][1], 6), round(s["b"][0], 6), round(s["b"][1], 6))
            for s in c["segs"] if s["type"] == "line"]   # (a circle is two arcs between the same points)
    rev = {(k[2], k[3], k[0], k[1]) for k in keys}
    assert not (set(keys) & rev), "a piece walked along both of its sides"
outer = arrange.outer(cycles)
assert outer and abs(outer["area"] - plate) < 0.5, outer and outer["area"]
arcs = [s for s in outer["segs"] if s["type"] == "arc"]
assert len(arcs) == 4 and all(abs(s["r"] - 5) < 1e-9 for s in arcs), arcs     # true R5 corners
# sketch primitives: every arc's ends lie exactly on its circle and the chain closes
sh = dxf.outline_shapes(arrange.outline(outer), lambda p: (p[0], p[1]))
for s in sh:
    if s["type"] == "arc":
        for x, y in ((s["x1"], s["y1"]), (s["x2"], s["y2"])):
            assert abs(math.hypot(x - s["x"], y - s["y"]) - s["r"]) < 1e-9
ends = [(round(s[k], 9), round(s[k.replace("x", "y")], 9)) for s in sh for k in ("x1", "x2")]
assert all(ends.count(p) == 2 for p in ends), "the outline does not close exactly"
print("ARRANGE TEST PASSED")
