"""A vector PDF printed from SOLIDWORKS (Art's «Нижняя плита», 110×200×12 mould plate with a pocket,
grooves, 3×Ø14 and 4×Ø3/Ø7): read as lines and text, views at their own scale brought to 1:1,
pocket depths read off sections А-А and Б-Б where they cut the main view. Art's reference model
(«нижняя плита.SLDPRT», KOMPAS opens it; not kept here) has 198 670.5 mm³. Offline: the reading and the plan; with --build the
part is built through the running app and its volume compared (within 0.5 %)."""
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
import dxf  # noqa: E402
import pdfvec  # noqa: E402

REF = 198670.506
uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
pdf = os.path.join(HERE, "pdfvec", "plate.pdf")
shutil.copy(pdf, os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder), "the PDF was not read as a vector drawing"
doc = dxf.read_dxf(os.path.join(folder, "source.dxf"))
n = dxf.numbers(doc)
assert {110.0, 200.0, 12.0, 4.6}.issubset(n["values"]), n["values"]
assert {3.0, 7.0, 14.0}.issubset(n["diameters"]), n["diameters"]  # the Ø sign is drawn, not a letter
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
names = {v["name"]: v["image"] for v in reading["views"]}
assert vec["views"][names["сверху"] - 1]["section"] and vec["views"][names["слева"] - 1]["section"], reading["views"]
sizes = sorted(d["value"] for d in reading["dimensions"] if d["overall"])
assert sizes == [12.0, 110.0, 200.0], sizes
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
assert any("глубина 4.6 мм — по разрезу" in s for s in notes), "main pocket depth not read from the sections"
assert sum("Бобышка" in s for s in notes) >= 3, "the 3×Ø14 bosses (drawn like holes) were not recognised"
assert not any("Ø14 насквозь" in s for s in notes), "Ø14 taken for through holes"
assert sum(s.startswith("Канавка") for s in notes) >= 8, "grooves not cut with the section's cross-section"
assert any("Скругление R0.5" in s for s in notes), "boss top fillets missing"
assert not any("глухое" in s for s in notes), "holes invented from the section in place of the top view"
# the grooves' cross-sections are the section's own lines and arcs (the rounded bottom a true arc),
# not points measured on its picture
grooves = [o for o in a["reading"]["plan"]["operations"] if "канавка" in o.get("comment", "")]
exact = [o for o in grooves if any(s["type"] == "arc" for s in o["shapes"])]
assert len(exact) >= len(grooves) - 1, f"{len(exact)} of {len(grooves)} grooves with exact cross-sections"
ok = True
if "--build" in sys.argv:
    ops = a["reading"]["plan"]["operations"]
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "plate", "screenshot": False}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, f"({(vol - REF) / REF:+.2%})",
          "size", res.get("size_mm"))
    ok = not res.get("failed") and abs(vol - REF) <= 0.002 * REF and res.get("size_mm") == [110.0, 12.0, 200.0] \
        and __import__("kcheck").check(res)
    # no slivers: a face under 0.01 mm wide (twice its area over its outline) and longer than 20 mm
    # is what two cuts that nearly meet leave (Art saw them along the groove walls)
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect(launch=False)
    faces = k.k5.ActiveDocument3D().GetPart(-1).EntityCollection(6)
    slivers = 0
    for i in range(faces.GetCount()):
        fd = faces.GetByIndex(i).GetDefinition()
        edges = fd.EdgeCollection()
        per = sum(edges.GetByIndex(j).GetLength(1) for j in range(edges.GetCount()))
        if per > 20 and 2 * fd.GetArea(0x1) / per < 0.01:
            slivers += 1
    print("slivers", slivers)
    ok = ok and slivers == 0
print("PDF VECTOR TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
