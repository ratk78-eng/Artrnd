"""Where a built plate differs from the reference, curved surfaces included (STL height maps):
    python hmap_compare.py ref.stl ref_sign model.stl model_sign [v][h] out.png
picture: reference | model | difference (red — the model is cut deeper, blue — material left over,
the stronger the colour the bigger the difference, full at 1 mm); prints the volumes."""
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stlmap  # noqa: E402

ref, rs, mod, ms, flip, out = sys.argv[1:7]
a = stlmap.height_map(stlmap.read_stl(ref), "y", float(rs))
b = stlmap.height_map(stlmap.read_stl(mod), "y", float(ms))
if "v" in flip:
    b = b[::-1]
if "h" in flip:
    b = b[:, ::-1]
h = min(a.shape[0], b.shape[0])
w = min(a.shape[1], b.shape[1])
a, b = a[:h, :w], b[:h, :w]
np.save(out.rsplit(".", 1)[0] + "_ref.npy", a)
np.save(out.rsplit(".", 1)[0] + "_mod.npy", b)


def col(m):
    o = np.zeros(m.shape + (3,), np.uint8)
    v = np.clip(np.nan_to_num(m) / 6.0, 0, 1)
    o[..., 0] = o[..., 1] = (255 * (1 - v)).astype(np.uint8)
    o[..., 2] = 255
    o[np.isnan(m)] = (255, 200, 0)
    return o
both = ~np.isnan(a) & ~np.isnan(b)
dd = np.where(both, b - a, 0.0)
s = np.clip(np.abs(dd) / 1.0, 0, 1)
diff = np.full((h, w, 3), 255, np.uint8)
red, blue = both & (dd > 0.02), both & (dd < -0.02)
diff[red, 1] = diff[red, 2] = (255 * (1 - s[red])).astype(np.uint8)
diff[blue, 0] = diff[blue, 1] = (255 * (1 - s[blue])).astype(np.uint8)
gap = np.full((h, 20, 3), 255, np.uint8)
img = np.concatenate([col(a), gap, col(b), gap, diff], axis=1)
Image.fromarray(img).resize((img.shape[1] // 2, h // 2)).save(out)
print("model deeper:", round(float(dd[red].sum()) * 0.01, 1), "mm3 over", round(float(red.sum()) * 0.01, 1), "mm2")
print("material left:", round(float(-dd[blue].sum()) * 0.01, 1), "mm3 over", round(float(blue.sum()) * 0.01, 1), "mm2")
