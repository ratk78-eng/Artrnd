"""Task №13 rebuilt from its two views with only the numbers of the drawing: no language model."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import vision  # noqa: E402

front, top = sys.argv[1], sys.argv[2]
NUMBERS = {"length": 100, "width": 50, "height": 45, "x": [36, 42, 50, 82], "y": [50], "z": [10, 15, 20],
           "diameters": [10, 20], "radii": [12]}
res = vision.reconstruct(front, top, NUMBERS)
print("dashed:", json.dumps(res["dashed"], ensure_ascii=False))
print("notes:", *res["notes"], sep="\n  ")
print("questions:", res["questions"])
for op in res["operations"]:
    print(" ", op.get("comment"), {k: v for k, v in op.items() if k not in ("shapes", "comment")})

if "--build" in sys.argv:
    sys.path.insert(0, os.path.join(HERE, ".."))
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect()
    b = k.build_part(res["operations"], name="№13 по картинке")
    print({key: b.get(key) for key in ("operations_done", "operations_total", "volume_mm3", "size_mm", "failed")})
    for r in b["results"]:
        if "warning" in r:
            print("  warning", r)
    print(k.screenshot(os.path.join(os.environ["TEMP"], "kompasmcp-vision", "auto13b.png")))
