"""Art's «Колесо» (spiral bevel gear, z 24, KOMPAS PDF, A3). Not a steel plate: a tolerance «-0,1»
written apart once made it «лист 0,1». It is a turned part: the hatched section «А» along the axis
(as tall as Ø72.7) beside the round face view — the areas above the axis (the teeth, unhatched by
ESKD, included) are turned round it, the bore with its keyway is cut from the face view. The
table (ГОСТ 2.405) gives the teeth: circular, βm 35° right-hand, cutter head Ø100 — the tooth space
is cut through sections along the tooth line and copied 24 times round, before the rest of the
blank is added."""
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
b = vec.get("bevel") or {}
assert b.get("z") == 24 and b.get("type") == "circular" and b.get("beta") == 35 and b.get("hand") == "right" \
    and b.get("d0") == 100 and abs(b.get("delta_f", 0) - 40.0689) < 1e-3, b
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
ops = a["reading"]["plan"]["operations"]
kinds = [o["op"] for o in ops]
# the rim, the tooth space through sections, 24 of them round, the rest of the blank, the bore
assert kinds[:3] == ["revolve", "loft", "circular"] and ops[2]["count"] == 24 and kinds[-1] == "cut", kinds
assert 8 <= len(ops[1]["sections"]) <= 12, len(ops[1]["sections"])
assert any(n.startswith("Коническое колесо по таблице: z 24") for n in notes), notes
assert any("шпоночный паз" in n for n in notes), notes
if "--build" in sys.argv:
    import json
    import urllib.request
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "wheel", "screenshot": False, "close": True,
                                                      "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=1800))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, "size", res.get("size_mm"))
    s = sorted(res.get("size_mm") or [])
    # the blank is 65 382 mm³; 24 spaces of about 300 mm³ each come off
    assert 56000 <= vol <= 60000 and abs(s[0] - 45.2) < 0.1 and abs(s[2] - 72.7) < 0.4, res
    assert kcheck.check(res) and not res.get("failed"), res
print("PDF WHEEL TEST PASSED")
shutil.rmtree(folder, ignore_errors=True)
