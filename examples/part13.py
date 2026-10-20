"""Part №13 (the user's task drawing) built with build_part, as a reference for what the model
should produce. X — length 100, Y — width 50, Z — height 45.

Reading of the drawing: base 100×50×20; middle tower x 25..75 up to 45 with a 36 wide, 10 deep slot
on top; a 42 wide, 15 high channel underneath along Y; in the middle a Ø20 hole 10 deep from the slot
bottom, continued down to the channel by a square hole inscribed in that circle (drawn as a diamond,
corrected by the user: the hole is not round all the way); two Ø10 holes 82 apart with U-shaped R12
recesses open to the ends, 15 deep from the top of the base (floor at Z 5). 93 806.6 mm³.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pythoncom  # noqa: E402

pythoncom.CoInitialize()
from kompas_api import Kompas  # noqa: E402

FRONT = [[0, 0], [29, 0], [29, 15], [71, 15], [71, 0], [100, 0], [100, 20], [75, 20], [75, 45], [68, 45],
         [68, 35], [32, 35], [32, 45], [25, 45], [25, 20], [0, 20]]


def u_pocket(cx, side):
    """U-shaped spot face R12 around a hole at (cx, 25), open towards the end of the part."""
    end = 100 if side > 0 else 0
    a1, a2 = (90, 270) if side > 0 else (-90, 90)
    return [{"type": "line", "x1": end, "y1": 13, "x2": cx, "y2": 13},
            {"type": "arc", "x": cx, "y": 25, "r": 12, "a1": a1, "a2": a2},
            {"type": "line", "x1": cx, "y1": 37, "x2": end, "y2": 37},
            {"type": "line", "x1": end, "y1": 37, "x2": end, "y2": 13}]


OPERATIONS = [
    {"op": "extrude", "plane": "XZ", "depth": 50, "shapes": [{"type": "polyline", "points": FRONT}]},
    # Ø20 counterbore 10 deep from the slot bottom (Z 35 → 25) …
    {"op": "cut", "plane": "XY", "offset": 35, "depth": 10, "reverse": True,
     "shapes": [{"type": "circle", "x": 50, "y": 25, "d": 20}]},
    # … then a square inscribed in it (corners on the axes, as drawn) down into the channel
    {"op": "cut", "plane": "XY", "offset": 25, "reverse": True, "through_all": True,
     "shapes": [{"type": "polyline", "points": [[60, 25], [50, 35], [40, 25], [50, 15]]}]},
    {"op": "cut", "plane": "XY", "through_all": True, "shapes": [{"type": "circle", "x": 9, "y": 25, "d": 10},
                                                                 {"type": "circle", "x": 91, "y": 25, "d": 10}]},
    # U-shaped recesses 15 deep from the top of the base (floor at Z 5, dashed line on the front view;
    # confirmed by the user) — the Ø10 holes only pierce the remaining 5 mm
    {"op": "cut", "plane": "XY", "offset": 20, "depth": 15, "reverse": True, "shapes": u_pocket(91, +1)},
    {"op": "cut", "plane": "XY", "offset": 20, "depth": 15, "reverse": True, "shapes": u_pocket(9, -1)},
]

pocket = (math.pi * 12 ** 2 / 2 + 24 * 9) * 15
center_hole = math.pi * 10 ** 2 * 10 + (20 ** 2 / 2) * 10  # Ø20 × 10, then square (diagonal 20) × 10
EXPECTED = 100 * 50 * 20 + 50 * 50 * 25 - 36 * 50 * 10 - 42 * 50 * 15 - center_hole \
    - 2 * math.pi * 5 ** 2 * 5 - 2 * pocket

if __name__ == "__main__":
    k = Kompas()
    k.connect()
    res = k.build_part(OPERATIONS, name="Деталь 13", expected_volume_mm3=EXPECTED)
    print(res)
    assert res["operations_done"] == len(OPERATIONS) and res["volume_matches"], res
    out = os.path.join(os.path.expanduser("~"), "Documents", "kompas-mcp", "part13")
    k.save(os.path.join(out, "part13.m3d"))
    print(k.screenshot(os.path.join(out, "part13.png")))
    print("PART 13 OK, expected", round(EXPECTED, 1))
