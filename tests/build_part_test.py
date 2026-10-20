"""build_part through MCP: the «Корпус» from example.pdf in one call (expected 271 421.683 mm³),
plus a failing operation to check partial results are reported."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_test import Client  # noqa: E402

KORPUS = [
    {"op": "extrude", "plane": "XZ", "depth": 70,
     "shapes": [{"type": "polyline", "points": [[0, 0], [110, 0], [110, 60], [70, 60], [40, 40], [40, 30], [0, 30]]}]},
    {"op": "cut", "plane": "XY", "offset": 30, "depth": 40, "shapes": [{"type": "rect", "x": 40, "y": 15, "w": 55, "h": 40}]},
    {"op": "cut", "plane": "XY", "through_all": True, "shapes": [{"type": "circle", "x": 55, "y": 35, "d": 30}]},
    {"op": "cut", "plane": "XY", "offset": 30, "depth": 8, "reverse": True, "shapes": [{"type": "rect", "x": 0, "y": 20, "w": 55, "h": 30}]},
]

c = Client()
c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bp", "version": "0"}})
c.send("notifications/initialized", notify=True)
res = c.call("build_part", operations=KORPUS, name="Корпус", expected_volume_mm3=271421.7)
data = json.loads(res["content"][0]["text"])
assert data["operations_done"] == 4 and data["volume_matches"], data
assert any(part["type"] == "image" for part in res["content"]), "no screenshot"

bad = KORPUS[:1] + [{"op": "cut", "plane": "XY", "depth": 5, "shapes": [{"type": "spline"}]}]
data = json.loads(c.call("build_part", operations=bad)["content"][0]["text"])
assert data["operations_done"] == 1 and data["failed"]["n"] == 2, data
c.p.terminate()
print("BUILD_PART TEST PASSED")
