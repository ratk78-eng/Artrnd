"""A solid turned part (no bore) drawn as a hatched section beside its round face view: the
section's metal is one area across the axis — the half above it is turned (made up drawing:
Ø40 × 20 with a Ø30 × 10 step, 2 holes Ø6 on the face are not turned features). Checks
_turned_part's clipping at the axis; --build builds it (π·(20²·20 + 15²·10) = 32 201 mm³)."""
import math
import os
import shutil
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402

uid = uuid.uuid4().hex
folder = os.path.join(app.UPLOADS, uid)
os.makedirs(folder)
L = []


def poly(layer, pts, closed=True):
    L.extend(["0", "LWPOLYLINE", "8", layer, "90", str(len(pts)), "70", "1" if closed else "0"])
    for x, y in pts:
        L.extend(["10", f"{x:.4f}", "20", f"{y:.4f}"])


def text(x, y, t):
    L.extend(["0", "TEXT", "8", "TEXT", "10", f"{x}", "20", f"{y}", "40", "3.5", "50", "0", "1", t])


def circle(layer, x, y, r):
    L.extend(["0", "CIRCLE", "8", layer, "10", f"{x}", "20", f"{y}", "40", f"{r}"])


# the section: axis at y = 100, x 50…80 (Ø40 for 20, then Ø30 for 10)
outline = [(50, 80), (70, 80), (70, 85), (80, 85), (80, 115), (70, 115), (70, 120), (50, 120)]
poly("MAIN", outline)
for k in range(-40, 40, 2):   # hatching at 45° clipped to the outline's box roughly (inside the metal)
    x0, y0 = 50 + k, 80
    pts = []
    for t in range(0, 400):
        x, y = x0 + t * 0.1, y0 + t * 0.1
        inside = (50 < x < 70 and 80 < y < 120) or (70 <= x < 80 and 85 < y < 115)
        if inside:
            pts.append((x, y))
        elif pts:
            break
    if len(pts) > 10:
        poly("THIN", [pts[0], pts[-1]], closed=False)
# the face view: x 120…160, centre (140, 100)
circle("MAIN", 140, 100, 20)
circle("MAIN", 140, 100, 15)
for t, (x, y) in (("Ø40", (40, 125)), ("Ø30", (82, 118)), ("20", (58, 76)), ("30", (62, 70)), ("10", (73, 82))):
    text(x, y, t)
head = ["0", "SECTION", "2", "HEADER", "0", "ENDSEC", "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER"]
for name, lt in (("MAIN", "CONTINUOUS"), ("THIN", "CONTINUOUS"), ("TEXT", "CONTINUOUS")):
    head += ["0", "LAYER", "2", name, "70", "0", "6", lt]
head += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
with open(os.path.join(folder, "source.dxf"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(head + L + ["0", "ENDSEC", "0", "EOF"]) + "\n")
app._prepare_vector(os.path.join(folder, "source.dxf"), folder)
vec = app._vector_meta(os.path.join(folder, "source_vector.png"))
print("views", [(v["box_mm"], v["section"]) for v in vec["views"]], vec["numbers"])
tp = app._turned_part(os.path.join(folder, "source.dxf"), vec["views"], set(), vec["numbers"]["diameters"],
                      vec["numbers"]["values"])
assert tp, "no turned part"
print(tp["notes"])
ops = tp["operations"]
assert ops[0]["comment"].endswith("сплошная часть над осью"), [o["comment"] for o in ops]
ys = [p[1] for p in ops[0]["shapes"][0]["points"]]
assert min(ys) == 0.0 and abs(max(ys) - 20) < 0.01, ys
if "--build" in sys.argv:
    import json
    import urllib.request
    import kcheck
    body = json.dumps({"name": "build_part", "args": {"operations": ops, "name": "turned", "screenshot": False, "close": True,
                                                      "keep_going": True}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765/api/tool", body,
                                                                {"Content-Type": "application/json"}), timeout=900))
    res = json.loads(r["text"])
    vol = res.get("volume_mm3") or 0
    want = math.pi * (20 ** 2 * 20 + 15 ** 2 * 10)
    print("built", vol, "want", round(want, 1), res.get("size_mm"))
    assert abs(vol - want) < 0.01 * want and kcheck.check(res) and not res.get("failed"), res
print("TURNED SOLID TEST PASSED")
shutil.rmtree(folder, ignore_errors=True)
