"""Drawing analysis: cut the views out of a drawing sheet, check what a model read from it against
the rules of engineering graphics, and evaluate a parametric build plan.

The model never builds from a picture directly. It fills a structure — dimensions with ids, and a
build_part plan whose numbers are expressions over those ids — which the user can correct field by
field before anything is built.
"""
from __future__ import annotations

import ast
import contextlib
import math
import operator
import os
import re

import numpy as np
from PIL import Image

# ---------------- view segmentation ----------------


def _box_filter_max(mask: np.ndarray, r: int) -> np.ndarray:
    """Binary dilation by a (2r+1)² square, done with cumulative sums (no scipy here)."""
    if r <= 0:
        return mask
    m = mask.astype(np.int32)
    c = np.pad(m, ((1, 0), (0, 0))).cumsum(0)
    rows = c[np.minimum(np.arange(m.shape[0]) + r + 1, m.shape[0])] - c[np.maximum(np.arange(m.shape[0]) - r, 0)]
    c = np.pad(rows, ((0, 0), (1, 0))).cumsum(1)
    cols = c[:, np.minimum(np.arange(m.shape[1]) + r + 1, m.shape[1])] - c[:, np.maximum(np.arange(m.shape[1]) - r, 0)]
    return cols > 0


def _label(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Connected components (4-neighbour) → [(x0, y0, x1, y1, pixel_count)]."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    boxes = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        x0 = x1 = sx
        y0 = y1 = sy
        n = 0
        while stack:
            y, x = stack.pop()
            n += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        boxes.append((x0, y0, x1 + 1, y1 + 1, n))
    return boxes


def _merge(boxes, gap: int):
    """Joins boxes that overlap or nearly touch: dimension lines and numbers next to a view
    belong to it."""
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] - gap <= b[2] and b[0] - gap <= a[2] and a[1] - gap <= b[3] and b[1] - gap <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]]
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return [tuple(b) for b in boxes]


def _run_lengths(mask: np.ndarray) -> np.ndarray:
    """For every pixel of the mask, the length of the horizontal run of mask pixels it lies in."""
    out = np.zeros(mask.shape, dtype=np.int32)
    for i, row in enumerate(mask):
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
            out[i, s:e] = e - s
    return out


