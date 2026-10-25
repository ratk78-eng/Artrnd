"""The review window's feature table: operations become rows with editable centre / size, and the
user's edits (a row switched off, a new Ø, a moved slot) are applied to the plan before building."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402

ops = [
    {"op": "extrude", "plane": "XY", "depth": 10, "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50}],
     "comment": "плита"},
    {"op": "cut", "plane": "XY", "through_all": True, "shapes": [{"type": "circle", "x": 20, "y": 25, "r": 5}],
     "comment": "отверстие Ø10"},
    {"op": "cut", "plane": "XY", "through_all": True, "comment": "паз",
     "shapes": [{"type": "polyline", "closed": True, "points": [[60, 20], [70, 20], [70, 30], [60, 30]]}]},
    {"op": "fillet", "radius": 2, "points": [[0, 0, 5]], "comment": "скругление"},
]
feats = app._op_features(ops)
assert [f["id"] for f in feats] == ["op0", "op1", "op2", "op3"], feats
assert feats[0]["base"] and feats[1]["params"] == {"x": 20, "y": 25, "d": 10}, feats[1]
assert feats[2]["params"] == {"x": 65, "y": 25, "w": 10, "h": 10} and feats[3]["params"] == {}, feats[2:]

out = app._apply_feature_edits(ops, {"op0": {"on": False},          # the body cannot be switched off
                                     "op1": {"params": {"d": 12, "x": "22"}},
                                     "op2": {"params": {"w": 4}},
                                     "op3": {"on": False}})
assert [o["op"] for o in out] == ["extrude", "cut", "cut"], out
c = out[1]["shapes"][0]
assert c["d"] == 12 and c["x"] == 22 and c["y"] == 25 and "r" not in c, c
assert out[2]["shapes"][0]["points"] == [[63, 20], [67, 20], [67, 30], [63, 30]], out[2]
assert ops[1]["shapes"][0] == {"type": "circle", "x": 20, "y": 25, "r": 5}, "the plan itself stays as read"
assert app._apply_feature_edits(ops, {}) is ops
# an edge fillet's candidate points (a list of [X, Y, Z] per edge) resolve as numbers, as do expressions
import drawing  # noqa: E402
fil = [{"op": "fillet", "radius": "d1/2", "edge_round": True,
        "points": [[[14.5, 6.5, 9.85], [14.5, 6.5, "d1"]], [10, 0, 2]]}]
r = drawing.resolve_plan(fil, {"d1": 4})
assert r[0]["radius"] == 2 and r[0]["points"] == [[[14.5, 6.5, 9.85], [14.5, 6.5, 4.0]], [10, 0, 2]], r
print("FEATURE EDITS TEST PASSED")
