"""Art's «Флажок» (KOMPAS PDF 5:1, АД31, 2,24 in the title block — grams on so small a part): a
curved base (R35 / R35,1 over 30°) with a plate leaning from it, four views. Once it did not build
at all («значение [x, y, z] не число»: an edge fillet's candidate points), then the main outline was
stretched to a rounded 18,5 high. What must hold: the overall sizes are exactly the drawing's
(25 × 13,5 × 18,39, the last from the file's own lines), the title block gives the material and the
mass, the plan goes through the same resolution as the review window's build; --build builds it
without KOMPAS errors, exactly that size, within 25 % of the title block's mass (the volume is not
right yet: counterbores, grooves on the plate and the R0,25 rounds are not made exactly)."""
import json
import os
import shutil
import sys
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402
import drawing  # noqa: E402

uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(os.path.join(HERE, "pdfvec", "flag.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
st = vec.get("stamp") or {}
assert st.get("mass") == 2.24 and st.get("density") == 2.7, st
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
sizes = sorted(d["value"] for d in reading["dimensions"] if d["overall"])
assert sizes == [13.5, 18.39, 25.0], sizes
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
assert not a["geometry"].get("error"), a["geometry"]
print("\n".join(a["geometry"]["notes"]))
ops = drawing.resolve_plan(a["reading"]["plan"]["operations"], a["values"])   # as the review window does
ok = True
if "--build" in sys.argv:
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "flag", "screenshot": False,
                                                      "close": True, "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    g = (res.get("volume_mm3") or 0) * 2.7 / 1000
    print("built", res.get("operations_done"), "/", len(ops), "mass", round(g, 2), "g vs 2.24 g", "size", res.get("size_mm"))
    size = res.get("size_mm") or [0, 0, 0]
    ok = kcheck.check(res) and not res.get("failed") and all(abs(s - w) <= 0.01 for s, w in zip(size, (25, 13.5, 18.39))) \
        and abs(g / 2.24 - 1) <= 0.25
print("PDF FLAG TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
