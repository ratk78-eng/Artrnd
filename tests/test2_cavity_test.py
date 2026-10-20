"""test2 (cover base 9-025-00-00): main view + left view, rounded outline, cavity 17, lip 3, slots
in the bosses, holes. Art's reference model has 56 515 mm³. Offline: the plan; with --build the
part is built in KOMPAS and its volume compared with the reference (±0.5 %)."""
import json
import os
import shutil
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
sys.path.insert(0, os.path.join(HERE, ".."))
import app  # noqa: E402

uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(os.path.join(HERE, "test2_arcs.dxf"), os.path.join(folder, "source.dxf"))
app._prepare_vector(os.path.join(folder, "source.dxf"), folder)
src = os.path.join(folder, "source_vector.png")
vec = app._vector_meta(src)
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
ops = a["reading"]["plan"]["operations"]
notes = a["geometry"]["notes"]
print("\n".join(notes))
assert any("глубина 17" in n for n in notes), "cavity 17 not found"
assert any("глубина 3 " in n for n in notes), "lip 3 not found"
assert any("Ø4.75" in n for n in notes) and any("Ø2.25" in n for n in notes), "holes not found"
ok = True
if "--build" in sys.argv:
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect(launch=False)
    r = k.build_part(ops, "test2", True, None)
    vol = r.get("volume_mm3") or 0
    print("built", r.get("operations_done"), "/", len(ops), "volume", vol, "size", r.get("size_mm"))
    ok = r.get("failed") is None and abs(vol - 56515.36) <= 0.005 * 56515.36 and __import__("kcheck").check(r)
print("TEST2 CAVITY TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
