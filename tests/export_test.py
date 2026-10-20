"""Exports really produce the requested format (not a native KOMPAS file under a foreign name)."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pythoncom; pythoncom.CoInitialize()
from kompas_api import Kompas

k = Kompas(); k.connect(launch=False)
d = tempfile.mkdtemp(prefix="kompasmcp-export-")
head = lambda p: open(p, "rb").read(16)  # noqa: E731
ok = True

def check(name, cond):
    global ok
    print(("OK  " if cond else "FAIL") + " " + name)
    ok &= bool(cond)

k.new_fragment(False)
k.draw_2d([{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 45}, {"type": "circle", "x": 50, "y": 20, "d": 20}])
dxf = k.export(os.path.join(d, "f.dxf")); check("fragment -> dxf", b"SECTION" in head(dxf))
dwg = k.export(os.path.join(d, "f.dwg")); check("fragment -> dwg", head(dwg).startswith(b"AC10"))
try:
    pdf = k.export(os.path.join(d, "f.pdf")); check("fragment -> pdf", head(pdf).startswith(b"%PDF"))
except Exception as e:
    check(f"fragment -> pdf ({e})", False)
frw = k.convert(dxf, os.path.join(d, "back.frw")); check("dxf -> frw", head(frw).startswith(b"PK"))
dxf2 = k.convert(frw, os.path.join(d, "back.dxf")); check("frw -> dxf", b"SECTION" in head(dxf2))
dxf3 = k.convert(dwg, os.path.join(d, "from_dwg.dxf")); check("dwg -> dxf", b"SECTION" in head(dxf3))

k.new_part("export_test")
k.build_part([{"op": "extrude", "plane": "XY", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 20, "h": 10}], "depth": 5}], new=False)
step = k.export(os.path.join(d, "p.step")); check("part -> step", head(step).startswith(b"ISO-10303"))
stl = k.export(os.path.join(d, "p.stl")); check("part -> stl", not head(stl).startswith(b"PK"))
print("EXPORT TEST", "PASSED" if ok else "FAILED", d)
