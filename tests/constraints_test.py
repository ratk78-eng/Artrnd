"""Sketch constraints (needs a running KOMPAS): the type codes found by experiment still mean what
kompas_api.constrain assumes, and a contour built through build_part gets its constraints without
moving. A line ending 0.28 mm off another's start is merged by code 11; a nearly level line is made
horizontal by 3; a line meeting an arc at 1.9° is made tangent by 15 (the solver moves the arc)."""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import pythoncom  # noqa: E402

pythoncom.CoInitialize()
import kompas_api  # noqa: E402
from kompas_api import Kompas, o3d_sketch  # noqa: E402

k = Kompas()
k.connect(launch=False)
mod = sys.modules[type(k.k5).__module__]


def struct(n, cls):
    return cls(k.k5.GetParamStruct(n)._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch))


def line_of(d2, ref):
    p = struct(11, mod.ksLineSegParam)
    d2.ksGetObjParam(ref, p, -1)
    return p.x1, p.y1, p.x2, p.y2


def put(d2, obj, kind, index=0, partner=0, pidx=0):
    c = struct(kompas_api.ko_ConstraintParam, mod.ksConstraintParam)
    c.Init()
    c.constrType, c.index, c.partner, c.partnerIndex = kind, index, partner, pidx
    return d2.ksSetObjConstraint(obj, c)


k.new_part("constraints_test")
part = k.part()


def sketch():
    sk = part.NewEntity(o3d_sketch)
    sd = sk.GetDefinition()
    sd.SetPlane(k._plane("XY", 0))
    sk.Create()
    return sd, sd.BeginEdit()


sd, d2 = sketch()
a = d2.ksLineSeg(0, 0, 30, 0.7, 1)
b = d2.ksLineSeg(30.2, 0.9, 31, 20, 1)
assert put(d2, a, kompas_api.C_MERGE, 1, b, 0)
la, lb = line_of(d2, a), line_of(d2, b)
assert math.dist(la[2:], lb[:2]) < 1e-6, (la, lb)
assert put(d2, a, kompas_api.C_HORIZONTAL)
la = line_of(d2, a)
assert abs(la[1] - la[3]) < 1e-6, la
sd.EndEdit()

sd, d2 = sketch()
a = d2.ksLineSeg(0, -1, 30, 0, 1)
arc = d2.ksArcByPoint(30, 5, 5, 30, 0, 35, 5, 1, 1)
assert put(d2, a, kompas_api.C_TANGENT, 0, arc, 0)
q = struct(12, mod.ksArcByAngleParam)
d2.ksGetObjParam(arc, q, -1)
x1, y1, x2, y2 = line_of(d2, a)
dist = abs((y2 - y1) * (q.xc - x1) - (x2 - x1) * (q.yc - y1)) / math.hypot(x2 - x1, y2 - y1)
assert abs(dist - q.rad) < 1e-6, (dist, q.rad)
sd.EndEdit()

# a rounded rectangle through build_part: merged, tangent, level and upright, nothing moved
r = 5
shapes = [{"type": "line", "x1": r, "y1": 0, "x2": 40 - r, "y2": 0},
          {"type": "arc", "x": 40 - r, "y": r, "r": r, "a1": 270, "a2": 360, "x1": 40 - r, "y1": 0, "x2": 40, "y2": r},
          {"type": "line", "x1": 40, "y1": r, "x2": 40, "y2": 20 - r},
          {"type": "arc", "x": 40 - r, "y": 20 - r, "r": r, "a1": 0, "a2": 90, "x1": 40, "y1": 20 - r, "x2": 40 - r, "y2": 20},
          {"type": "line", "x1": 40 - r, "y1": 20, "x2": r, "y2": 20},
          {"type": "arc", "x": r, "y": 20 - r, "r": r, "a1": 90, "a2": 180, "x1": r, "y1": 20, "x2": 0, "y2": 20 - r},
          {"type": "line", "x1": 0, "y1": 20 - r, "x2": 0, "y2": r},
          {"type": "arc", "x": r, "y": r, "r": r, "a1": 180, "a2": 270, "x1": 0, "y1": r, "x2": r, "y2": 0}]
k.close_current()   # test parts are closed without saving
res = k.build_part([{"op": "extrude", "plane": "XY", "depth": 10, "shapes": shapes}], "constraints_test")
k.close_current()
c = res.get("sketch_constraints", {})
print(c, res["volume_mm3"])
assert c.get("совпадение") == 8 and c.get("касание") == 8 and c.get("горизонталь") == 2 and c.get("вертикаль") == 2, c
assert "эскизов без связей" not in c and "не наложено" not in c, c
assert abs(res["volume_mm3"] - (40 * 20 - (4 - math.pi) * r * r) * 10) < 0.01, res["volume_mm3"]
print("CONSTRAINTS TEST PASSED")
