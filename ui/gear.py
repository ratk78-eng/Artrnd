"""Gear wheels: the transverse involute outline of a (helical) spur gear from its parameters.

A helical gear is cut with the standard rack in its normal section (normal module m, profile angle
α): in the transverse section (the face) the module is m/cos β and the profile angle αt with
tan αt = tan α / cos β; the teeth then follow a helix — each section turned by tan β / r per unit
of length, r the pitch radius (d = m z / cos β)."""
from __future__ import annotations

import math

MODULES = (0.05, 0.06, 0.08, 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.25, 1.5,
           2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0)   # ГОСТ 9563-60, ряд 1


def inv(a: float) -> float:
    return math.tan(a) - a


def params(m: float, z: int, beta_deg: float = 0.0, alpha_deg: float = 20.0, x: float = 0.0,
           ha: float = 1.0, c: float = 0.25, da: float | None = None) -> dict:
    """The transverse circles and angles. da: the tip diameter as drawn (it wins over the formula)."""
    beta, alpha = math.radians(beta_deg), math.radians(alpha_deg)
    mt = m / math.cos(beta)
    at = math.atan(math.tan(alpha) / math.cos(beta))
    r = mt * z / 2
    return {"m": m, "z": z, "beta": beta_deg, "alpha": alpha_deg, "x": x, "mt": mt, "alpha_t": at,
            "r": r, "rb": r * math.cos(at), "ra": (da / 2) if da else r + m * (ha + x),
            "rf": r - m * (ha + c - x),
            # tooth thickness on the pitch circle, transverse: half of it as an angle
            "psi": m * (math.pi / 2 + 2 * x * math.tan(alpha)) / math.cos(beta) / (2 * r)}


def module_from(da: float, z: int, beta_deg: float, x: float = 0.0, ha: float = 1.0) -> float | None:
    """The standard module that gives this tip diameter (within 3 %), or None."""
    k = z / math.cos(math.radians(beta_deg)) + 2 * (ha + x)
    m = da / k
    best = min(MODULES, key=lambda v: abs(v - m))
    return best if abs(best - m) <= 0.03 * best else None


def outline(p: dict, phase: float = 0.0, n_flank: int = 10) -> list[tuple]:
    """The whole transverse outline, counter-clockwise, as points round (0, 0); tooth k's middle at
    phase + 2πk/z. Involute flanks from the base circle (or the root, whichever is larger) to the
    tip, a radial line below the base circle, arcs at the tip and the root."""
    z, r, rb, ra, rf = p["z"], p["r"], p["rb"], p["ra"], p["rf"]
    at, psi = p["alpha_t"], p["psi"]

    def half(rho):   # half the tooth's angular width at radius rho
        a = math.acos(min(1.0, rb / rho))
        return psi + inv(at) - inv(a)
    r0 = max(rb, rf)
    flank = [r0 + (ra - r0) * (k / n_flank) ** 1.5 for k in range(n_flank + 1)]   # finer near the base
    pts = []
    for k in range(z):
        mid = phase + 2 * math.pi * k / z
        nxt = phase + 2 * math.pi * (k + 1) / z
        if rf < rb:
            pts.append(_pol(rf, mid - half(rb)))           # radial bit below the base circle
        for rho in flank:                                  # left flank, root to tip
            pts.append(_pol(rho, mid - half(rho)))
        w = half(ra)
        for t in (0.25, 0.5, 0.75):                        # tip arc
            pts.append(_pol(ra, mid - w + 2 * w * t))
        for rho in reversed(flank):                        # right flank, tip to root
            pts.append(_pol(rho, mid + half(rho)))
        if rf < rb:
            pts.append(_pol(rf, mid + half(rb)))
        a0 = mid + half(r0)
        a1 = nxt - half(r0)
        for t in (0.25, 0.5, 0.75):                        # root arc to the next tooth
            pts.append(_pol(rf, a0 + (a1 - a0) * t))
    # with very few teeth the flanks may cross below the tip: such a gear is undercut — not handled
    return pts