def _main_colour(rgb: np.ndarray, ink: np.ndarray) -> np.ndarray | None:
    """KOMPAS (and most CAD) exports a sheet in colour: the main lines in one colour (blue here),
    thin lines black, axes another colour. When a third of the ink or more is one saturated hue,
    that hue is the main lines."""
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    sat = (mx - mn) > 90
    col = ink & sat
    if col.sum() < 0.25 * ink.sum():
        return None
    r, g, b = (rgb[..., i].astype(float) for i in range(3))
    hue = (np.degrees(np.arctan2(math.sqrt(3) * (g - b), 2 * r - g - b)) + 360) % 360
    hist = np.bincount((hue[col] // 10).astype(int), minlength=36)
    top = int(hist.argmax())
    if hist[top] + hist[(top - 1) % 36] + hist[(top + 1) % 36] < 0.6 * col.sum():
        return None
    centre = top * 10 + 5
    dh = np.abs((hue - centre + 180) % 360 - 180)
    return col & (dh <= 20)


def _main_line_views(path: str, max_side: int = 1400) -> list[tuple] | None:
    """The views of a sheet found by their main (thick) lines — boxes (full-size pixels) of the
    thick lines of each view and, padded, of its dimensions. Dimension lines and notes are thin:
    on a busy sheet they glued everything into one blob when all the ink was spread out together
    (Art's tube drawings: no view at all on 9 sheets of 11). The frame, the title block and the
    designation box are thick too but lie against the frame: they are left out."""
    img = Image.open(path).convert("RGB")
    # shrunk by whole blocks keeping each block's darkest value per colour: a line stays unbroken
    # (resampling turned thin lines into dotted grey ones)
    f = max(1, int(math.ceil(max(img.size) / max_side)))
    a = np.asarray(img).astype(np.int16)
    H0, W0 = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    rgb = a[:H0, :W0].reshape(H0 // f, f, W0 // f, f, 3).min(axis=(1, 3))
    k = 1.0 / f
    gray = rgb.mean(axis=2)
    ink = gray < 175
    h, w = ink.shape
    if ink.sum() < 200:
        return None
    thick = _main_colour(rgb, ink)
    if thick is None:
        # no colour of its own for the main lines (a print, a scan): they are the thicker strokes
        hr = _run_lengths(ink)
        vr = _run_lengths(ink.T).T
        stroke = np.minimum(hr, vr)                 # a line's width across it (its length along it)
        widths = stroke[ink]
        widths = widths[(widths > 0) & (widths <= 12)]
        if not len(widths):
            return None
        thin = max(1, int(np.bincount(widths).argmax()))
        if thin < 3:
            # thin lines one or two pixels wide (a page rendered small): bold text and tables are
            # as "thick" as the main lines — no telling them apart, the views are found by the ink
            return None
        thick = ink & (stroke >= max(2, round(1.6 * thin)))
    if thick.sum() < 50:
        return None
    # the frame: rows / columns with an unbroken line across most of the sheet (a long view's edge
    # with another view beside it covers as much, but in pieces)
    def longest(m):
        best = np.zeros(m.shape[0], dtype=int)
        for i, row in enumerate(m):
            dd = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
            s, e = np.nonzero(dd == 1)[0], np.nonzero(dd == -1)[0]
            best[i] = (e - s).max() if len(s) else 0
        return best
    bridged = _box_filter_max(thick, 1)
    rows = np.nonzero(longest(bridged) > 0.7 * w)[0]
    cols = np.nonzero(longest(bridged.T) > 0.7 * h)[0]
    fx0 = cols[cols < w / 2].max() if len(cols[cols < w / 2]) else 0
    fx1 = cols[cols > w / 2].min() if len(cols[cols > w / 2]) else w - 1
    fy0 = rows[rows < h / 2].max() if len(rows[rows < h / 2]) else 0
    fy1 = rows[rows > h / 2].min() if len(rows[rows > h / 2]) else h - 1
    near = max(3, int(0.006 * max(h, w)))
    inner = np.zeros_like(thick)
    inner[fy0 + near:fy1 - near, fx0 + near:fx1 - near] = True
    core = thick & inner
    span = max(h, w)
    # letters are thick too where the font is big (notes, view labels, the roughness sign): only
    # pieces longer than a letter are lines of a view
    keep = np.zeros_like(core)
    lab = np.zeros(core.shape, dtype=np.int32)
    pieces = _label(_box_filter_max(core, 1))
    for x0, y0, x1, y1, n in pieces:
        if max(x1 - x0, y1 - y0) >= 0.03 * span:
            keep[y0:y1, x0:x1] |= core[y0:y1, x0:x1]
    core = keep
    del lab
    # a view's pieces (a break, holes inside its outline) come together; separate views stay apart
    gap = max(3, int(0.012 * max(h, w)))
    blobs = _merge(_label(_box_filter_max(core, gap // 2)), gap)
    out = []
    for x0, y0, x1, y1, n in blobs:
        bw, bh = x1 - x0, y1 - y0
        if max(bw, bh) < 0.06 * span or n < 40:
            continue                                # a roughness sign, a letter, a stray mark
        # the title block, the designation box: against the frame
        touches = x0 <= fx0 + 2 * near or x1 >= fx1 - 2 * near or y0 <= fy0 + 2 * near or y1 >= fy1 - 2 * near
        if touches:
            continue
        # the lines themselves, not their spread-out blob
        ys, xs = np.nonzero(core[y0:y1, x0:x1])
        if len(ys):
            x0, y0, x1, y1 = x0 + xs.min(), y0 + ys.min(), x0 + xs.max() + 1, y0 + ys.max() + 1
        out.append((x0, y0, x1, y1))
    if not out:
        return None
    # each view takes the thin ink around it (dimensions, its label) up to halfway to its neighbours
    pad = int(0.06 * span)
    res = []
    for i, (x0, y0, x1, y1) in enumerate(out):
        X0, Y0, X1, Y1 = max(fx0 + near, x0 - pad), max(fy0 + near, y0 - pad), min(fx1 - near, x1 + pad), min(fy1 - near, y1 + pad)
        for j, (a0, b0, a1, b1) in enumerate(out):
            if j == i:
                continue
            if b0 < y1 and y0 < b1:                 # side by side
                if a0 >= x1:
                    X1 = min(X1, (x1 + a0) // 2)
                elif a1 <= x0:
                    X0 = max(X0, (a1 + x0) // 2)
            if a0 < x1 and x0 < a1:                 # one over the other
                if b0 >= y1:
                    Y1 = min(Y1, (y1 + b0) // 2)
                elif b1 <= y0:
                    Y0 = max(Y0, (b1 + y0) // 2)
        res.append((int(X0 / k), int(Y0 / k), int(X1 / k), int(Y1 / k), (int(x0 / k), int(y0 / k), int(x1 / k), int(y1 / k))))
    return res


def segment_views(path: str, out_dir: str, max_views: int = 6) -> list[dict]:
    """The views of a sheet: by their main lines (_main_line_views), else by all the ink spread out
    (_segment_by_ink, the older way)."""
    try:
        found = _main_line_views(path)
    except Exception:  # noqa: BLE001
        found = None
    if not found or len(found) > 12:
        return _segment_by_ink(path, out_dir, max_views)
    found.sort(key=lambda b: -(b[4][2] - b[4][0]) * (b[4][3] - b[4][1]))
    found = found[:max(max_views, 8)]
    with Image.open(path) as im:
        full = im.convert("RGB")
    # reading order: rows of views whose heights overlap, left to right in a row
    found.sort(key=lambda b: b[4][1])
    rows, bottom = [], None
    for b in found:
        if bottom is None or b[4][1] > bottom:
            rows.append([])
            bottom = b[4][3]
        rows[-1].append(b)
        bottom = max(bottom, b[4][3])
    found = [b for r in rows for b in sorted(r, key=lambda b: b[4][0])]
    os.makedirs(out_dir, exist_ok=True)
    views = []
    for i, (x0, y0, x1, y1, core) in enumerate(found, 1):
        crop = full.crop((x0, y0, x1, y1))
        name = f"view{i}.png"
        crop.save(os.path.join(out_dir, name))
        views.append({"index": i, "file": name, "box": (x0, y0, x1, y1), "size": crop.size, "core": core})
    return views


def _segment_by_ink(path: str, out_dir: str, max_views: int = 6) -> list[dict]:
    """Finds the separate drawing views on a sheet and saves each as viewN.png.

    Ink is dilated so that a view, its dimension lines and numbers merge into one blob, while the
    gaps between views stay open. Frame and title-block lines (long straight runs across the sheet)
    are removed first, otherwise they would glue everything together."""
    img = Image.open(path).convert("L")
    scale = 600 / max(img.size)
    small = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    ink = np.asarray(small) < 160
    h, w = ink.shape
    # remove frame lines: rows/columns that are mostly ink over a long run. Thin frame lines shrunk
    # to 600 px come out light grey, so they are looked for with a lighter threshold, and the
    # neighbouring row/column goes too (a line falls between two rows after resizing)
    faint = np.asarray(small) < 215

    def longest(a):
        best = np.zeros(a.shape[0], dtype=int)
        for i, row in enumerate(a):
            d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
            s, e = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
            best[i] = (e - s).max() if len(s) else 0
        return best
    long_rows = (ink.sum(1) > 0.85 * w) | (longest(faint) > 0.85 * w)
    long_cols = (ink.sum(0) > 0.85 * h) | (longest(faint.T) > 0.85 * h)
    # title block and table lines run from the frame half across the sheet: they glue the views
    # to the title block, so they go too — only lines that start or end on a frame line
    frame_x = np.nonzero(long_cols)[0]
    if len(frame_x):
        for y in range(h):
            if long_rows[y]:
                continue
            row = faint[y]
            d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
            for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
                if e - s > 0.5 * w and (np.abs(frame_x - s).min() <= 3 or np.abs(frame_x - (e - 1)).min() <= 3):
                    long_rows[y] = True
                    break
    long_rows = long_rows | np.roll(long_rows, 1) | np.roll(long_rows, -1)
    long_cols = long_cols | np.roll(long_cols, 1) | np.roll(long_cols, -1)
    ink = ink.copy()
    ink[long_rows, :] = False
    ink[:, long_cols] = False
    blobs = _merge(_label(_box_filter_max(ink, max(2, int(0.012 * max(h, w))))), int(0.02 * max(h, w)))
    area = h * w
    blobs = [b for b in blobs if (b[2] - b[0]) * (b[3] - b[1]) > 0.01 * area and b[4] > 30]
    # a title block / huge table touching the frame corner is not a view
    blobs = [b for b in blobs if (b[2] - b[0]) * (b[3] - b[1]) < 0.7 * area]
    blobs.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    blobs = sorted(blobs[:max_views], key=lambda b: (round(b[1] / (0.25 * h)), b[0]))
    os.makedirs(out_dir, exist_ok=True)
    full = Image.open(path).convert("RGB")
    views = []
    for i, (x0, y0, x1, y1, _) in enumerate(blobs, 1):
        pad = int(0.01 * max(full.size))
        box = (max(0, int(x0 / scale) - pad), max(0, int(y0 / scale) - pad),
               min(full.width, int(x1 / scale) + pad), min(full.height, int(y1 / scale) + pad))
        crop = full.crop(box)
        name = f"view{i}.png"
        crop.save(os.path.join(out_dir, name))
        views.append({"index": i, "file": name, "box": box, "size": crop.size})
    return views


# ---------------- reading request ----------------

_NUM = {"type": ["number", "string"]}
_SHAPE = {"type": "object", "properties": {
    "type": {"type": "string", "enum": ["rect", "circle", "polyline", "line", "arc"]},
    "x": _NUM, "y": _NUM, "w": _NUM, "h": _NUM, "d": _NUM, "r": _NUM, "a1": _NUM, "a2": _NUM,
    "x1": _NUM, "y1": _NUM, "x2": _NUM, "y2": _NUM,
    "points": {"type": "array", "items": {"type": "array", "items": _NUM, "minItems": 2, "maxItems": 2}},
}, "required": ["type"]}
READING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "views": {"type": "array", "items": {"type": "object", "properties": {
            "image": {"type": "integer"}, "name": {"type": "string", "enum": ["главный", "сверху", "слева", "справа", "снизу", "сзади", "разрез", "другое"]}},
            "required": ["image", "name"]}},
        "dimensions": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "value": {"type": "number"}, "view": {"type": "string"},
            "meaning": {"type": "string"},
            "axis": {"type": "string", "enum": ["length", "width", "height", "diameter", "radius", "other"]},
            "overall": {"type": "boolean"}, "confidence": {"type": "number"}},
            "required": ["id", "value", "view", "meaning", "axis", "overall", "confidence"]}},
        "overall": {"type": "object", "properties": {"length": _NUM, "width": _NUM, "height": _NUM}},
        "chains": {"type": "array", "items": {"type": "object", "properties": {
            "parts": {"type": "array", "items": {"type": "string"}}, "total": {"type": "string"}},
            "required": ["parts", "total"]}},
        "mass_kg": {"type": ["number", "null"]},
        "features": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "object", "properties": {
            "operations": {"type": "array", "items": {"type": "object", "properties": {
                "op": {"type": "string", "enum": ["extrude", "cut", "revolve"]},
                "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]},
                "offset": _NUM, "depth": _NUM, "reverse": {"type": "boolean"}, "through_all": {"type": "boolean"},
                "comment": {"type": "string"},
                "shapes": {"type": "array", "items": _SHAPE}},
                "required": ["op", "plane", "shapes"]}}},
            "required": ["operations"]},
    },
    "required": ["views", "dimensions", "overall", "features", "plan"],
}

READING_PROMPT = """Ты читаешь машиностроительный чертёж (ЕСКД, проецирование «первого угла»: вид сверху под главным, вид слева справа от главного).
Тебе даны вырезанные виды чертежа (изображения пронумерованы с 1) и при необходимости весь лист.

Сделай строго по шагам и верни ТОЛЬКО JSON по схеме:
1. views: для каждого изображения — какой это вид.
2. dimensions: КАЖДОЕ размерное число на чертеже — отдельной записью: id (d1, d2, …), value (число как написано), view, meaning (что размер задаёт, коротко: «длина детали», «ширина паза сверху», «диаметр отверстия», «высота основания»), axis (length — вдоль X/длины, width — Y/ширины, height — Z/высоты, diameter, radius, other), overall (true — габаритный), confidence (0…1 — насколько уверенно прочитана цифра).
3. overall: габариты length/width/height — ссылками на id размеров (например "d1").
4. chains: цепочки размеров, которые должны складываться: {"parts": ["d3","d4","d3"], "total": "d1"}.
5. features: элементы формы по-русски, по одному в строке («основание 100×50×20», «паз сверху 36×10», «сквозное отверстие Ø20 в центре»).
6. plan.operations: план построения для инструмента build_part. ВСЕ числа — выражениями через id размеров ("d1", "d2/2", "d1-d5"); голые числа допустимы только 0.
   Оси: X — длина (горизонталь главного вида), Y — глубина (вертикаль вида сверху), Z — высота (вертикаль главного вида). Деталь стоит в углу: X от 0, Y от 0, Z от 0.
   Плоскости: XY — эскиз в координатах (X, Y) на высоте offset; XZ — профиль главного вида в координатах (X, Z), выдавливается на глубину по +Y; YZ — (Y, Z), по +X.
   Удобный приём: внешний контур главного вида — одна ломаная (polyline) на XZ, выдавить на ширину детали; затем вырезы и отверстия на XY.
   Вырез вниз с верхней грани высоты H: plane XY, offset H, reverse true. Сквозное отверстие: through_all true.
Не придумывай размеров, которых нет на чертеже: если размер нужен, но не указан — выведи его из других (выражением) и поставь confidence ниже.
Пример фрагмента: {"op":"extrude","plane":"XZ","depth":"d2","shapes":[{"type":"polyline","points":[[0,0],["d1",0],["d1","d3"],[0,"d3"]]}]}"""


PLAN_PROMPT = """Ты составляешь план построения 3D-детали для инструмента build_part. Размеры уже прочитаны с чертежа и ПОДТВЕРЖДЕНЫ человеком — используй только их, по id.
Верни ТОЛЬКО JSON: {"overall": {"length": id, "width": id, "height": id}, "plan": {"operations": [...]}}.
Все числа в операциях — выражения через id ("d1", "(d1-d4)/2", "d2-d5"); голые числа допустимы только 0.
Оси: X — длина (горизонталь главного вида), Y — глубина (вертикаль вида сверху), Z — высота. Деталь стоит в углу: X, Y, Z от 0.
Плоскости: XY — эскиз (X, Y) на высоте offset; XZ — профиль главного вида (X, Z), выдавливается по +Y на depth; YZ — (Y, Z), по +X.
Надёжный приём: весь внешний контур главного вида (с выступами, пазами сверху и каналами снизу) — одна замкнутая ломаная polyline на XZ, выдавить на ширину детали. Затем отверстия и местные вырезы на XY.
Вырез вниз от грани на высоте H: plane XY, offset H, reverse true, depth — глубина. Сквозное: through_all true. Окружность: {"type":"circle","x":…,"y":…,"d":…}.

Подтверждённые размеры:
{dims}

Элементы формы (как их понял читающий; могут быть неточны):
{features}
{note}"""

PLAN_SCHEMA = {"type": "object", "properties": {
    "overall": READING_SCHEMA["properties"]["overall"],
    "plan": READING_SCHEMA["properties"]["plan"]}, "required": ["overall", "plan"]}


def plan_messages(reading: dict, values: dict, view_images: list, note: str = "") -> list[dict]:
    reading = without_excluded(reading)
    dims = "\n".join(f"{d['id']} = {values.get(d['id'], d.get('value'))} — {d.get('meaning', '')} ({d.get('view', '')}, ось {d.get('axis', '')})"
                     for d in reading.get("dimensions", []))
    feats = "\n".join(f"- {f}" for f in reading.get("features", []))
    text = PLAN_PROMPT.replace("{dims}", dims).replace("{features}", feats or "-").replace(
        "{note}", f"\nУказание пользователя: {note}" if note else "")
    content = [{"type": "text", "text": text}]
    for i, item in enumerate(view_images, 1):
        num, url = item if isinstance(item, tuple) else (i, item)
        content += [{"type": "text", "text": f"Вид {num}:"}, {"type": "image_url", "image_url": {"url": url}}]
    return [{"role": "user", "content": content}]


# Reading only: the geometry now comes from the picture itself (vision.reconstruct), the model just
# has to read the numbers and say what they are.
READ_DIMS_SCHEMA = {**READING_SCHEMA, "properties": {k: v for k, v in READING_SCHEMA["properties"].items() if k != "plan"},
                    "required": ["views", "dimensions", "overall", "features"]}
READ_DIMS_PROMPT = READING_PROMPT.split("6. plan.operations")[0] + \
    "Плана построения НЕ нужно: геометрию программа возьмёт с самих видов. Главное — прочитать ВСЕ числа без пропусков (Ø, R, «2 отв.»)."


# Cross-check: when the user writes a comment, a vector drawing the program has read by itself is
# also read by the language model, which is shown what the program found; the two are compared
CROSS_SCHEMA = {**READ_DIMS_SCHEMA,
                "properties": {**READ_DIMS_SCHEMA["properties"],
                               "review": {"type": "array", "items": {"type": "string"}},
                               "gear": {"type": "object", "properties": {
                                   "m": {"type": ["number", "null"]}, "z": {"type": ["integer", "null"]},
                                   "beta": {"type": ["number", "null"]}, "hand": {"type": ["string", "null"]},
                                   "alpha": {"type": ["number", "null"]}, "x": {"type": ["number", "null"]}}}},
                "required": READ_DIMS_SCHEMA["required"] + ["review"]}


def cross_prompt(summary: str) -> str:
    return (READ_DIMS_PROMPT + "\n\n7. review: программа уже распознала этот чертёж сама, вот что она нашла:\n"
            + summary + "\nСначала прочитай чертёж сам (пункты 1–5), потом сравни со сводкой программы и перечисли "
            "в review по-русски, по одному пункту в строке, где программа, по-твоему, ошиблась или что упустила: "
            "размеры, габариты, отверстия, элементы формы (зубья, резьба, шпоночный паз, фаски, скругления). "
            "Учитывай пояснение пользователя. Если расхождений нет — пустой список.\n"
            "8. gear: если на листе есть таблица параметров зубчатого колеса — перепиши из неё числа: m (модуль), "
            "z (число зубьев), beta (угол наклона зубьев, градусы, 0 для прямозубого), hand (направление линии "
            "зуба: \"right\" — правое, \"left\" — левое), alpha (угол профиля, градусы), x (коэффициент смещения). "
            "Нет таблицы — gear не заполняй.")


def program_summary(numbers: dict, reading: dict, values: dict, notes: list) -> str:
    """What the program recognised, as text for the model."""
    lines = []
    if numbers:
        lines.append(f"Габариты: длина X {numbers.get('length')}, ширина Y {numbers.get('width')}, "
                     f"высота Z {numbers.get('height')} мм")
    dims = [f"{d['id']}={values.get(d['id'], d.get('value'))} ({d.get('axis')})" for d in reading.get("dimensions", [])]
    if dims:
        lines.append("Размеры с чертежа: " + ", ".join(dims))
    lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


def compare_readings(numbers: dict, reading: dict, values: dict, llm: dict) -> list[dict]:
    """Where the model's own reading differs from the program's: overall sizes, numbers one of them
    did not see, and the model's remarks on the program's recognition. Issues for the review window."""
    out = []
    lv = {d.get("id"): d.get("value") for d in llm.get("dimensions", []) if isinstance(d.get("value"), (int, float))}
    for axis, name in (("length", "длина X"), ("width", "ширина Y"), ("height", "высота Z")):
        ref = (llm.get("overall") or {}).get(axis)
        v = lv.get(ref) if isinstance(ref, str) else ref if isinstance(ref, (int, float)) else None
        p = (numbers or {}).get(axis)
        if isinstance(v, (int, float)) and isinstance(p, (int, float)) and abs(v - p) > max(0.2, 0.02 * p):
            out.append({"level": "warning", "dims": [], "text": f"Сверка с моделью ИИ — габарит {name}: программа {p:g} мм, "
                        f"модель {v:g} мм"})
    prog = [float(values.get(d["id"], d.get("value"))) for d in reading.get("dimensions", [])
            if isinstance(values.get(d["id"], d.get("value")), (int, float))]
    near = lambda v, pool: any(abs(v - q) <= max(0.05, 0.005 * abs(v)) for q in pool)  # noqa: E731
    seen = [float(d["value"]) for d in llm.get("dimensions", []) if isinstance(d.get("value"), (int, float))]
    only_llm = sorted({v for v in seen if not near(v, prog)})
    only_prog = sorted({v for v in prog if not near(v, seen)})
    if only_llm:
        out.append({"level": "warning", "dims": [], "text": "Сверка с моделью ИИ — модель прочитала числа, которых нет "
                    "у программы: " + ", ".join(f"{v:g}" for v in only_llm)})
    if only_prog:
        out.append({"level": "info", "dims": [], "text": "Сверка с моделью ИИ — модель не назвала числа, которые "
                    "программа взяла из файла: " + ", ".join(f"{v:g}" for v in only_prog)})
    for r in llm.get("review") or []:
        r = _tidy_text(r)
        if r:
            out.append({"level": "warning", "dims": [], "text": "Модель ИИ: " + r})
    if not out:
        out.append({"level": "ok", "dims": [], "text": "Сверка с моделью ИИ: расхождений с распознаванием программы нет"})
    return out


def numbers_from_dims(reading: dict, values: dict) -> dict | None:
    """Turns the confirmed dimensions into what vision.reconstruct needs: overall sizes and the
    numbers along each axis (for snapping), diameters and radii."""
    dims = reading.get("dimensions", [])
    val = lambda d: values.get(d["id"], d.get("value"))  # noqa: E731
    out = {"x": [], "y": [], "z": [], "diameters": [], "radii": []}
    axis_key = {"length": "x", "width": "y", "height": "z"}
    for d in dims:
        v = val(d)
        if not isinstance(v, (int, float)) or v != v:  # NaN = empty field
            continue
        a = d.get("axis")
        if a in axis_key:
            out[axis_key[a]].append(float(v))
            if d.get("overall"):
                out[{"x": "length", "y": "width", "z": "height"}[axis_key[a]]] = float(v)
        elif a == "diameter":
            out["diameters"].append(float(v))
        elif a == "radius":
            out["radii"].append(float(v))
    for axis, key in (("length", "x"), ("width", "y"), ("height", "z")):
        if axis not in out:
            if not out[key]:
                return None
            out[axis] = max(out[key])  # no dimension marked overall: the largest one along the axis
    return out


def excluded_views(reading: dict) -> set[int]:
    """0-based indices of the views the user marked «исключить из расчёта»."""
    return {v.get("image") - 1 for v in reading.get("views", []) or []
            if isinstance(v, dict) and v.get("name") == "исключить" and isinstance(v.get("image"), int)}


def without_excluded(reading: dict) -> dict:
    """The reading without dimensions taken from excluded views (they count for nothing)."""
    off = {i + 1 for i in excluded_views(reading)}
    if not off:
        return reading
    keep = []
    for d in reading.get("dimensions", []):
        m = re.search(r"(?:вид|изображени\w*|image)\s*(\d+)", str(d.get("view", "")), re.I)
        if not (m and int(m.group(1)) in off):
            keep.append(d)
    return {**reading, "dimensions": keep}


def pick_side(reading: dict, n_images: int, boxes: list | None, front: int) -> int | None:
    """Index of the left view crop: named so by the model, or (first-angle layout) the crop to the
    right of the main view at the same height."""
    names = {v.get("image"): v.get("name") for v in reading.get("views", [])}
    skip = excluded_views(reading)
    side = next((i - 1 for i, n in names.items() if n == "слева"), None)
    if side is not None and 0 <= side < n_images and side != front:
        return side
    if boxes and front < len(boxes):
        fx0, fy0, fx1, fy1 = boxes[front]
        best = None
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            if i in skip:
                continue
            overlap = min(y1, fy1) - max(y0, fy0)
            if i != front and x0 >= fx1 - 5 and overlap > 0.6 * min(y1 - y0, fy1 - fy0):
                if best is None or x0 < boxes[best][0]:
                    best = i
        return best
    return None


def pick_views(reading: dict, n_images: int, boxes: list | None = None) -> tuple[int, int] | None:
    """Indices (0-based) of the front and top view crops: as the model named them, otherwise by the
    first-angle layout — the top view lies under the main view, aligned with it horizontally."""
    names = {v.get("image"): v.get("name") for v in reading.get("views", [])}
    skip = excluded_views(reading)
    kept = [i for i in range(n_images) if i not in skip]
    front = next((i - 1 for i, n in names.items() if n == "главный"), None)
    top = next((i - 1 for i, n in names.items() if n == "сверху"), None)
    if (front is None or top is None or front == top) and len(kept) >= 2:
        front, top = kept[0], kept[1]
        if boxes and len(boxes) == n_images:
            pairs = []
            for i, (fx0, fy0, fx1, fy1) in enumerate(boxes):
                for j, (x0, y0, x1, y1) in enumerate(boxes):
                    if i in skip or j in skip:
                        continue
                    overlap = min(x1, fx1) - max(x0, fx0)
                    if i != j and y0 >= fy1 - 5 and overlap > 0.6 * min(x1 - x0, fx1 - fx0):
                        pairs.append((fy0, fx0, i, j))
            if pairs:
                _, _, front, top = min(pairs)  # the upper-left main view with a view under it
    if front is None or top is None or not (0 <= front < n_images and 0 <= top < n_images):
        return None
    return front, top


def reading_messages(view_images: list, sheet_image: str | None, note: str = "",
                     prompt: str | None = None) -> list[dict]:
    """Chat messages for the reading request: prompt + numbered view crops (+ the whole sheet).
    view_images: urls numbered 1… in order, or (number, url) pairs to keep the sheet's numbering
    when some views are left out."""
    content = [{"type": "text", "text": (prompt or READ_DIMS_PROMPT) + (f"\n\nПояснение пользователя: {note}" if note else "")}]
    for i, item in enumerate(view_images, 1):
        num, url = item if isinstance(item, tuple) else (i, item)
        content += [{"type": "text", "text": f"Изображение {num}:"}, {"type": "image_url", "image_url": {"url": url}}]
    if sheet_image:
        content += [{"type": "text", "text": "Весь лист целиком (для взаимного расположения видов):"},
                    {"type": "image_url", "image_url": {"url": sheet_image}}]
    return [{"role": "user", "content": content}]


def parse_reading(text: str) -> dict:
    """Takes the JSON object out of a model reply: tolerates ```json fences, text around it and
    several JSON fragments (as in a reasoning text) — the largest object with "dimensions" or
    "plan" wins."""
    import json
    dec = json.JSONDecoder()
    best = None
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, end = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("dimensions" in obj or "plan" in obj):
            if best is None or end - m.start() > best[1]:
                best = (obj, end - m.start())
    if best is None:
        best = (_salvage_dimensions(text or ""), 0)
    if not best[0]:
        raise ValueError("модель не вернула JSON")
    data = best[0]
    dims = []
    for d in data.get("dimensions", []):
        d["id"] = str(d.get("id", "")).strip()
        d["view"] = _tidy_view(d.get("view"))
        d["meaning"] = _tidy_text(d.get("meaning"))
        dims += _split_value(d)
    if "dimensions" in data:
        data["dimensions"] = dims
    data["views"] = _tidy_views(data.get("views"))
    return data


def _salvage_dimensions(text: str) -> dict | None:
    """A reply cut off in the middle (a small model copying a whole table of Ø into "dimensions"
    runs out of tokens): every complete dimension object before the cut is kept, with the views
    if they came first. None when there is nothing to keep."""
    import json
    m = re.search(r'"dimensions"\s*:\s*\[', text)
    if not m:
        return None
    dec = json.JSONDecoder()
    dims, i = [], m.end()
    while True:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(text, j)
        except json.JSONDecodeError:
            break                                   # the cut-off object: stop here
        if isinstance(obj, dict) and "value" in obj:
            dims.append(obj)
        i = end
        if text[i:].lstrip().startswith("]"):
            break
    if not dims:
        return None
    out = {"dimensions": dims}
    v = re.search(r'"views"\s*:\s*(\[[^\]]*\])', text[:m.start()])
    if v:
        with contextlib.suppress(json.JSONDecodeError):
            out["views"] = json.loads(v.group(1))
    return out


def _tidy_views(views) -> list[dict]:
    """Views as [{image, name}]. Models also answer ["главный", "сверху"] (names in image order) or
    [{"image": "Изображение 2", ...}]; anything else is dropped rather than breaking the analysis."""
    out = []
    for k, v in enumerate(views if isinstance(views, list) else [], 1):
        if isinstance(v, str):
            v = {"image": k, "name": v}
        if not isinstance(v, dict):
            continue
        img = v.get("image")
        if not isinstance(img, int):
            m = re.search(r"\d+", str(img or ""))
            img = int(m.group()) if m else k
        out.append({"image": img, "name": str(v.get("name") or "другое").strip().lower()})
    return out


def _split_value(d: dict) -> list[dict]:
    """Models copy table cells as text: "Ø12,92→Ø10,07", "Ø14 / 7,39", "15,91". Such a value
    becomes one number per entry: numbers marked Ø are diameters, the others keep the axis."""
    v = d.get("value")
    if isinstance(v, (int, float)) or v is None:
        return [d]
    nums = re.findall(r"(Ø|%%c|∅|R)?\s*(\d+(?:[.,]\d+)?)", str(v))
    if not nums:
        return [d]
    out = []
    for k, (pre, num) in enumerate(nums):
        e = dict(d)
        e["id"] = d["id"] if k == 0 else f"{d['id']}_{k + 1}"
        e["value"] = float(num.replace(",", "."))
        if pre and pre.upper() == "R":
            e["axis"] = "radius"
        elif pre:
            e["axis"] = "diameter"
        elif d.get("axis") == "diameter" and k > 0:
            e["axis"] = "other"   # "Ø14 / 7,39": the second number is a length
        out.append(e)
    return out


def _tidy_text(s) -> str:
    """Small models sometimes put escaped markup and other fields' names into a text field
    (',&lt;img src=&quot;Image 1&quot;&gt;, meaning_context: "..."'): keep only the words."""
    import html
    s = html.unescape(html.unescape(str(s or "")))
    s = re.sub(r"<[^>]*>", " ", s)
    s = re.sub(r"\b[a-z_]+\s*:", " ", s)  # stray "meaning_context:" keys
    s = re.sub(r"[\"'«»{}\[\]]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" ,;.")


def _tidy_view(s) -> str:
    """The view a dimension is on, as 'вид N' when the model referred to an image number."""
    import html
    raw = html.unescape(html.unescape(str(s or "")))
    m = re.search(r"(?:image|изображени\w*|вид)\s*(\d+)", raw, re.I)
    return f"вид {m.group(1)}" if m else _tidy_text(raw)


# ---------------- parametric plan ----------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def evaluate(expr, values: dict[str, float]):
    """Evaluates a plan value: a number, or an expression over dimension ids ("d3/2 + 5").
    Only numbers, ids, + - * / and parentheses are allowed."""
    if isinstance(expr, (int, float)):
        return float(expr)
    if not isinstance(expr, str):
        raise ValueError(f"значение {expr!r} не число и не выражение")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"в выражении «{expr}» нет размера {node.id}")
            return float(values[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"недопустимое выражение «{expr}»")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except (SyntaxError, ValueError):
        # models sometimes write prose where an expression belongs ("длина паза (≈72)")
        raise ValueError(f"«{expr[:60]}» — не размер и не выражение") from None
    try:
        return ev(tree)
    except (ZeroDivisionError, OverflowError, RecursionError) as e:
        raise ValueError(f"выражение «{expr}» не вычисляется: {e}") from None


def _safe_eval(expr, values):
    """evaluate() for checks: None when there is nothing to evaluate or it does not evaluate."""
    if expr is None:
        return None
    try:
        return evaluate(expr, values)
    except (ValueError, KeyError, TypeError):
        return None


_NUMERIC_KEYS = {"x", "y", "w", "h", "x1", "y1", "x2", "y2", "r", "d", "a1", "a2", "offset", "depth", "angle"}


def resolve_plan(operations: list[dict], values: dict[str, float]) -> list[dict]:
    """Replaces expressions in a plan with numbers (recursively, including polyline points)."""
    def walk(v, key=None):
        if isinstance(v, dict):
            return {k: walk(x, k) for k, x in v.items()}
        if isinstance(v, list):
            if key == "points":
                return [[evaluate(c, values) for c in p] if isinstance(p, list) else walk(p) for p in v]
            return [walk(x) for x in v]
        if key in _NUMERIC_KEYS:
            return evaluate(v, values)
        return v
    return [walk(op) for op in operations]


def _plan_numbers(operations) -> list[str]:
    """All literal numbers and names used in plan expressions (for the "where did this come from" check)."""
    found = []

    def walk(v, key=None):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif (key in _NUMERIC_KEYS or key == "points") and key not in ("a1", "a2", "angle"):  # angles: not drawing dims
            if isinstance(v, (int, float)):
                found.append(repr(v))
            elif isinstance(v, str):
                found.extend(re.findall(r"[A-Za-z_]\w*|\d+(?:\.\d+)?", v))
    walk(operations)
    return found


# ---------------- checks ----------------


def plan_bbox(ops: list[dict]) -> tuple[list[float], list[float]] | None:
    """Rough bounding box (X, Y, Z) of the bodies a resolved plan adds (cuts are ignored)."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for op in ops:
        if op.get("op", "extrude") not in ("extrude", "revolve") or op.get("cut"):
            continue
        us, vs = [], []
        for s in op.get("shapes", []):
            t = s.get("type")
            if int(s.get("style", 1) or 1) == 3:
                continue  # an axis is not part of the body
            if t == "rect":
                us += [s["x"], s["x"] + s["w"]]
                vs += [s["y"], s["y"] + s["h"]]
            elif t == "arc" and "a1" in s and "a2" in s:
                # an arc reaches only its ends and the quadrant points it sweeps over (a gear's
                # tooth tips taken as whole circles made it 30 % bigger than it is)
                r, a1 = float(s["r"]), float(s["a1"]) % 360
                sw = (float(s["a2"]) - a1) % 360 or 360
                for ang in [a1, a1 + sw] + [q for q in (0, 90, 180, 270, 360, 450, 540, 630) if a1 < q < a1 + sw]:
                    us.append(s["x"] + r * math.cos(math.radians(ang)))
                    vs.append(s["y"] + r * math.sin(math.radians(ang)))
            elif t in ("circle", "arc"):
                r = s.get("r", s.get("d", 0) / 2)
                us += [s["x"] - r, s["x"] + r]
                vs += [s["y"] - r, s["y"] + r]
            elif t == "polyline":
                us += [p[0] for p in s["points"]]
                vs += [p[1] for p in s["points"]]
            elif t == "line":
                us += [s["x1"], s["x2"]]
                vs += [s["y1"], s["y2"]]
        if not us:
            continue
        if op.get("op") == "revolve":
            # about the axis line (style 3) drawn in the sketch: the body reaches as far from the
            # axis on every side as the profile does
            ax = next((s for s in op.get("shapes", []) if s.get("type") == "line" and int(s.get("style", 1)) == 3), None)
            if ax and abs(ax["y1"] - ax["y2"]) < 1e-9:
                R = max(abs(v - ax["y1"]) for v in vs)
                axes = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}[str(op.get("plane", "XY")).upper()]
                for axis, a, b in ((axes[0], min(us), max(us)), (axes[1], ax["y1"] - R, ax["y1"] + R),
                                   (axes[2], -R, R)):
                    lo[axis], hi[axis] = min(lo[axis], a), max(hi[axis], b)
                continue
        off = float(op.get("offset", 0))
        depth = float(op.get("depth", 0))
        n0, n1 = (off - depth, off) if op.get("reverse") else (off, off + depth)
        if op.get("symmetric"):
            n0, n1 = off - depth / 2, off + depth / 2
        axes = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}[str(op.get("plane", "XY")).upper()]
        for axis, a, b in ((axes[0], min(us), max(us)), (axes[1], min(vs), max(vs)), (axes[2], n0, n1)):
            lo[axis], hi[axis] = min(lo[axis], a), max(hi[axis], b)
    if math.inf in lo:
        return None
    return lo, hi


def check(analysis: dict, values: dict[str, float]) -> list[dict]:
    """Rules of engineering graphics and bookkeeping, done in code. Returns issues
    [{level: error|warning, text, dims: [ids]}]."""
    issues = []
    dims = {d["id"]: d for d in analysis.get("dimensions", [])}

    # 1. every dimension is a positive number
    for i, d in dims.items():
        if not isinstance(values.get(i), (int, float)) or values[i] <= 0:
            issues.append({"level": "error", "text": f"Размер {i} ({d.get('meaning', '')}) не задан", "dims": [i]})

    # 2. projection link: the same overall size must agree between views
    overall = analysis.get("overall", {})
    for axis, label in (("length", "длина"), ("width", "ширина"), ("height", "высота")):
        ref = overall.get(axis)
        if isinstance(ref, str) and ref in dims and dims[ref].get("axis") not in (axis, None):
            issues.append({"level": "error", "dims": [ref],
                           "text": f"Габаритная {label} задана размером {ref} ({dims[ref].get('meaning', '')}), "
                                   f"а это размер по другой оси ({dims[ref].get('axis')})"})
        if axis not in overall:
            issues.append({"level": "error", "dims": [],
                           "text": f"Не найден габаритный размер: {label}. Добавьте его в таблицу."})
    for axis, label in (("length", "длина"), ("width", "ширина"), ("height", "высота")):
        ids = [i for i, d in dims.items() if d.get("axis") == axis and d.get("overall")]
        vals = {i: values.get(i) for i in ids}
        if len(set(vals.values())) > 1:
            issues.append({"level": "error", "dims": ids,
                           "text": f"Проекционная связь: габаритная {label} на разных видах разная: "
                                   + ", ".join(f"{i}={v:g}" for i, v in vals.items())})
        want = _safe_eval(overall.get(axis), values)
        if want is not None and ids and values.get(ids[0]) != want:
            issues.append({"level": "warning", "dims": ids,
                           "text": f"Габаритная {label} {values.get(ids[0]):g} не совпадает с указанной в плане {want:g}"})

    # 3. dimension chains: parts must add up to the total
    for ch in analysis.get("chains", []):
        try:
            parts = sum(evaluate(p, values) for p in ch["parts"])
            total = evaluate(ch["total"], values)
        except (ValueError, KeyError) as e:
            issues.append({"level": "warning", "text": f"Цепочка размеров не проверена: {e}", "dims": []})
            continue
        if abs(parts - total) > 0.05:
            # chains are the model's own guess about how dimensions add up: a hint, not a blocker
            issues.append({"level": "warning", "dims": [p for p in ch["parts"] if isinstance(p, str)] + [ch["total"]],
                           "text": f"Цепочка размеров не замыкается: {' + '.join(map(str, ch['parts']))} = {parts:g}, "
                                   f"а общий размер {ch['total']} = {total:g}"})

    # 4. the plan resolves, and its body fits the overall dimensions
    plan = analysis.get("plan", {}).get("operations", [])
    from_picture = analysis.get("plan", {}).get("source") == "picture"
    try:
        ops = resolve_plan(plan, values)
    except ValueError as e:
        issues.append({"level": "error", "text": f"План не вычисляется: {e}", "dims": []})
        return issues
    if not ops or ops[0].get("op", "extrude") == "cut" or ops[0].get("cut"):
        issues.append({"level": "error", "dims": [],
                       "text": "План начинается с выреза: сначала нужно тело (выдавливание или вращение), из которого потом вырезать"})
    box = plan_bbox(ops)
    if box and overall:
        size = [hi - lo for lo, hi in zip(*box)]
        for k, (axis, label) in enumerate((("length", "длина (X)"), ("width", "ширина (Y)"), ("height", "высота (Z)"))):
            want = _safe_eval(overall.get(axis), values)
            if want is not None:
                if abs(size[k] - want) > 0.05:
                    issues.append({"level": "error", "dims": [],
                                   "text": f"По плану {label} детали {size[k]:g} мм, а габарит на чертеже {want:g} мм"})

    # 5. bookkeeping: dimensions not used by the plan, numbers in the plan not taken from the drawing
    #    (a plan traced from the picture has plain numbers by design: nothing to check here)
    if from_picture:
        return issues
    used = set(_plan_numbers(plan)) | {str(c) for ch in analysis.get("chains", []) for c in ch.get("parts", [])}
    for i, d in dims.items():
        if i not in used and not d.get("overall"):
            issues.append({"level": "warning", "dims": [i],
                           "text": f"Размер {i} = {values.get(i):g} ({d.get('meaning', '')}) не используется в плане"})
    literals = sorted({n for n in _plan_numbers(plan) if re.fullmatch(r"\d+(?:\.\d+)?", n) and float(n) not in (0, 1, 2)},
                      key=float)
    if literals:
        issues.append({"level": "warning", "dims": [],
                       "text": "В плане есть числа, не привязанные к размерам чертежа: " + ", ".join(literals)})
    return issues
