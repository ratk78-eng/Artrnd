"""«Метод переноса» on the textbook example (figure д): an L-shaped part with a 16×10 slot that only the
left view and the top view show. Views are cut from the scanned page by hand (box coordinates)."""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import vision  # noqa: E402

page = sys.argv[1]
out = os.path.join(os.environ["TEMP"], "kompasmcp-vision")
os.makedirs(out, exist_ok=True)
im = Image.open(page)
boxes = {"front": (272, 38, 418, 126), "side": (452, 38, 546, 126), "top": (272, 168, 418, 272)}
paths = {}
for name, box in boxes.items():
    paths[name] = os.path.join(out, f"d_{name}.png")
    im.crop(box).save(paths[name])

# the book gives only 16 and 10; the other sizes follow from the picture at the same scale
f = vision.silhouette(paths["front"])
s = vision.silhouette(paths["side"])
fx0, fy0, fx1, fy1 = f["bbox"]
sx0, sy0, sx1, sy1 = s["bbox"]
mm_per_px = 16 / (0.54 * (sx1 - sx0))  # the slot takes about 54 % of the left view's width
L = round((fx1 - fx0) * mm_per_px)
H = round((fy1 - fy0) * mm_per_px)
W = round((sx1 - sx0) * mm_per_px)
print("sizes from the picture:", L, W, H)
numbers = {"length": L, "width": W, "height": H, "x": [], "y": [16, (W - 16) / 2], "z": [10, H - 10],
           "diameters": [], "radii": []}
for with_side in (False, True):
    res = vision.reconstruct(paths["front"], paths["top"], numbers, side=paths["side"] if with_side else None,
                             construction_png=os.path.join(out, f"d_transfer{'_side' if with_side else ''}.png"))
    print("with left view" if with_side else "front + top only", res["notes"])
    for op in res["operations"]:
        print("  ", op.get("comment"), op["plane"], op.get("shapes")[-1].get("points", "")[:12])

if "--build" in sys.argv:
    sys.path.insert(0, os.path.join(HERE, ".."))
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect()
    b = k.build_part(res["operations"], name="пример д")
    print({key: b.get(key) for key in ("operations_done", "volume_mm3", "size_mm", "failed")})
    print(k.screenshot(os.path.join(out, "d_model.png")))
