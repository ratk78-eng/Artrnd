"""Vector path: №13 as DXF → render → views → reconstruction with the numbers from the file. No model."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import drawing  # noqa: E402
import dxf  # noqa: E402
import vision  # noqa: E402

out = os.path.join(os.environ["TEMP"], "kompasmcp-dxf")
os.makedirs(out, exist_ok=True)
src = os.path.join(out, "part13.dxf")
subprocess.run([sys.executable, os.path.join(HERE, "make_dxf13.py"), src], check=True)

doc = dxf.read_dxf(src)
nums = dxf.numbers(doc)
print("entities:", len(doc["entities"]), "hidden:", sum(e["kind"] == "hidden" for e in doc["entities"]))
print("numbers:", nums)
r = dxf.render(doc, os.path.join(out, "part13.png"))
views = drawing.segment_views(r["png"], out)
print("views:", [(v["file"], v["box"]) for v in views])
pair = drawing.pick_views({"views": []}, len(views), [v["box"] for v in views])
front, top = (os.path.join(out, views[i]["file"]) for i in pair)
values = nums["values"]
L, H = dxf.view_size_mm(front, r["px_per_mm"], values)
L2, W = dxf.view_size_mm(top, r["px_per_mm"], values)
print("sizes from the file:", L, W, H, "(top view length", L2, ")")
numbers = {"length": L, "width": W, "height": H, "x": values, "y": values, "z": values,
           "diameters": nums["diameters"], "radii": nums["radii"]}
res = vision.reconstruct(front, top, numbers, upscale=1, exact=True)
print("profile:", res["profile"])
print("notes:", *res["notes"], sep="\n  ")
print("questions:", res["questions"])

if "--build" in sys.argv:
    sys.path.insert(0, os.path.join(HERE, ".."))
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect()
    b = k.build_part(res["operations"], name="№13 из DXF", expected_volume_mm3=93806.6)
    print({key: b.get(key) for key in ("operations_done", "volume_mm3", "size_mm", "volume_matches", "failed")})
    assert b["volume_matches"], b
    assert __import__("kcheck").check(b), b
    print("DXF TEST PASSED")
