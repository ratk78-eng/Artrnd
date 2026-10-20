"""Views marked «исключить из расчёта» in the review window: their dimensions and the views
themselves take no part in the calculation."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui"))
import drawing  # noqa: E402

reading = {
    "views": [{"image": 1, "name": "главный"}, {"image": 3, "name": "исключить"}],
    "dimensions": [
        {"id": "d1", "value": 100, "view": "вид 1", "axis": "length", "overall": True},
        {"id": "d2", "value": 50, "view": "вид 2", "axis": "width", "overall": True},
        {"id": "d3", "value": 45, "view": "вид 1", "axis": "height", "overall": True},
        {"id": "d4", "value": 999, "view": "вид 3", "axis": "length", "overall": True},   # from the table
    ],
}
assert drawing.excluded_views(reading) == {2}
kept = drawing.without_excluded(reading)
assert [d["id"] for d in kept["dimensions"]] == ["d1", "d2", "d3"], kept
nums = drawing.numbers_from_dims(kept, {})
assert nums["length"] == 100, nums
# layout: views 2 and 3 both lie under view 1; with view 2 excluded, view 3 is the top view
boxes = [(0, 0, 100, 50), (0, 60, 100, 120), (0, 200, 100, 260)]
assert drawing.pick_views({"views": []}, 3, boxes) == (0, 1)
assert drawing.pick_views({"views": [{"image": 2, "name": "исключить"}]}, 3, boxes) == (0, 2)
# an excluded first view: the main view is looked for among the others
assert drawing.pick_views({"views": [{"image": 1, "name": "исключить"}]}, 3, boxes) == (1, 2)
print("EXCLUDE TEST PASSED")
