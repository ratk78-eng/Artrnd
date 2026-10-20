"""Proof of concept on task №13: front-view profile and top-view holes taken from the picture itself."""
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import vision  # noqa: E402

out = os.path.join(os.environ["TEMP"], "kompasmcp-vision")
os.makedirs(out, exist_ok=True)
front, top = sys.argv[1], sys.argv[2]

for name, path in (("front", front), ("top", top)):
    s = vision.silhouette(path)
    print(name, "stroke runs 1..11 px:", s["stroke_hist"][1:], "bbox px:", s["bbox"])
    Image.fromarray((s["thick"] * 255).astype(np.uint8)).save(os.path.join(out, f"{name}_thick.png"))
    Image.fromarray((s["mask"] * 255).astype(np.uint8)).save(os.path.join(out, f"{name}_mask.png"))

def area(p):
    return abs(sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1] for i in range(len(p)))) / 2


f = vision.silhouette(front)
raw = vision.orthogonal_profile(f["mask"], f["bbox"], (100, 45))
print("front profile, raw (mm):", raw, "area", area(raw))
# numbers read from the drawing (what the model / the user confirmed): lengths and heights
xs = vision.candidates(100, [36, 42, 50, 82])
zs = vision.candidates(45, [10, 15, 20])
prof = vision.snap_profile(raw, xs, zs)
print("front profile, snapped:", prof)
print("profile area:", area(prof), "→ ×50 =", area(prof) * 50, "(reference body before holes: 113000)")
t = vision.silhouette(top)
holes = vision.find_circles(top, t["bbox"], (100, 50), [10, 20], min_score=0.75)
print("top holes (ring match, Ø from the drawing):", holes)

if "--build" in sys.argv:
    sys.path.insert(0, os.path.join(HERE, ".."))
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    ys = vision.candidates(50, [50])
    snap = lambda v, c: min(c, key=lambda q: abs(q - v))  # noqa: E731
    ops = [{"op": "extrude", "plane": "XZ", "depth": 50, "shapes": [{"type": "polyline", "points": prof}]},
           {"op": "cut", "plane": "XY", "through_all": True,
            "shapes": [{"type": "circle", "x": snap(h["x"], xs), "y": snap(h["y"], ys), "d": h["d"]} for h in holes]}]
    print("plan:", ops)
    k = Kompas()
    k.connect()
    res = k.build_part(ops, name="from picture")
    print({key: res.get(key) for key in ("operations_done", "volume_mm3", "size_mm")})
    print(k.screenshot(os.path.join(out, "auto13.png")))
