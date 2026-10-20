"""Bevel gears with circular (spiral) teeth from the drawing's parameter table (ГОСТ 2.405).

The tooth space is cut through sections. A point of the tooth surface is found the usual way of a
drawing office (Tredgold's approximation): at cone distance R along the pitch cone, in the plane
square to the cone's generatrix, the profile is the involute of the equivalent spur gear (radius
R·tan δ, transverse module mte·R/Re, transverse pressure angle from the local spiral angle), and
the space's middle lies on the tooth line — in the plane of the crown gear a circle of the cutter
head's radius (d0 / 2) through the mean point at the mean spiral angle βm; a crown angle θ turns
the gear by θ / sin δ. Tooth depths follow the drawing's tip and root cones.

The sections themselves lie in planes square to the gear's axis (X): each is the cut of that
tooth space with the plane, worked out point by point — KOMPAS sketches on such planes like on
any other offset plane. The space is cut longer than the teeth at both ends; whatever it takes
away beyond the toothed rim is given back by the rest of the blank, built after the teeth."""
from __future__ import annotations

import math

from gear import inv


def params(t: dict, tip_angle: float | None = None) -> dict:
    """The table as numbers the sections need. t: pdfvec.bevel_table(); tip_angle: the tip cone's
    angle drawn (degrees), else the root cone's of the mating gear (the same here: Σ 90°, 1 : 1)."""
    z = int(t["z"])
    delta = math.radians(t["delta"])
    beta = math.radians(t.get("beta") or 0.0)
    mn = t.get("mn") or t["mte"] * (t["R"] / t["Re"]) * math.cos(beta)
    Re = t.get("Re") or t["mte"] * z / (2 * math.sin(delta))
    Rm = t.get("R") or Re - 0.5 * (t.get("b") or 0.3 * Re)
    b = 2 * (Re - Rm)
    an = math.radians(20.0)                     # ГОСТ 16202-81 / 13754-81 basic rack
    xn, xt = t.get("x") or 0.0, t.get("xt") or 0.0
    theta_f = delta - math.radians(t["delta_f"]) if t.get("delta_f") else math.atan(1.25 * mn / Rm)
    theta_a = math.radians(tip_angle) - delta if tip_angle else theta_f
    ham, hfm = mn * (1 + xn), mn * (1.25 - xn)
    he = t.get("he")
    k = he / (ham + hfm + (Re - Rm) * (math.tan(theta_a) + math.tan(theta_f))) if he else 1.0
    return {"z": z, "delta": delta, "beta": beta, "mn": mn, "Re": Re, "Rm": Rm, "Ri": Re - b, "b": b,
            "an": an, "snm": mn * (math.pi / 2 + 2 * xn * math.tan(an) + xt),
            "ham": ham * k, "hfm": hfm * k, "theta_a": theta_a, "theta_f": theta_f,
            "r0": (t.get("d0") or 0.0) / 2, "hand": t.get("hand") or "right",
            "circular": t.get("type") == "circular" and (t.get("d0") or 0) > 0}


def tooth_line(p: dict):
    """θ(R): the space's middle on the crown gear (radians, 0 at the mean point) and β(R)."""
    Rm, bm, r0 = p["Rm"], p["beta"], p["r0"]
    if not p["circular"] or r0 <= 0:
        return (lambda R: 0.0), (lambda R: bm)
    cx, cy = Rm - r0 * math.sin(bm), r0 * math.cos(bm)     # the cutter's centre
    Rc2 = cx * cx + cy * cy

    def point(R):
        # the circle |X| = R and the cutter circle |X − C| = r0: the crossing near the mean point
        d = math.sqrt(Rc2)
        a = (R * R - r0 * r0 + Rc2) / (2 * d)
        h = math.sqrt(max(0.0, R * R - a * a))
        ux, uy = cx / d, cy / d
        px, py = a * ux, a * uy
        cands = [(px + h * uy, py - h * ux), (px - h * uy, py + h * ux)]
        return min(cands, key=lambda q: math.atan2(q[1], q[0]) ** 2)
    th0 = math.atan2(*point(Rm)[::-1])
    theta = lambda R: math.atan2(*point(R)[::-1]) - th0  # noqa: E731
    beta = lambda R: math.asin(max(-1.0, min(1.0, (R * R + r0 * r0 - Rc2) / (2 * R * r0))))  # noqa: E731
    return theta, beta


