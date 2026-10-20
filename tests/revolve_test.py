"""Body of revolution from a longitudinal section picture (insideFront «Втулка», Art's PDF):
segmentation finds the section, detect() recognises an axis with hatched metal, the profile is
revolved. The model's reading is replaced by the drawing's numbers. Reference STEP: 2 718.6 mm³
with real threads, flats and cross holes the drawing does not show — the plain revolve is
expected within +10 %. Usage: python tests/revolve_test.py <page.png> [--build]"""
import os
import shutil
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
sys.path.insert(0, os.path.join(HERE, ".."))
import app  # noqa: E402
import drawing  # noqa: E402
import revolve  # noqa: E402

pdf = sys.argv[1]
uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(pdf, os.path.join(folder, "source.pdf"))
app._render_pdf(os.path.join(folder, "source.pdf"), folder)
page = os.path.join(folder, "page1.png")
views = drawing.segment_views(page, folder)
for v in views:
    v["url"] = f"/uploads/{uid}/{v['file']}"
rev = None
for i in sorted(range(len(views)), key=lambda i: -(views[i]["box"][2] - views[i]["box"][0]) * (views[i]["box"][3] - views[i]["box"][1]))[:3]:
    vpath = app._upload_path(views[i]["url"])
    if revolve.has_axis(vpath):
        fine = app._fine_view(page, views[i]["box"], i)
        det = revolve.detect(fine or vpath)
        if det:
            rev = {"view": i, "path": fine or vpath, **det}
            break
assert rev, f"section of a body of revolution not recognised among {len(views)} views"
print("revolve view", rev)
dia = [15.91, 15.36, 19.14, 17.16, 14.73, 12.35, 12.93, 10.32, 10.25, 14, 10.06, 7.76, 8.82]
lens = [5.09, 2.9, 3.9, 5.19, 9.89, 10.19, 4.4]
dims = [{"id": f"d{k}", "value": v, "axis": "diameter", "meaning": f"Ø{v}", "overall": False} for k, v in enumerate(dia, 1)]
dims += [{"id": f"l{k}", "value": v, "axis": "length", "meaning": "", "overall": False} for k, v in enumerate(lens, 1)]
dims.append({"id": "L", "value": 41.56, "axis": "length", "meaning": "общая длина", "overall": True})
reading = {"views": [], "dimensions": dims, "overall": {}, "features": ["2 лыски S11,4 на Ø12,93, длина 9,29"]}
a = {"id": "r", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in dims}, "revolve": rev, "note": ""}
app._geometry(a)
g = a["geometry"]
assert not g.get("error"), g.get("error")
print("\n".join(g["notes"]))
out = app._analysis_view(a)
print("issues:", [i["text"] for i in out["issues"]])
ok = True
if "--build" in sys.argv:
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect(launch=False)
    r = k.build_part(a["reading"]["plan"]["operations"], "Втулка", True, None)
    k.close_current()   # test parts are closed without saving
    vol = r.get("volume_mm3") or 0
    print("built", r.get("operations_done"), "volume", vol, "size", r.get("size_mm"), "failed", r.get("failed"))
    ok = r.get("failed") is None and 2718.6 * 0.95 <= vol <= 2718.6 * 1.10 and abs(r["size_mm"][0] - 41.56) < 0.05 \
        and __import__("kcheck").check(r)
print("REVOLVE TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
