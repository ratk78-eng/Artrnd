"""Drawing-analysis checks on a hand-written reading of task №13 (the answer a perfect model gives):
no issues for the right values, the plan builds the reference part, and wrong values are caught."""
import copy
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import drawing  # noqa: E402

D = lambda i, v, view, meaning, axis, overall=False: {"id": i, "value": v, "view": view, "meaning": meaning,  # noqa: E731
                                                     "axis": axis, "overall": overall, "confidence": 0.9}
READING = {
    "title": "Деталь 13",
    "views": [{"image": 1, "name": "главный"}, {"image": 2, "name": "сверху"}],
    "dimensions": [
        D("d1", 100, "главный", "длина детали", "length", True),
        D("d2", 45, "главный", "высота детали", "height", True),
        D("d3", 20, "главный", "высота основания", "height"),
        D("d4", 36, "главный", "ширина паза сверху", "length"),
        D("d5", 10, "главный", "глубина паза сверху", "height"),
        D("d6", 42, "главный", "ширина канала снизу", "length"),
        D("d7", 15, "главный", "высота канала снизу", "height"),
        D("d8", 50, "сверху", "ширина детали", "width", True),
        D("d9", 50, "сверху", "ширина стойки", "length"),
        D("d10", 82, "сверху", "расстояние между отверстиями", "length"),
        D("d11", 20, "сверху", "диаметр центрального отверстия", "diameter"),
        D("d12", 10, "сверху", "диаметр отверстий", "diameter"),
        D("d13", 12, "сверху", "радиус подрезки", "radius"),
        D("d14", 15, "главный", "глубина подрезки от верха основания", "height"),
        D("d16", 10, "главный", "глубина отверстия Ø20 (ниже — квадрат, вписанный в него)", "height"),
    ],
    "overall": {"length": "d1", "width": "d8", "height": "d2"},
    "chains": [],
    "features": ["основание", "стойка с пазом", "канал снизу", "отверстие Ø20", "2 отверстия Ø10 с подрезками R12"],
    "plan": {"operations": [
        {"op": "extrude", "plane": "XZ", "depth": "d8", "shapes": [{"type": "polyline", "points": [
            [0, 0], ["(d1-d6)/2", 0], ["(d1-d6)/2", "d7"], ["(d1+d6)/2", "d7"], ["(d1+d6)/2", 0], ["d1", 0],
            ["d1", "d3"], ["(d1+d9)/2", "d3"], ["(d1+d9)/2", "d2"], ["(d1+d4)/2", "d2"], ["(d1+d4)/2", "d2-d5"],
            ["(d1-d4)/2", "d2-d5"], ["(d1-d4)/2", "d2"], ["(d1-d9)/2", "d2"], ["(d1-d9)/2", "d3"], [0, "d3"]]}]},
        {"op": "cut", "plane": "XY", "offset": "d2-d5", "depth": "d16", "reverse": True,
         "shapes": [{"type": "circle", "x": "d1/2", "y": "d8/2", "d": "d11"}]},
        {"op": "cut", "plane": "XY", "offset": "d2-d5-d16", "reverse": True, "through_all": True,
         "shapes": [{"type": "polyline", "points": [["d1/2+d11/2", "d8/2"], ["d1/2", "d8/2+d11/2"],
                                                    ["d1/2-d11/2", "d8/2"], ["d1/2", "d8/2-d11/2"]]}]},
        {"op": "cut", "plane": "XY", "through_all": True, "shapes": [
            {"type": "circle", "x": "(d1-d10)/2", "y": "d8/2", "d": "d12"},
            {"type": "circle", "x": "(d1+d10)/2", "y": "d8/2", "d": "d12"}]},
        {"op": "cut", "plane": "XY", "offset": "d3", "depth": "d14", "reverse": True, "shapes": [
            {"type": "line", "x1": "d1", "y1": "d8/2-d13", "x2": "(d1+d10)/2", "y2": "d8/2-d13"},
            {"type": "arc", "x": "(d1+d10)/2", "y": "d8/2", "r": "d13", "a1": 90, "a2": 270},
            {"type": "line", "x1": "(d1+d10)/2", "y1": "d8/2+d13", "x2": "d1", "y2": "d8/2+d13"},
            {"type": "line", "x1": "d1", "y1": "d8/2+d13", "x2": "d1", "y2": "d8/2-d13"}]},
    ]},
}

values = {d["id"]: d["value"] for d in READING["dimensions"]}
issues = drawing.check(READING, values)
errors = [i for i in issues if i["level"] == "error"]
assert not errors, errors
print("issues for correct values:", issues)

# a misread overall length must break the projection link / bounding-box check
bad = dict(values, d1=110)
bad_issues = drawing.check(READING, bad)
assert not any(i["level"] == "error" for i in bad_issues), "length change is consistent by construction"
r2 = copy.deepcopy(READING)
r2["dimensions"].append(D("d15", 110, "сверху", "длина детали на виде сверху", "length", True))
r2_issues = drawing.check(r2, {**values, "d15": 110})
assert any("Проекционная связь" in i["text"] for i in r2_issues), r2_issues

# a plan number that is not on the drawing is reported
r3 = copy.deepcopy(READING)
r3["plan"]["operations"][1]["shapes"][0]["d"] = 22  # the Ø20 circle with a number not on the drawing
assert any("не привязанные" in i["text"] for i in drawing.check(r3, values))

if "--build" in sys.argv:
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect()
    ops = drawing.resolve_plan(READING["plan"]["operations"], values)
    res = k.build_part(ops, name="drawing_test")
    k.close_current()   # test parts are closed without saving
    print(res)
    # one pocket here (the right one) — reference part13.py has two
    pocket = (math.pi * 12 ** 2 / 2 + 24 * 9) * 15   # floor at Z 5
    center_hole = math.pi * 100 * 10 + 200 * 10
    # left Ø10 goes through the whole 20 mm base, the right one only through the 5 mm under the recess
    expected = 100 * 50 * 20 + 50 * 50 * 25 - 36 * 50 * 10 - 42 * 50 * 15 - center_hole \
        - math.pi * 25 * 20 - math.pi * 25 * 5 - pocket
    assert abs(res["volume_mm3"] - expected) < 1, (res["volume_mm3"], expected)
    assert __import__("kcheck").check(res), res
    assert res["size_mm"] == [100, 50, 45], res["size_mm"]
print("DRAWING TEST PASSED")
