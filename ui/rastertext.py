"""Numbers and dimensions read off a drawing picture (PNG/JPG, a scan) without any model.

CAD programs write their sheets in a drafting font (ГОСТ 2.304 type A in KOMPAS): its glyphs are
known, so each character on the sheet is matched against the same glyphs drawn from the font file
(GOST_AU.ttf / GOST_A.TTF ship with KOMPAS). Characters standing in a row make a word; a word of
digits is a number. A number sits on its dimension line; the line ends at two extension lines —
their positions on the sheet, with the number, are what the views are measured by (a long part is
drawn with breaks, so its views are not to scale along their length).

Upright and vertical (turned 90°, read upwards) texts are both read."""
from __future__ import annotations

import math
import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import revolve   # label(): connected components without scipy

FONT_FILES = ("GOST_AU.ttf", "GOST_A.TTF", "GOST_BU.ttf", "GOST_B.TTF")
CHARS = "0123456789,*±-+()R×"
NORM = (20, 30)          # width × height a glyph is compared at


def _font_path() -> str | None:
    for d in (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")):
        for f in FONT_FILES:
            p = os.path.join(d, f)
            if os.path.exists(p):
                return p
    return None


def _norm(bitmap: np.ndarray) -> np.ndarray:
    """A glyph's ink (bool) cropped to its box and brought to NORM (aspect kept, centred)."""
    ys, xs = np.nonzero(bitmap)
    if not len(ys):
        return np.zeros(NORM[::-1], dtype=float)
    b = bitmap[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = b.shape
    W, H = NORM
    k = min(W / w, H / h)
    im = Image.fromarray((b * 255).astype(np.uint8)).resize((max(1, round(w * k)), max(1, round(h * k))), Image.BILINEAR)
    out = np.zeros((H, W), dtype=float)
    a = np.asarray(im) / 255.0
    oy, ox = (H - a.shape[0]) // 2, (W - a.shape[1]) // 2
    out[oy:oy + a.shape[0], ox:ox + a.shape[1]] = a
    # strokes of any weight alike: the glyph's centre lines, spread out a little
    s = out > 0.3
    sk = s.copy()
    for _ in range(3):                            # thin to ~1 px by peeling (cheap, no skeleton)
        e = sk.copy()
        e[1:, :] &= sk[:-1, :]
        e[:-1, :] &= sk[1:, :]
        e[:, 1:] &= sk[:, :-1]
        e[:, :-1] &= sk[:, 1:]
        if e.sum() < 0.3 * sk.sum():
            break
        sk = e
    f = sk.astype(float)
    for _ in range(2):                            # blur: 3×3 box twice
        p = np.pad(f, 1)
        f = sum(p[dy:dy + H, dx:dx + W] for dy in range(3) for dx in range(3)) / 9.0
    n = np.linalg.norm(f)
    return f / n if n else f


_TEMPLATES: list | None = None


def templates() -> list:
    """[(char, normalised glyph, aspect h/w, height relative to a digit)] from the drafting font,
    drawn at a few stroke weights (screens and prints differ)."""
    global _TEMPLATES
    if _TEMPLATES is not None:
        return _TEMPLATES
    path = _font_path()
    out = []
    if path:
        size = 96
        font = ImageFont.truetype(path, size)
        digit_h = None
        for ch in CHARS:
            # upright, and slanted the way ГОСТ 2.304 writes (75°) and a little either side
            for shear in (0.0, 0.21, 0.27, 0.33):
                im = Image.new("L", (size * 2, size * 2), 0)
                ImageDraw.Draw(im).text((size // 2, size // 2), ch, fill=255, font=font, stroke_width=1, stroke_fill=255)
                if shear:
                    im = im.transform(im.size, Image.AFFINE, (1, shear, -shear * size, 0, 1, 0), Image.BILINEAR)
                a = np.asarray(im) > 128
                ys, xs = np.nonzero(a)
                if not len(ys):
                    continue
                h, w = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
                if ch == "0" and not shear:
                    digit_h = h
                out.append((ch, _norm(a), h / w, h))
        if digit_h:
            out = [(c, g, asp, h / digit_h) for c, g, asp, h in out]
            # the square sign before a square's side (□50): a square as tall as a digit's body
            for shear in (0.0, 0.27):
                im = Image.new("L", (200, 200), 0)
                ImageDraw.Draw(im).rectangle([40, 60, 110, 130], outline=255, width=6)
                if shear:
                    im = im.transform(im.size, Image.AFFINE, (1, shear, -shear * 100, 0, 1, 0), Image.BILINEAR)
                a = np.asarray(im) > 128
                out.append(("□", _norm(a), 1.0, 0.75))
    _TEMPLATES = out
    return out


def classify(bitmap: np.ndarray, rel_h: float) -> tuple[str, float]:
    """The character a glyph's ink is most like, and how alike (0…1). rel_h: its height against
    the line's digit height (a comma is small, a minus flat)."""
    g = _norm(bitmap)
    ys, xs = np.nonzero(bitmap)
    asp = (ys.max() - ys.min() + 1) / (xs.max() - xs.min() + 1) if len(ys) else 1.0
    best, score = "?", 0.0
    for ch, t, t_asp, t_h in templates():
        s = float((g * t).sum())                  # cosine of the blurred centre lines
        s *= math.exp(-abs(math.log(max(asp, 0.05) / max(t_asp, 0.05))) * 0.8)
        s *= math.exp(-abs(math.log(max(rel_h, 0.05) / max(t_h, 0.05))) * 1.2)
        if s > score:
            best, score = ch, s
    return best, score


def _components(mask: np.ndarray):
    lab, n = revolve.label(mask)
    if n <= 1:
        return lab, []
    ys, xs = np.nonzero(lab)
    ids = lab[ys, xs]
    x0 = np.full(n, 1 << 30)
    y0 = np.full(n, 1 << 30)
    x1 = np.full(n, -1)
    y1 = np.full(n, -1)
    cnt = np.zeros(n, dtype=int)
    np.minimum.at(x0, ids, xs)
    np.minimum.at(y0, ids, ys)
    np.maximum.at(x1, ids, xs)
    np.maximum.at(y1, ids, ys)
    np.add.at(cnt, ids, 1)
    comps = [{"id": i, "x0": int(x0[i]), "y0": int(y0[i]), "x1": int(x1[i]) + 1, "y1": int(y1[i]) + 1, "n": int(cnt[i])}
             for i in range(1, n) if cnt[i] > 0]
    return lab, comps


def text_ink(rgb: np.ndarray) -> np.ndarray:
    """Dark, colourless ink: thin lines and texts (main lines and axes are coloured on a CAD
    export; on a black-and-white sheet this is all the ink)."""
    a = rgb.astype(np.int16)
    gray = a.mean(axis=2)
    sat = a.max(axis=2) - a.min(axis=2)
    return (gray < 150) & (sat < 90)


def read_words(rgb: np.ndarray, mask: np.ndarray | None = None) -> list[dict]:
    """Words on the sheet: {text, x0, y0, x1, y1, vertical, score, chars:[...]} in pixels.
    mask: the ink to read (text_ink by default)."""
    if mask is None:
        mask = text_ink(rgb)
    lab, comps = _components(mask)
    if not comps:
        return []
    # characters: small blobs; their usual height is the digits' height
    hs = np.array([c["y1"] - c["y0"] for c in comps])
    ws = np.array([c["x1"] - c["x0"] for c in comps])
    small = (hs < 0.05 * max(mask.shape)) & (ws < 0.05 * max(mask.shape)) & (np.maximum(hs, ws) >= 5)
    if not small.any():
        return []
    words = []
    for vertical in (False, True):
        # in a vertical word the characters' long side runs across the line
        tall = np.where(vertical, ws, hs)
        cand = [c for c, ok, t in zip(comps, small, tall) if ok]
        if not cand:
            continue
        # the digits' height: the commonest height of digit-shaped blobs (taller than wide)
        Ls = []
        for c in cand:
            L = (c["x1"] - c["x0"]) if vertical else (c["y1"] - c["y0"])
            Wd = (c["y1"] - c["y0"]) if vertical else (c["x1"] - c["x0"])
            if L >= 12 and 1.2 <= L / max(Wd, 1) <= 3.5:
                Ls.append(L // 2)
        if not Ls:
            continue
        H = int(np.bincount(Ls).argmax()) * 2 + 1
        if H < 12:
            continue
        chars = []
        for c in cand:
            L = (c["x1"] - c["x0"]) if vertical else (c["y1"] - c["y0"])
            if L > 1.6 * H:
                continue
            bmp = lab[c["y0"]:c["y1"], c["x0"]:c["x1"]] == c["id"]
            if vertical:
                bmp = np.rot90(bmp, -1)          # read upwards: turned back clockwise
            ch, s = classify(bmp, L / H)
            if s < 0.35:
                continue
            chars.append({**c, "ch": ch, "score": s, "L": L})
        # characters in a row: along the line (x, or −y for vertical text), close, on one baseline
        key_along = (lambda c: -c["y1"]) if vertical else (lambda c: c["x0"])
        chars.sort(key=key_along)
        used = set()
        for i, c in enumerate(chars):
            if i in used:
                continue
            word = [c]
            used.add(i)
            for j in range(i + 1, len(chars)):
                if j in used:
                    continue
                d = chars[j]
                last = word[-1]
                if vertical:
                    gap = last["y0"] - d["y1"]
                    off = abs((d["x0"] + d["x1"]) / 2 - (last["x0"] + last["x1"]) / 2)
                    base_ok = abs(d["x1"] - last["x1"]) < 0.5 * H or d["L"] < 0.8 * H
                else:
                    gap = d["x0"] - last["x1"]
                    off = abs((d["y0"] + d["y1"]) / 2 - (last["y0"] + last["y1"]) / 2)
                    base_ok = abs(d["y1"] - last["y1"]) < 0.5 * H or d["L"] < 0.8 * H
                if off >= 0.8 * H:
                    continue                         # another line
                # a comma tucks in under the digit before it (italic font)
                low = -0.45 * H if d["L"] < 0.5 * H else -0.2 * H
                if low <= gap <= 0.7 * H and base_ok:
                    word.append(d)
                    used.add(j)
                elif gap > 3 * H:
                    break
            text = "".join(w["ch"] for w in word)
            words.append({"text": text, "vertical": vertical, "H": H,
                          "x0": min(w["x0"] for w in word), "y0": min(w["y0"] for w in word),
                          "x1": max(w["x1"] for w in word), "y1": max(w["y1"] for w in word),
                          "score": float(np.mean([w["score"] for w in word])), "chars": word})
    return words


def dimensions(rgb: np.ndarray, words: list[dict], min_score: float = 0.7) -> list[dict]:
    """Linear dimensions: a number with the two extension lines its dimension line runs between.
    {value, star, prefix, vertical, a, b (sheet px along the dimension: x for a horizontal one,
    y for a vertical one), line (px across: the dimension line's y / x), word}. The extension
    lines are the thin lines crossing the dimension line, standing out from it towards the view;
    the pair round the number (or, for a small one written beside its arrows, the nearest pair)."""
    ink = text_ink(rgb) | ((rgb.astype(np.int16).mean(axis=2) < 150))
    h, w = ink.shape
    out = []
    for wd in words:
        num = number_of(wd)
        # judged by the number itself: a tolerance in small digits reads worse
        if not num or num["score"] < min_score:
            continue
        H = wd["H"]
        v = wd["vertical"]
        m = ink.T if v else ink          # work as if horizontal: rows run along the dimension
        if v:
            # text read upwards stands left of its dimension line: the line is to its right
            lo, hi = wd["x1"], min(w, wd["x1"] + int(1.0 * H))
            along0, along1 = wd["y0"], wd["y1"]
        else:
            lo, hi = wd["y1"], min(h, wd["y1"] + int(1.0 * H))
            along0, along1 = wd["x0"], wd["x1"]
        c = (along0 + along1) / 2
        best = None
        for r in range(lo, hi):
            row = m[r]
            # the run of ink through this row reaching under the text
            seg = row[max(0, int(along0) - 2):int(along1) + 3]
            if seg.mean() < 0.85:
                continue
            a = int(along0)
            while a > 0 and (row[a - 1] or (a > 2 and row[a - 2] and row[a - 3])):
                a -= 1
            b = int(along1)
            while b < len(row) - 1 and (row[b + 1] or (b + 3 < len(row) and row[b + 2] and row[b + 3])):
                b += 1
            if best is None or (b - a) > best[2] - best[1]:
                best = (r, a, b)
        if best is None:
            continue
        r, a, b = best
        # extension lines: columns (rows of m.T) through the line with ink beyond it on the side
        # away from the text, at least a text height long
        ext = []
        reach = int(1.2 * H)
        for x in range(max(0, a - 3), min(m.shape[1], b + 4)):
            col = m[:, x]
            below = col[r + 2:min(m.shape[0], r + 2 + reach)]
            above = col[max(0, r - reach):max(0, r - 1)]
            if (len(below) and below.mean() > 0.9) or (len(above) and above.mean() > 0.9 and len(below) and below[:3].any()):
                ext.append(x)
        # merge neighbouring columns of one line
        lines = []
        for x in ext:
            if lines and x - lines[-1][-1] <= 3:
                lines[-1].append(x)
            else:
                lines.append([x])
        xs = [sum(g) / len(g) for g in lines]
        if len(xs) < 2:
            continue
        left = [x for x in xs if x <= c]
        right = [x for x in xs if x >= c]
        if left and right:
            pa, pb = max(left), min(right)
        else:
            # a small dimension written outside its arrows: the closest pair of lines
            pairs = sorted(((abs((p + q) / 2 - c), p, q) for p, q in zip(xs, xs[1:])))
            _, pa, pb = pairs[0]
        out.append({**num, "vertical": v, "a": pa, "b": pb, "line": r, "word": wd})
    return out


NUM_RE = re.compile(r"^(?P<pre>[ØR□]?)(?P<num>\d+(?:,\d+)?)(?P<rest>.*)$")


def number_of(word: dict) -> dict | None:
    """The value a word writes — «276,5», «943-1» (the −1 is the tolerance in small digits),
    «120±0,3», «50*» — as {value, star, tol}. Small characters after the number (subscripts)
    are its tolerance."""
    H = max(c["L"] for c in word["chars"])
    chars = word["chars"]
    pre, i = "", 0
    if chars and chars[0]["ch"] in "ØR□":
        pre, i = chars[0]["ch"], 1
    num = ""
    while i < len(chars):
        c = chars[i]
        if c["ch"].isdigit() and c["L"] >= 0.85 * H:
            num += c["ch"]
        elif c["ch"] == "," and num and "," not in num and i + 1 < len(chars) and chars[i + 1]["ch"].isdigit():
            num += ","
        else:
            break                                   # ±, a tolerance in small digits, a star…
        i += 1
    if not num or num.endswith(","):
        return None
    rest = "".join(c["ch"] for c in chars[i:])
    score = float(np.mean([c["score"] for c in chars[:i] if c["ch"] != ","]))
    return {"value": float(num.replace(",", ".")), "star": "*" in rest, "prefix": pre, "text": word["text"],
            "score": score}
