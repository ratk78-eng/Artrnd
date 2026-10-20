"""A turned part in a vector PDF (Art's «Зубчатая рейка m2,5 L340», SOLIDWORKS): Ø30×340 shaft with
grooves, a rack of 31 teeth on a flat, a keyway and centre holes. The title block says «1:5», but the
views are drawn at 1:1.3 — the scale has to come from the dimensions. The body of revolution comes
from section А-А, the teeth from the main view's outline, the keyway depth from the section.
Art's reference STEP has 172 595.3 mm³. With --build: built through the running app (within 0.5 %)."""
import json
import os
import shutil
import sys
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
sys.path.insert(0, os.path.join(HERE, ".."))
import app  # noqa: E402

REF = 172595.287
uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(os.path.join(HERE, "pdfvec", "rack.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
assert vec.get("shaft"), "the section was not taken for a body of revolution"
assert 30.0 in vec["numbers"]["diameters"], vec["numbers"]  # «Ø30 h8 -0,033»: fit and tolerance
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
sizes = sorted(d["value"] for d in reading["dimensions"] if d["overall"])
assert sizes == [30.0, 30.0, 340.0], sizes
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
ops = a["reading"]["plan"]["operations"]
assert ops[0]["op"] == "revolve" and any("контура главного вида" in o.get("comment", "") for o in ops)
assert any(n.startswith("Паз") for n in notes), "keyway not found"
ok = True
if "--build" in sys.argv:
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "rack", "screenshot": False}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, f"({(vol - REF) / REF:+.2%})",
          "size", res.get("size_mm"))
    ok = not res.get("failed") and abs(vol - REF) <= 0.005 * REF and res.get("size_mm") == [340.0, 30.0, 30.0] \
        and __import__("kcheck").check(res)
print("PDF SHAFT TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
