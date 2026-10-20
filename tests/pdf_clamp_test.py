"""Art's «Прижим» (SOLIDWORKS PDF, Д16Т, 215.43 g in the title block ≈ 77 000 mm³): a plate machined
on both sides with a raised block, eight views. What must hold so far: the main view is the plain
view the others line up with (not the larger labelled view «В»), the part is 28 thick (the block),
the outline from the side comes from the plain view over the main one (turned over), section А-А is
read with its front face at the top (its cutting arrows point up), and no holes are invented on the
pictures. The volume is not yet right (the back side and the block's own slots are not built)."""
import os
import shutil
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402
import dxf  # noqa: E402

uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
shutil.copy(os.path.join(HERE, "pdfvec", "clamp.pdf"), os.path.join(folder, "source.pdf"))
assert app._vector_pdf(os.path.join(folder, "source.pdf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
lay = app._vector_layout(vec)
main = vec["views"][lay["front"]]
assert main.get("label") in (None, "Д"), main.get("label")          # «Д» is a cutting-line letter
assert abs(main["box_mm"][2] - main["box_mm"][0] - 143) < 0.5
assert lay["silhouette"] and lay["silhouette"][1], lay              # the plain view above, turned over
doc = dxf.read_dxf(os.path.join(folder, "source.dxf"))
top = vec["views"][lay["top"]]["box_mm"]
assert dxf.cut_front(doc, main["box_mm"], top, "y") == "top"
reading = app._vector_reading(vec, [app._upload_path(v["url"]) for v in views], [v["box"] for v in views])
sizes = sorted(d["value"] for d in reading["dimensions"] if d["overall"])
assert sizes == [28.0, 66.7, 143.0], sizes
a = {"id": "t", "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
     "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
app._geometry(a)
notes = a["geometry"]["notes"]
print("\n".join(notes))
assert any("Силуэт сбоку взят с вида" in s for s in notes)
assert not any(s.startswith("Ø") and ("глухое" in s or "сквозное" in s) for s in notes), "holes invented"
assert any("тыльная сторона" in s for s in notes), "the back view «В» not used"
ok = True
if "--build" in sys.argv:
    # Art's reference model («Прижим Артур.STEP») has 77 490 mm³
    import json
    import urllib.request
    ops = a["reading"]["plan"]["operations"]
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "clamp", "screenshot": False, "close": True,
                                                      "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    print("built", res.get("operations_done"), "/", len(ops), "volume", vol, f"({(vol - 77490) / 77490:+.2%})",
          "size", res.get("size_mm"))
    # and no feature KOMPAS marks with an error («Тело с нарушенной топологией» on a back notch
    # ending exactly on the end face, Art's screenshot)
    import kcheck
    ok = abs(vol - 77490) <= 0.01 * 77490 and res.get("size_mm") == [143.0, 28.0, 66.7] and kcheck.check(res)
print("PDF CLAMP TEST", "PASSED" if ok else "FAILED")
shutil.rmtree(folder, ignore_errors=True)
