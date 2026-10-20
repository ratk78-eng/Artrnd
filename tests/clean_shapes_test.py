"""Malformed primitives from models must give readable errors, and common variants must be accepted."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from kompas_api import KompasError, clean_shapes  # noqa: E402

ok = clean_shapes([
    {"type": "polyline", "points": [{"x": 0, "y": 0}, {"x": "10", "y": 0}, {"x": 10, "y": 5}]},
    {"type": "polyline", "points": [0, 0, 10, 0, 10, 5]},
    {"type": "polyline", "points": "[[0,0],[10,0],[10,5]]"},
    {"type": "circle", "x": "5", "y": 5, "d": "4"},
])
assert ok[0]["points"] == ok[1]["points"] == ok[2]["points"] == [[0, 0], [10, 0], [10, 5]], ok
assert ok[3]["x"] == 5.0 and ok[3]["d"] == 4.0

for bad, words in [
    ([{"type": "polyline", "points": [[0, 0], [10]]}], "точка 2"),
    ([{"type": "polyline", "points": [0, 0, 10]}], "нечётное"),
    ([{"type": "circle", "x": 0, "y": 0}], "r или d"),
    ([{"type": "rect", "x": 0, "y": 0, "w": 5}], "h"),
    ([{"type": "ellipse"}], "неизвестный тип"),
    ([{"type": "spline"}], "points"),
    ([{"type": "line", "x1": "a", "y1": 0, "x2": 1, "y2": 1}], "x1"),
]:
    try:
        clean_shapes(bad)
    except KompasError as e:
        assert words in str(e), (bad, str(e))
    else:
        raise AssertionError(f"no error for {bad}")
print("CLEAN SHAPES TEST PASSED")
