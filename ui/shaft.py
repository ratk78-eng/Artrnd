"""Shafts and other turned parts in a vector drawing (DXF, CAD-made PDF): the body of revolution comes
from a longitudinal section — an axis line across it and hatched metal mirrored on both sides —
measured exactly in millimetres. What is not round (rack teeth, flats, a cut along the length) comes
from the plain view of the same part: whatever lies outside its outline is cut away through the
part, and slots drawn inside it (keyways) are cut in from the front by the depth the section shows.
"""
from __future__ import annotations

import numpy as np

import dxf

PPM = 20.0  # pixels per mm for measuring the section


def _axis(doc: dict, box: tuple) -> float | None:
    """y of a horizontal axis (centre line) running along most of the view, in its middle part."""
    x0, y0, x1, y1 = box
    best = None
    for e in doc["entities"]:
        if e["kind"] != "center" or e["type"] not in ("LINE", "LWPOLYLINE"):
            continue
        for (ax, ay), (bx, by) in dxf._segments(e, 15.0):
            if abs(ay - by) > 0.01 or not (y0 + 0.2 * (y1 - y0) < ay < y1 - 0.2 * (y1 - y0)):
                continue
            cover = min(max(ax, bx), x1) - max(min(ax, bx), x0)
            if cover >= 0.8 * (x1 - x0) and (best is None or cover > best[0]):
                best = (cover, ay)
    return best[1] if best else None


def _metal(doc: dict, box: tuple):
    """The hatched areas of a section as a boolean picture at PPM (rows top-down) with its frame."""
    from PIL import ImageDraw
    ctx = dxf._draw_main_lines(doc, box, PPM)
    if ctx is None:
        return None
    img, W, H, x0, y1 = ctx["img"], ctx["W"], ctx["H"], ctx["x0"], ctx["y1"]
    bx0, by0, bx1, by1 = box
    for (ax, ay), (bx, by) in dxf.hatch_segments(doc, box):
        for t in (0.25, 0.5, 0.75):
            mx, my = ax + t * (bx - ax), ay + t * (by - ay)
            if not (bx0 < mx < bx1 and by0 < my < by1):
                continue
            c, r = int((mx - x0) * PPM), int((y1 - my) * PPM)
            if 0 <= c < W and 0 <= r < H and img.getpixel((c, r)) == 255:
                keep = img.copy()
                ImageDraw.floodfill(img, (c, r), 60)
                if img.getpixel((0, 0)) == 60:
                    img = keep
    return {"metal": np.array(img) == 60, "x0": x0, "y1": y1, "W": W, "H": H}


def detect(doc: dict, box: tuple) -> dict | None:
    """A longitudinal section of a body of revolution? Returns its profile: z (mm from the left end),
    outer and inner radius per sample, the axis height and the section's length; None otherwise."""
    if box[2] - box[0] < 1.5 * (box[3] - box[1]):
        return None  # a turned part's section is drawn lying along its axis, longer than it is wide
    ay = _axis(doc, box)
    if ay is None:
        return None
    m = _metal(doc, box)
    if m is None or not m["metal"].any():
        return None
    metal, x0, y1 = m["metal"], m["x0"], m["y1"]
    row = int(round((y1 - ay) * PPM))
    n = min(row, metal.shape[0] - row - 1)
    if n < 10:
        return None
    up = metal[row - n:row][::-1]
    down = metal[row + 1:row + 1 + n]
    inter, union = (up & down).sum(), (up | down).sum()
    if union == 0 or inter / union < 0.7:
        return None
    # the metal stops half a stroke (1.5 px) short of each edge line: add it back
    half = 1.5 / PPM
    zs, ro, ri, ro_dn = [], [], [], []
    cols = np.nonzero(up.any(axis=0) | down.any(axis=0))[0]
    if not len(cols):
        return None
    c0, c1 = int(cols.min()), int(cols.max())
    for c in range(c0, c1 + 1):
        u = np.nonzero(up[:, c])[0]
        d = np.nonzero(down[:, c])[0]
        if not len(u) and not len(d):
            continue
        zs.append((c - c0 + 0.5) / PPM + half)
        ro.append((u.max() + 1) / PPM + half if len(u) else 0.0)
        ri.append(max(0.0, u.min() / PPM - half) if len(u) and u.min() > 3 else 0.0)
        ro_dn.append((d.max() + 1) / PPM + half if len(d) else 0.0)
    length = (c1 - c0 + 1) / PPM + 2 * half
    return {"z": zs, "r_out": ro, "r_in": ri, "r_out_other": ro_dn, "axis_y": ay, "length": length,
            "left": x0 + c0 / PPM - half, "symmetry": float(inter / union)}