def space(p: dict, R: float, n_flank: int = 9, top: float = 0.8):
    """The tooth space at cone distance R in the plane square to the generatrix: points (u, h) —
    u along the pitch circle, h out of the pitch cone — as [left flank root→top, right flank
    top→root]; root and top close it with straight lines."""
    _, beta_of = tooth_line(p)
    bR = beta_of(R)
    at = math.atan(math.tan(p["an"]) / math.cos(bR))
    mt = p["mn"] / math.cos(p["beta"]) * R / p["Rm"]          # transverse module ∝ R
    rv = R * math.tan(p["delta"])                             # Tredgold's equivalent spur gear
    rb = rv * math.cos(at)
    st = p["snm"] * R / p["Rm"] / math.cos(bR)                 # transverse thickness at the pitch circle
    ha = max(0.15, p["ham"] + (R - p["Rm"]) * math.tan(p["theta_a"]))
    hf = max(0.15, p["hfm"] + (R - p["Rm"]) * math.tan(p["theta_f"]))
    half_pitch = math.pi * mt / (2 * rv)

    def gap_half(rho):   # half the space's angle at radius rho of the equivalent gear
        a = math.acos(min(1.0, rb / rho))
        return half_pitch - (st / (2 * rv) + inv(at) - inv(a))
    rho_f, rho_t = max(rv - hf, rb * 1.0001), rv + ha + top
    rhos = [rho_f + (rho_t - rho_f) * (k / n_flank) ** 1.2 for k in range(n_flank + 1)]
    left = [(-r * math.sin(gap_half(r)), r * math.cos(gap_half(r)) - rv) for r in rhos]
    right = [(-u, h) for u, h in reversed(left)]
    return left, right


def sections(p: dict, x_apex: float, n: int | None = None, ext: float | None = None) -> list[dict]:
    """The space's sections in planes X = const (the gear's axis is X, its apex at x_apex on the
    axis, the teeth growing towards +X): [{offset: X, shapes: [spline, line, spline, line]}] in the
    YZ plane's (Y, Z). The space is followed past both ends of the teeth by ext along the cone."""
    theta_of, _ = tooth_line(p)
    d, z = p["delta"], p["z"]
    sd, cd = math.sin(d), math.cos(d)
    sign = 1.0 if p["hand"] == "right" else -1.0
    # the turn grows outwards: a right-hand tooth turns clockwise seen from the apex (+X here)
    th_e = theta_of(p["Re"]) - theta_of(p["Ri"])
    flip = sign * (1.0 if th_e >= 0 else -1.0)
    if ext is None:
        ext = (p["ham"] + p["hfm"]) * 2.2 * math.tan(d) + 1.0
    R_lo, R_hi = p["Ri"] - ext, p["Re"] + ext
    n_pts = len(space(p, p["Rm"])[0])

    def point(R, side, k):
        left, right = space(p, R)
        u, h = (left if side == 0 else right)[k]
        x = x_apex + R * cd - h * sd
        rho = R * sd + h * cd
        phi = flip * theta_of(R) / sd
        ang = phi + math.atan2(u, rho)
        rad = math.hypot(rho, u)
        return x, rad * math.cos(ang), rad * math.sin(ang)

    def solve(X, side, k):
        lo, hi = R_lo, R_hi
        for _ in range(60):
            mid = (lo + hi) / 2
            if point(mid, side, k)[0] < X:
                lo = mid
            else:
                hi = mid
        return point((lo + hi) / 2, side, k)
    idx = [(0, k) for k in range(n_pts)] + [(1, k) for k in range(n_pts)]
    x_from = max(point(R_lo, s, k)[0] for s, k in idx)
    x_to = min(point(R_hi, s, k)[0] for s, k in idx)
    # sections every ~5° of the space's turn, 8…12 of them (35 brought KOMPAS down; with 16 the
    # rest of the blank would no longer join the teeth)
    turn = abs(theta_of(R_hi) - theta_of(R_lo)) / sd
    n = n or min(12, max(8, int(math.ceil(math.degrees(turn) / 5.0)) + 1))
    out = []
    for j in range(n):
        X = x_from + (x_to - x_from) * j / (n - 1)
        L = [solve(X, 0, k)[1:] for k in range(n_pts)]
        Rr = [solve(X, 1, k)[1:] for k in range(n_pts)]
        out.append({"offset": X, "shapes": [
            {"type": "spline", "points": [list(q) for q in L]},
            {"type": "line", "x1": L[-1][0], "y1": L[-1][1], "x2": Rr[0][0], "y2": Rr[0][1]},
            {"type": "spline", "points": [list(q) for q in Rr]},
            {"type": "line", "x1": Rr[-1][0], "y1": Rr[-1][1], "x2": L[0][0], "y2": L[0][1]}]})
    return out
