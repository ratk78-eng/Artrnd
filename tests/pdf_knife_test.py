"""Art's «Крепление ножа Б-2024.01.003» — a PDF saved by KOMPAS itself. Such a PDF puts every letter
in a text block of its own («1 0»), writes the diameter sign as «G» of its Symbol_A font, draws
every line as a path there and back again and every circle as a run of tiny chords on a 1/300″
grid, and paints white scan lines under arrowheads. What must hold: the numbers read as numbers,
three views, the part 38×50×45, the lug's R10 and the holes found as circles, the channel 22×8
under the part, and the part built without a KOMPAS error. (Art's model of it differs from the
drawing — lug along the length, two Ø8 — so only the drawing is checked.)"""
import os
import shutil
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402

uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(os.path.join(HERE, "pdfvec", "knife.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
n = vec["numbers"]
assert {10.0, 14.0, 19.0, 20.0, 22.0, 38.0, 40.0, 45.0, 50.0} <= set(n["values"]), n
assert {7.0, 8.0, 10.0} <= set(n["diameters"]) and 10.0 in n["radii"], n
assert len(vec["views"]) == 3, vec["views"]
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
sizes = sorted(d["value"] for d in reading["dimensions"] if d["overall"])
assert sizes == [38.0, 45.0, 50.0], sizes
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
ops = a["reading"]["plan"]["operations"]
assert any(n.startswith("Паз X 8.1…29.9") and "высотой 8" in n for n in notes), notes
assert any(n.startswith("6 отв. вдоль длины") for n in notes), notes
side = next(o for o in ops if o.get("comment") == "контур вида слева")
assert any(s["type"] == "arc" and abs(s["r"] - 10) < 0.01 for s in side["shapes"]), side["shapes"]
if "--build" in sys.argv:
    import json
    import urllib.request
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "knife", "screenshot": False,
                                                      "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, "size", res.get("size_mm"))
    # body 38×50×20 − channel − holes + lug 10 thick, 25 high with R10 and Ø10: about 32 000 mm³
    # (the stamp's 0.25 kg of steel is 31 850)
    assert 31000 <= vol <= 33000 and res.get("size_mm") == [38.0, 50.0, 45.0], res
    assert kcheck.check(res) and not res.get("failed"), res
print("PDF KNIFE TEST PASSED")
shutil.rmtree(folder, ignore_errors=True)
