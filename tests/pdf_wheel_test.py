"""Art's «Колесо» (spiral bevel gear, z 24, KOMPAS PDF, A3). Not a steel plate: a tolerance «-0,1»
written apart once made it «лист 0,1». It is a turned part: the hatched section «А» along the axis
(as tall as Ø72.7) beside the round face view — the areas above the axis (the teeth, unhatched by
ESKD, included) are turned round it, the bore with its keyway is cut from the face view. The
spiral teeth themselves are not built."""
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
shutil.copy(os.path.join(HERE, "pdfvec", "wheel.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
assert not vec.get("steel"), vec.get("steel")
assert {30.0, 45.0, 72.7} <= set(vec["numbers"]["diameters"]), vec["numbers"]
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
ops = a["reading"]["plan"]["operations"]
assert ops and all(o["op"] == "revolve" for o in ops[:-1]) and ops[-1]["op"] == "cut", [o["op"] for o in ops]
assert any("шпоночный паз" in n for n in notes), notes
if "--build" in sys.argv:
    import json
    import urllib.request
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "wheel", "screenshot": False, "close": True,
                                                      "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, "size", res.get("size_mm"))
    s = sorted(res.get("size_mm") or [])
    assert 60000 <= vol <= 70000 and abs(s[0] - 45.2) < 0.1 and abs(s[2] - 72.7) < 0.2, res
    assert kcheck.check(res) and not res.get("failed"), res
print("PDF WHEEL TEST PASSED")
shutil.rmtree(folder, ignore_errors=True)