def outline_shapes(p: dict, phase: float = 0.0, cx: float = 0.0, cy: float = 0.0, n_flank: int = 8) -> list[dict]:
    """The same outline as sketch primitives round (cx, cy): each flank one spline through points of
    the involute, tip and root arcs, a radial line below the base circle — four or six pieces a
    tooth, so the operation through sections gets smooth faces (a polyline of hundreds of facets
    left KOMPAS edges it could not cut cleanly)."""
    z, rb, ra, rf = p["z"], p["rb"], p["ra"], p["rf"]
    at, psi = p["alpha_t"], p["psi"]

    def half(rho):
        return psi + inv(at) - inv(math.acos(min(1.0, rb / rho)))
    r0 = max(rb, rf)
    flank = [r0 + (ra - r0) * (k / n_flank) ** 1.3 for k in range(n_flank + 1)]
    P = lambda r, a: (cx + r * math.cos(a), cy + r * math.sin(a))  # noqa: E731

    def arc(r, a0, a1):   # counter-clockwise from a0 to a1
        (x1, y1), (x2, y2) = P(r, a0), P(r, a1)
        return {"type": "arc", "x": cx, "y": cy, "r": r, "a1": math.degrees(a0), "a2": math.degrees(a1),
                "x1": x1, "y1": y1, "x2": x2, "y2": y2}
    out = []
    for k in range(z):
        mid = phase + 2 * math.pi * k / z
        nxt = mid + 2 * math.pi / z
        if rf < rb:
            out.append({"type": "line", **dict(zip(("x1", "y1"), P(rf, mid - half(rb)))),
                        **dict(zip(("x2", "y2"), P(rb, mid - half(rb))))})
        out.append({"type": "spline", "points": [list(P(rho, mid - half(rho))) for rho in flank]})
        out.append(arc(ra, mid - half(ra), mid + half(ra)))
        out.append({"type": "spline", "points": [list(P(rho, mid + half(rho))) for rho in reversed(flank)]})
        if rf < rb:
            out.append({"type": "line", **dict(zip(("x1", "y1"), P(rb, mid + half(rb)))),
                        **dict(zip(("x2", "y2"), P(rf, mid + half(rb))))})
        out.append(arc(rf, mid + half(r0), nxt - half(r0)))
    return out


def tooth_shapes(p: dict, mid: float, cx: float = 0.0, cy: float = 0.0, r_in: float | None = None,
                 n_flank: int = 8) -> list[dict]:
    """One tooth as a closed contour, its middle at angle mid: the flanks (radial below the base
    circle) go down to r_in, a little inside the root circle, and an arc there closes it — the
    tooth is built on a cylinder of the root diameter and copied round. Six pieces only: KOMPAS
    matches them from section to section reliably, where with the whole outline (120 pieces) it
    paired a piece with its neighbour's partway along and the teeth got a kink."""
    rb, ra, rf = p["rb"], p["ra"], p["rf"]
    at, psi = p["alpha_t"], p["psi"]
    r_in = r_in if r_in is not None else rf - 0.2 * p["m"]

    def half(rho):
        return psi + inv(at) - inv(math.acos(min(1.0, rb / rho)))
    r0 = max(rb, rf)
    flank = [r0 + (ra - r0) * (k / n_flank) ** 1.3 for k in range(n_flank + 1)]
    P = lambda r, a: (cx + r * math.cos(a), cy + r * math.sin(a))  # noqa: E731

    def arc(r, a0, a1):
        (x1, y1), (x2, y2) = P(r, a0), P(r, a1)
        return {"type": "arc", "x": cx, "y": cy, "r": r, "a1": math.degrees(a0), "a2": math.degrees(a1),
                "x1": x1, "y1": y1, "x2": x2, "y2": y2}

    def line(a, b):
        return {"type": "line", "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}
    h0 = half(r0)
    return [line(P(r_in, mid - h0), P(r0, mid - h0)),
            {"type": "spline", "points": [list(P(rho, mid - half(rho))) for rho in flank]},
            arc(ra, mid - half(ra), mid + half(ra)),
            {"type": "spline", "points": [list(P(rho, mid + half(rho))) for rho in reversed(flank)]},
            line(P(r0, mid + h0), P(r_in, mid + h0)),
            # back along the inside, clockwise: an arc from the right foot to the left one
            arc(r_in, mid - h0, mid + h0)]


def _pol(r, a):
    return r * math.cos(a), r * math.sin(a)


def area(pts) -> float:
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2
