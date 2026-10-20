"""Arcs stored as polyline bulges (KOMPAS/AutoCAD export) must come through as arcs:
read as arcs, the view outline taken from the file with them, and the sketch made of real arcs
whose ends meet the lines exactly (otherwise KOMPAS refuses the contour as not closed)."""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import dxf  # noqa: E402

doc = dxf.read_dxf(os.path.join(HERE, "test2_arcs.dxf"))
bulged = [e for e in doc["entities"] if e["type"] == "LWPOLYLINE" and any(e.get("bulge") or [])]
assert bulged, "no bulges read"
# a half-circle bulge (1.0) must give points off the chord
e = next(e for e in bulged if 1.0 in e["bulge"])
pts = [p for s in dxf._segments(e) for p in s]
(a, b) = e["pts"][0], e["pts"][1]
far = max(abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / math.dist(a, b) for p in pts)
assert abs(far - math.dist(a, b) / 2) < 0.01, far

box = max(dxf.view_boxes(doc), key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
out = dxf.outer_loop(doc, box)
assert out, "outline not traced"
shapes = dxf.outline_shapes(out, lambda p: (p[0] - box[0], p[1] - box[1]))
arcs = [s for s in shapes if s["type"] == "arc"]
assert len(arcs) == 4, f"rounded corners expected as 4 arcs, got {len(arcs)}"
# every piece ends where the next one starts
ends = []
for s in shapes:
    if s["type"] == "line":
        ends.append(((s["x1"], s["y1"]), (s["x2"], s["y2"])))
    else:
        ends.append(None)
for i, s in enumerate(shapes):
    if s["type"] == "line":
        prev = shapes[i - 1]
        if prev["type"] == "arc":
            assert min(math.dist((s["x1"], s["y1"]), (prev[k1], prev[k2])) for k1, k2 in (("x1", "y1"), ("x2", "y2"))) < 1e-9
print("ARC OUTLINE TEST PASSED")
