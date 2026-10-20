"""Shows how sketch (x, y) maps to model axes on each default plane: a 10x10 square at (10..20, 30..40)
is extruded 1 mm and the body's center of mass is printed."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pythoncom  # noqa: E402

pythoncom.CoInitialize()
from kompas_api import Kompas  # noqa: E402

k = Kompas()
k.connect()
expected = {"XY": [15, 35, 0.5], "XZ": [15, 0.5, 35], "YZ": [0.5, 15, 35]}
for plane in ("XY", "XZ", "YZ"):
    k.new_part("axes " + plane)
    sid = k.sketch(plane, [{"type": "rect", "x": 10, "y": 30, "w": 10, "h": 10}], offset=0)
    k.extrude(sid, 1)
    c = k.mass_properties()["center_mm"]
    print(plane, "center:", c, "expected:", expected[plane])
    assert all(abs(a - b) < 1e-6 for a, b in zip(c, expected[plane])), plane
    # offset 5 along the plane normal moves the body 5 mm along +normal
    k.new_part("offset " + plane)
    k.extrude(k.sketch(plane, [{"type": "rect", "x": 10, "y": 30, "w": 10, "h": 10}], offset=5), 1)
    c = k.mass_properties()["center_mm"]
    normal = {"XY": 2, "XZ": 1, "YZ": 0}[plane]
    assert abs(c[normal] - 5.5) < 1e-6, (plane, c)
    # upper half-disc (arc 0..180° + diameter line): centroid must be at +v = 4r/3π
    k.new_part("arc " + plane)
    k.extrude(k.sketch(plane, [{"type": "arc", "x": 0, "y": 0, "r": 10, "a1": 0, "a2": 180},
                               {"type": "line", "x1": -10, "y1": 0, "x2": 10, "y2": 0}]), 1)
    m = k.mass_properties()
    v_axis = {"XY": 1, "XZ": 2, "YZ": 2}[plane]
    assert abs(m["volume_mm3"] - 157.08) < 0.1 and abs(m["center_mm"][v_axis] - 4.2441) < 1e-3, (plane, m)
print("PLANE AXES TEST PASSED")
