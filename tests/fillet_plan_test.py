"""Radii and fillets as the drawing means them (offline):
- an arc drawn R5.02 between two lines, a hair off tangent, comes out R5 (the drawing's dimension)
  and exactly tangent to both lines, the chain still closing exactly;
- the rounded corners of an outline that is cut around become sharp corners plus fillet edges."""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402
import arrange  # noqa: E402
import dxf  # noqa: E402

# a corner: line along X to (35, 0), arc R≈5.02 round (35, 5.02) up to (40.02, 5.02), line up
segs = [{"type": "line", "a": (0, 0), "b": (35, 0)},
        {"type": "arc", "c": (35, 5.02), "r": 5.02, "a": (35, 0), "b": (40.02, 5.02), "ccw": True},
        {"type": "line", "a": (40.02, 5.02), "b": (40.02, 20)},
        {"type": "line", "a": (40.02, 20), "b": (0, 20)},
        {"type": "line", "a": (0, 20), "b": (0, 0)}]
dxf.SNAP_RADII, dxf.SNAPPED = [5.0], {}
sh = dxf.outline_shapes({"points": [], "arcs": [], "segs": segs}, lambda p: (p[0], p[1]))
arc = next(s for s in sh if s["type"] == "arc")
assert arc["r"] == 5.0, arc
for x, y in ((arc["x1"], arc["y1"]), (arc["x2"], arc["y2"])):
    assert abs(math.hypot(x - arc["x"], y - arc["y"]) - 5.0) < 1e-9
# tangent: the centre is exactly R from both lines
assert abs(arc["y"] - 5.0) < 1e-9 and abs(40.02 - arc["x"] - 5.0) < 1e-9, arc
ends = [(round(s[k], 9), round(s[k.replace("x", "y")], 9)) for s in sh for k in ("x1", "x2")]
assert all(ends.count(p) == 2 for p in ends), "the chain does not close"
assert dxf.SNAPPED == {"касание": 1, 5.0: 1}, dxf.SNAPPED

# corner fillets of a rounded 40×20 outline (R5 corners)
ents = [{"type": "LINE", 10: 5, 20: 0, 11: 35, 21: 0, "kind": "solid"},
        {"type": "ARC", 10: 35, 20: 5, 40: 5, 50: 270, 51: 360, "kind": "solid"},
        {"type": "LINE", 10: 40, 20: 5, 11: 40, 21: 15, "kind": "solid"},
        {"type": "ARC", 10: 35, 20: 15, 40: 5, 50: 0, 51: 90, "kind": "solid"},
        {"type": "LINE", 10: 35, 20: 20, 11: 5, 21: 20, "kind": "solid"},
        {"type": "ARC", 10: 5, 20: 15, 40: 5, 50: 90, 51: 180, "kind": "solid"},
        {"type": "LINE", 10: 0, 20: 15, 11: 0, 21: 5, "kind": "solid"},
        {"type": "ARC", 10: 5, 20: 5, 40: 5, 50: 180, 51: 270, "kind": "solid"}]
outer = arrange.outer(arrange.build({"entities": ents}, (0, 0, 40, 20)))
o2, corners = app._corner_fillets(arrange.outline(outer))
assert sorted((round(p[0], 6), round(p[1], 6)) for p, _ in corners) == [(0, 0), (0, 20), (40, 0), (40, 20)], corners
assert all(abs(r - 5) < 1e-9 for _, r in corners)
assert all(s["type"] == "line" for s in o2["segs"]) and len(o2["segs"]) == 4, o2["segs"]
print("FILLET PLAN TEST PASSED")
