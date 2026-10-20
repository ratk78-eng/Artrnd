"""Checks which way extrude/cut go relative to the sketch plane (prints volume after each cut)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pythoncom
pythoncom.CoInitialize()
from kompas_api import Kompas

k = Kompas()
k.connect()
for label, offset, kw in [("z=0 through fwd", 0, dict(through_all=True)),
                          ("z=0 through rev", 0, dict(through_all=True, reverse=True)),
                          ("z=10 depth4 fwd", 10, dict()),
                          ("z=10 depth4 rev", 10, dict(reverse=True))]:
    k.new_part("cutdir")
    k.extrude(k.sketch("XY", [{"type": "rect", "x": 0, "y": 0, "w": 20, "h": 20}]), 20)
    k.extrude(k.sketch("XY", [{"type": "rect", "x": 5, "y": 5, "w": 10, "h": 10}], offset), 4, cut=True, **kw)
    print(label, k.mass_properties()["volume_mm3"])
# Expected (reverse=False means +Z): through fwd 6000, through rev 8000, depth4 at z=10 7600 both ways.
