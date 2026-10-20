"""Model replies the drawing reader must survive (JSON inside text, fences, reasoning with fragments)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui"))
from drawing import parse_reading  # noqa: E402

J = '{"views": [], "dimensions": [{"id": " d1", "value": 100}], "overall": {}, "features": []}'
assert parse_reading(J)["dimensions"][0]["id"] == "d1"
assert parse_reading("Вот ответ:\n```json\n" + J + "\n```\nГотово.")["dimensions"][0]["value"] == 100
reasoning = 'Let me think. A dimension like {"id": "d1"} is small. Final answer: ' + J + " That's it {x}"
assert parse_reading(reasoning)["dimensions"][0]["value"] == 100
try:
    parse_reading("The")
except ValueError:
    pass
else:
    raise AssertionError("no JSON must raise")
r = parse_reading('{"dimensions":[{"id":"d17","value":"Ø14 / 7,39","axis":"other","meaning":"Расточка"},'
                          '{"id":"d12","value":"Ø12,92→Ø10,07","axis":"diameter","meaning":"Конус"}]}')
vals = [(d["id"], d["value"], d["axis"]) for d in r["dimensions"]]
assert vals == [("d17", 14.0, "diameter"), ("d17_2", 7.39, "other"), ("d12", 12.92, "diameter"), ("d12_2", 10.07, "diameter")], vals
# views as plain names (Gemma did this on the GOST drawing and the analysis crashed)
r = parse_reading('{"views":["главный","сверху"],"dimensions":[{"id":"d1","value":5}]}')
assert r["views"] == [{"image": 1, "name": "главный"}, {"image": 2, "name": "сверху"}], r["views"]
r = parse_reading('{"views":[{"image":"Изображение 3","name":"Слева"}, 7],"dimensions":[]}')
assert r["views"] == [{"image": 3, "name": "слева"}], r["views"]
print("PARSE READING TEST PASSED")
