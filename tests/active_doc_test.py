"""Regression: new_part followed by plate_with_holes (which makes another part) must keep building
in the newest part. Before the fix KOMPAS's "active document" switched back to the empty first part."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pythoncom  # noqa: E402

pythoncom.CoInitialize()
from kompas_api import Kompas  # noqa: E402

k = Kompas()
k.connect()
k.new_part("empty first")
k.new_part("plate")  # plate_with_holes starts the same way
sid = k.sketch("XY", [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60}])
k.extrude(sid, 5)
sid = k.sketch("XY", [{"type": "circle", "x": 50, "y": 30, "d": 20}], offset=5)
res = k.extrude(sid, 10)
vol = k.mass_properties()["volume_mm3"]
expected = 100 * 60 * 5 + 3.141592653589793 * 10 ** 2 * 10
print(res, vol)
assert abs(vol - expected) < 1, (vol, expected)
print("ACTIVE DOC TEST PASSED")