def plain_view(doc: dict, views: list, sec_i: int, length: float) -> int | None:
    """The view showing the same part from outside: as long as the section, lying above or below it,
    not hatched."""
    s = views[sec_i]["box_mm"]
    best = None
    for i, v in enumerate(views):
        b = v.get("box_mm")
        if i == sec_i or not b or v.get("section"):
            continue
        if abs((b[2] - b[0]) - length) > 0.02 * length or abs(b[0] - s[0]) > 0.05 * length:
            continue
        d = abs((b[1] + b[3]) / 2 - (s[1] + s[3]) / 2)
        if best is None or d < best[0]:
            best = (d, i)
    return best[1] if best else None


def slots(doc: dict, view: dict, prof: dict, R: float) -> tuple[list, list]:
    """Closed areas drawn inside the plain view (keyways and the like): each cut in from the front by
    the depth the section shows on its near half (the half under the axis in a section seen from
    above). Returns (operations, notes)."""
    b = view["box_mm"]
    ay = _axis(doc, b)
    if ay is None:
        return [], []
    ops, notes = [], []
    zs = np.array(prof["z"])
    near = np.array(prof["r_out_other"])
    for f in dxf.faces(doc, b):
        if f["body"] or f["area"] < 2.0:
            continue
        bx0, by0, bx1, by1 = f["box"]
        z0, z1 = bx0 - b[0], bx1 - b[0]
        if z1 - z0 < 1.0:
            continue
        inner = (zs > z0 + 0.25 * (z1 - z0)) & (zs < z1 - 0.25 * (z1 - z0))
        if not inner.any():
            continue
        floor = float(np.median(near[inner]))
        if floor >= R - 0.1 or floor <= 0:
            notes.append(f"Контур на главном виде (X {z0:.1f}…{z1:.1f}) на разрезе не виден — не вырезан")
            continue
        fn = lambda p: (p[0] - b[0], p[1] - ay)  # noqa: E731
        ops.append({"op": "cut", "plane": "XY", "offset": R + 1, "depth": R + 1 - floor, "reverse": True,
                    "shapes": dxf.outline_shapes(f["outline"], fn),
                    "comment": f"паз X {z0:.1f}…{z1:.1f}, дно в {floor:.2f} мм от оси"})
        notes.append(f"Паз X {z0:.1f}…{z1:.1f} (ширина {by1 - by0:.1f}): глубина {R - floor:.2f} мм по разрезу")
    return ops, notes


def outside_cut(doc: dict, view: dict, R: float) -> dict | None:
    """What lies outside the plain view's outline, cut away through the part (rack teeth, a flat, a
    cut along the length): a sketch on the XY plane (x along the axis from the left end, y up from
    the axis), the body of revolution having been built on that plane about X."""
    b = view["box_mm"]
    ay = _axis(doc, b)
    out = view.get("outline")
    if ay is None or not isinstance(out, dict):
        return None
    fn = lambda p: (p[0] - b[0], p[1] - ay)  # noqa: E731
    shapes = dxf.outline_shapes(out, fn)
    L = b[2] - b[0]
    big = R + 3
    return {"op": "cut", "plane": "XY", "offset": -big, "through_all": True,
            "shapes": [{"type": "rect", "x": -2, "y": -big, "w": L + 4, "h": 2 * big}] + shapes,
            "comment": "всё, что вне контура главного вида (зубья, лыски)"}
