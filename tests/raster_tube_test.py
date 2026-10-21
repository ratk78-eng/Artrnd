"""Parts from a square tube drawn on PNG pictures (KOMPAS colour export, PACKTECH sheets): the
title block names the stock, the length and the features come from the views. Each drawing goes
through the app's own path (profile_raster.plan → _geometry → the review's checks); --build builds
them and compares the mass with the title block's (within 3 %), with no KOMPAS errors.
Names on the command line pick some of the drawings."""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402
import drawing  # noqa: E402
import profile_raster  # noqa: E402

FOLDER = os.path.join(HERE, "raster_tubes")
# the title block's mass, kg, and the length the dimensions give
MASS = {"25.80.02.233": (9.48, 2206), "25.200.07.401": (1.33, 309), "25.200.07.402": (4.02, 943),
        "25.200.07.403": (0.41, 123), "25.200.07.405": (1.31, 315), "25.200.07.407": (4.0, 943),
        "25.200.07.408": (0.41, 120), "25.200.07.415": (0.61, 143), "25.201.02.233": (9.47, 2200),
        "25.201.02.238": (1.72, 400), "25.201.02.249": (0.64, 148)}
names = [a for a in sys.argv[1:] if not a.startswith("--")] or sorted(MASS)
failed = []
for n in names:
    t0 = time.time()
    src = os.path.join(FOLDER, n + ".png")
    words = profile_raster.ocr(src)
    lines = profile_raster.lines_of(words)
    res = profile_raster.plan(src, lines=lines, ocr_words=words)
    if not res:
        print(n, "NO PLAN")
        failed.append(n)
        continue
    mass, L = MASS[n]
    a = {"id": n, "views": [], "reading": profile_raster.reading(res), "profile": {"path": src, "lines": lines}}
    a["values"] = {d["id"]: d["value"] for d in a["reading"]["dimensions"]}
    app._geometry(a)
    ops = a["reading"]["plan"]["operations"]
    issues = [i for i in drawing.check(a["reading"], a["values"]) if i["level"] == "error"]
    # the review window's data: one feature row per operation, every hole / slot / end marked on the sheet
    view = app._analysis_view(a)
    marked = {m["op"] for m in res["marks"] if m.get("op") is not None}
    far = sum("дальней стенке" in o.get("comment", "") for o in ops)
    print(n, res["stock"]["name"], "L", res["length"], "ops", len(ops), "mass in stamp", res["mass"],
          f"{time.time() - t0:.1f} s", issues or "")
    if res["stock"]["a"] != 50 or res["stock"]["t"] != 3 or abs(res["length"] - L) > 0.01 or issues \
            or res["mass"] != mass or len(view["features"]) != len(ops) or len(marked) + 1 + far != len(ops):
        failed.append(n)
        continue
    if "--build" in sys.argv:
        import kcheck
        body = json.dumps({"name": "build_part", "args": {"operations": drawing.resolve_plan(ops, a["values"]),
                                                          "name": n, "screenshot": False, "close": True,
                                                          "keep_going": True}}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8765/api/tool", body, {"Content-Type": "application/json"}), timeout=900))
        out = json.loads(r["text"])
        m = (out.get("volume_mm3") or 0) * 7.85e-6
        ok = abs(m / mass - 1) <= 0.03 and kcheck.check(out) and not out.get("failed") and not out.get("skipped")
        print(f"   built mass {m:.3f} kg vs {mass} ({100 * (m / mass - 1):+.1f} %)", out.get("size_mm"),
              "" if ok else f"FAILED {out.get('failed') or out.get('skipped') or ''}")
        if not ok:
            failed.append(n)
assert not failed, f"FAILED: {failed}"
print("RASTER TUBE TEST PASSED")
