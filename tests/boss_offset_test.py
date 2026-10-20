"""A boss placed at offset 0 on a 5 mm plate half-disappears into it: build_part must warn.
With the right offset there must be no warning."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pythoncom  # noqa: E402

pythoncom.CoInitialize()
from kompas_api import Kompas, profile_area  # noqa: E402

assert abs(profile_area([{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60},
                         {"type": "circle", "x": 8, "y": 8, "d": 4}]) - (6000 - 3.14159 * 4)) < 0.01
k = Kompas()
k.connect()
plate = {"op": "extrude", "plane": "XY", "depth": 5, "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60}]}
boss = {"op": "extrude", "plane": "XY", "depth": 10, "shapes": [{"type": "circle", "x": 50, "y": 30, "d": 20}]}
bad = k.build_part([plate, boss])
assert "warning" in bad["results"][1], bad
good = k.build_part([plate, {**boss, "offset": 5}], expected_volume_mm3=30000 + 3141.59)
assert "warning" not in good["results"][1] and good["volume_matches"], good
# the nested form Gemma produced in a real session must be understood as is
nested = [{"cut": False, "depth": 5, "operations": [{"plane": "XY", "shapes": plate["shapes"]}]},
          {"cut": True, "through_all": True, "operations": [{"plane": "XY", "shapes": [{"type": "circle", "x": 8, "y": 8, "d": 4}]}]},
          {"depth": 10, "operations": [{"plane": "XY", "offset": 5, "shapes": boss["shapes"]}]}]
res = k.build_part(nested, expected_volume_mm3=30000 - 62.83 + 3141.59)
assert res["operations_done"] == 3 and res["volume_matches"], res
print("BOSS OFFSET TEST PASSED")
