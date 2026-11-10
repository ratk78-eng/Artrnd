"""Art's «Накладка (В1)» K4110.00.00.002 (KOMPAS PDF through libHaru: its font is UTF-8 «Modern-UTF8-H»,
the diameter sign U+2300): a turned part standing up — the longitudinal section В-В is tall, the
bore an unhatched strip down its middle — with five grooves: deep at section Б-Б, a notch at А-А,
their width at each height read off the main view.
What must hold: the texts are read (Ø16.5…Ø42.3, 188, R648, the mass 0,07), the part is the section's
fuller wall turned about the bore's middle, standing along Z, 42.3 × 42.3 × 188 exactly; --build
builds it without KOMPAS errors."""
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
shutil.copy(os.path.join(HERE, "pdfvec", "nakl.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
n = vec["numbers"]
assert n["diameters"] == [16.5, 20.0, 25.8, 29.2, 42.3] and 188.0 in n["values"] and n["radii"] == [648.0], n
assert (vec.get("stamp") or {}).get("mass") == 0.07, vec.get("stamp")
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
assert not a["geometry"].get("error"), a["geometry"]
print("\n".join(a["geometry"]["notes"]))
assert a["reading"]["overall"] == {"length": 42.3, "width": 42.3, "height": 188.0}, a["reading"]["overall"]
ops = drawing.resolve_plan(a["reading"]["plan"]["operations"], a["values"])
assert ops[0]["op"] == "revolve" and ops[0]["plane"] == "XZ", ops[0]
# the five grooves: a cut loft of cutter circles, deep at Б-Б (z 17.7), a notch at А-А (z 178.3), copied ×5
assert [o["op"] for o in ops] == ["revolve", "loft", "circular"] and ops[2]["count"] == 5, [o["op"] for o in ops]
assert any("5 канавок" in s and "17.7" in s and "178.3" in s for s in a["geometry"]["notes"]), a["geometry"]["notes"]
ok = True
if "--build" in sys.argv:
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "nakl", "screenshot": False,
                                                      "close": True, "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    size = res.get("size_mm") or [0, 0, 0]
    print("built", res.get("operations_done"), "/", len(ops), "volume", res.get("volume_mm3"), "size", size)
    # across: the lobes between the grooves (section Б-Б is 41.57 × 39.71 on the sheet)
    ok = kcheck.check(res) and not res.get("failed") and abs(size[2] - 188) <= 0.01 \
        and 41.0 < size[0] < 42.4 and 39.3 < size[1] < 40.6 and 60000 < (res.get("volume_mm3") or 0) < 72000
print("PDF NAKL TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
