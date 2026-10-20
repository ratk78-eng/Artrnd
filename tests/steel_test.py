"""Steel detail drawings (Tekla КМД, DXF): profile from the specification, cuts and holes from the
views. Usage: python tests/steel_test.py <dir with w1.dxf … w7.dxf> [--build]
With --build each part is built in KOMPAS and its mass compared with the specification."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
sys.path.insert(0, os.path.join(HERE, ".."))
import dxf  # noqa: E402
import steel  # noqa: E402

folder = sys.argv[1]
build = "--build" in sys.argv
files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".dxf"))
k = None
if build:
    import pythoncom
    pythoncom.CoInitialize()
    from kompas_api import Kompas
    k = Kompas()
    k.connect(launch=False)
ok = True
for f in files:
    doc = dxf.read_dxf(os.path.join(folder, f))
    st = steel.analyze(doc)
    if st is None:
        print(f"== {f}: не распознан как металлоконструкция")
        ok = False
        continue
    rd = steel.reading(st)
    values = {d["id"]: d["value"] for d in rd["dimensions"]}
    res = steel.plan(st, values)
    print(f"== {f}: {st['spec']['profile']['name']}  views={len(st['views'])}  "
          f"values={values}  mass(spec)={st['spec'].get('mass_kg')}")
    for n in res["notes"]:
        print("   •", n)
    for q in res["questions"]:
        print("   ?", q["text"])
    for i in res["issues"]:
        print("   !", i["text"])
    if k:
        r = k.build_part(res["operations"], f, True, None)
        k.close_current()   # test parts are closed without saving
        vol = r.get("volume_mm3") or 0
        m_kg = vol * steel.STEEL_DENSITY
        spec = st["spec"].get("mass_kg")
        # the specification gives the blank's mass (profile × L): the section must match it within 3 %,
        # and the built part — with its cuts and holes — must weigh a bit less, never more
        gross = next((float(n.split("Масса по сечению без вырезов ")[1].split(" кг")[0])
                      for n in res["notes"] if n.startswith("Масса по сечению")), None)
        size = r.get("size_mm") or [0, 0, 0]
        want = [values["L"], values.get("b", values.get("a", 0)), values.get("h", values.get("b", 0))] \
            if st["spec"]["profile"]["kind"] != "plate" else [values["L"], values["b"], values["t"]]
        good = (r.get("failed") is None and spec and gross and abs(gross - spec) <= 0.03 * spec
                and 0.5 * gross <= m_kg <= gross + 0.01 and all(abs(a - b) <= 0.05 for a, b in zip(size, want))
                and __import__("kcheck").check(r))
        ok &= bool(good)
        print(f"   built: ops {r.get('operations_done')}  size {size} (want {want})  "
              f"mass {m_kg:.2f} kg (blank {gross}, spec {spec})  {'OK' if good else 'CHECK'}  failed={r.get('failed')}")
        warn = [o for o in r.get("operations", []) if isinstance(o, dict) and o.get("warning")]
        for w in warn:
            print("   warn:", w.get("warning"))
print("STEEL TEST", "PASSED" if ok else "FAILED")
