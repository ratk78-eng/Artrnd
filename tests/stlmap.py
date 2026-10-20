"""Height map of a plate's front side from an STL (binary or text), curved surfaces included: every
triangle facing the front is rasterised, the one nearest the front wins (a z-buffer).
    python stlmap.py model.stl y 1|-1   →  model_hmap.npy (depth from the front face, mm, 0.1 mm grid)
The grid starts at the model's own minimum along the other two axes."""
import struct
import sys

import numpy as np


def read_stl(path):
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:400]:
        v = [list(map(float, l.split()[1:4])) for l in data.decode("latin-1").splitlines() if l.strip().startswith("vertex")]
        return np.array(v, dtype=np.float64).reshape(-1, 3, 3)
    n = struct.unpack("<I", data[80:84])[0]
    rec = np.frombuffer(data[84:84 + n * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    return rec["v"].astype(np.float64)


def height_map(tris, axis="y", sign=1.0, step=0.1):
    k = "xyz".index(axis)
    u, v = [i for i in range(3) if i != k]
    mn, mx = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    W = int(round((mx[u] - mn[u]) / step)) + 1
    H = int(round((mx[v] - mn[v]) / step)) + 1
    front = mx[k] if sign > 0 else mn[k]
    depth = np.full((H, W), np.inf)
    # triangle normals: keep those facing the front
    nrm = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    keep = nrm[:, k] * sign > 1e-12
    for t in tris[keep]:
        pu = (t[:, u] - mn[u]) / step
        pv = (mx[v] - t[:, v]) / step
        d = np.abs(front - t[:, k])
        c0, c1 = int(np.floor(pu.min())), int(np.ceil(pu.max()))
        r0, r1 = int(np.floor(pv.min())), int(np.ceil(pv.max()))
        c0, r0 = max(c0, 0), max(r0, 0)
        c1, r1 = min(c1, W - 1), min(r1, H - 1)
        if c1 < c0 or r1 < r0:
            continue
        cc, rr = np.meshgrid(np.arange(c0, c1 + 1), np.arange(r0, r1 + 1))
        x0, y0, x1, y1, x2, y2 = pu[0], pv[0], pu[1], pv[1], pu[2], pv[2]
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:
            continue
        a = ((y1 - y2) * (cc - x2) + (x2 - x1) * (rr - y2)) / den
        b = ((y2 - y0) * (cc - x2) + (x0 - x2) * (rr - y2)) / den
        c = 1 - a - b
        inside = (a >= -1e-6) & (b >= -1e-6) & (c >= -1e-6)
        if not inside.any():
            continue
        z = a * d[0] + b * d[1] + c * d[2]
        sub = depth[r0:r1 + 1, c0:c1 + 1]
        upd = inside & (z < sub)
        sub[upd] = z[upd]
    depth[np.isinf(depth)] = np.nan
    return depth


if __name__ == "__main__":
    path, axis, sign = sys.argv[1], sys.argv[2], float(sys.argv[3])
    m = height_map(read_stl(path), axis, sign)
    np.save(path.rsplit(".", 1)[0] + "_hmap.npy", m)
    print("grid", m.shape, "unknown", int(np.isnan(m).sum()))
