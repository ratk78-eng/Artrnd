"""Where a built plate differs from the reference: depth maps of both front sides (stepmap.py) and a
picture — reference | model | difference (red: the model is cut deeper, blue: material left over,
yellow: the reference is not flat there — fillets, round grooves — and is not compared).

    python depth_compare.py ref.step ref_sign model.step model_sign [v][h] out.png

sign: 1 when the front face looks along +Y, -1 along -Y; v/h flip the model's map to line it up."""
import os
import subprocess
import sys

import numpy as np
from PIL import Image

ref, rs, mod, ms, flip, out = sys.argv[1:7]
here = os.path.dirname(os.path.abspath(__file__))
for path, sign in ((ref, rs), (mod, ms)):
    subprocess.run([sys.executable, os.path.join(here, "stepmap.py"), path, "y", sign], check=True)
a = np.load(ref.rsplit(".", 1)[0] + "_depth.npy")
b = np.load(mod.rsplit(".", 1)[0] + "_depth.npy")
if "v" in flip:
    b = b[::-1]
if "h" in flip:
    b = b[:, ::-1]


def col(m):
    o = np.zeros(m.shape + (3,), np.uint8)
    v = np.clip(np.nan_to_num(m) / 6.0, 0, 1)
    o[..., 0] = o[..., 1] = (255 * (1 - v)).astype(np.uint8)
    o[..., 2] = 255
    o[np.isnan(m)] = (255, 200, 0)
    return o
h, w = a.shape
both = ~np.isnan(a) & ~np.isnan(b)
dd = np.where(both, b - a, 0)
diff = np.full((h, w, 3), 255, np.uint8)
diff[both & (dd > 0.05)] = (220, 0, 0)
diff[both & (dd < -0.05)] = (0, 0, 220)
diff[np.isnan(a)] = (255, 200, 0)
gap = np.full((h, 20, 3), 255, np.uint8)
img = np.concatenate([col(a), gap, col(b), gap, diff], axis=1)
Image.fromarray(img).resize((img.shape[1] // 2, h // 2)).save(out)
print("deeper than the reference:", round(float((both & (dd > 0.05)).sum()) * 0.01, 1), "mm2,",
      round(float(dd[both & (dd > 0.05)].sum()) * 0.01, 1), "mm3")
print("material left over:", round(float((both & (dd < -0.05)).sum()) * 0.01, 1), "mm2,",
      round(float(-dd[both & (dd < -0.05)].sum()) * 0.01, 1), "mm3")
