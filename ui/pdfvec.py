"""Vector PDF drawings (what SOLIDWORKS, KOMPAS, AutoCAD and others print to PDF): the lines and texts
are read straight from the page and written out as a DXF, so the drawing goes the exact vector way —
numbers from the file, true scale, real arcs — instead of being rasterised and read by a model.

Only what drawings use is supported: straight path pieces and Bézier curves (flattened), stroke
widths and dash patterns (thick main lines, thin lines, hidden and axis lines), text drawn with
fonts that carry a ToUnicode map, and the SOLIDWORKS bookmarks that name every view with its box.
Polylines that follow a circle are turned back into arcs and circles. Views drawn at their own scale
(«Б-Б (2 : 1)») are brought back to true size around their middle, so every view is 1 : 1.
"""
from __future__ import annotations

import math
import re
import zlib

PT = 25.4 / 72  # PDF units (points) → mm


# ---------------- objects ----------------

class Ref:
    __slots__ = ("n",)

    def __init__(self, n):
        self.n = n

    def __repr__(self):
        return f"Ref({self.n})"


class Name(str):
    pass


_TOKEN = re.compile(rb"\s*(?:(%[^\r\n]*)|(<<|>>|\[|\]|\{|\})|(/[^\s/<>\[\]\(\)\{\}%]*)|(<[0-9A-Fa-f\s]*>)|(\()|"
                    rb"([+-]?(?:\d+\.?\d*|\.\d+))|([A-Za-z'\"*][A-Za-z0-9'\"*]*))")


def _name(raw: bytes) -> Name:
    return Name(re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw[1:]).decode("latin-1"))


def _literal(data: bytes, i: int) -> tuple[bytes, int]:
    """A (string) starting after its opening bracket at i; returns the bytes and the index after it."""
    out, depth = bytearray(), 1
    esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(data):
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            c = data[i]
            if c in esc:
                out.append(esc[c])
            elif 0x30 <= c <= 0x37:
                j = i
                while j < i + 3 and j < len(data) and 0x30 <= data[j] <= 0x37:
                    j += 1
                out.append(int(data[i:j], 8) & 255)
                i = j - 1
            elif c in (10, 13):
                if c == 13 and i + 1 < len(data) and data[i + 1] == 10:
                    i += 1
            else:
                out.append(c)
        elif c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out.append(c)
        else:
            out.append(c)
        i += 1
    return bytes(out), i


def tokens(data: bytes, i: int = 0):
    """Lexical tokens: ('op', b'm'), ('num', 1.0), ('name', 'Font'), ('str', b'..'), ('punct', b'[')."""
    n = len(data)
    while i < n:
        m = _TOKEN.match(data, i)
        if not m or m.end() == i:
            i += 1
            continue
        i = m.end()
        if m.group(1):
            continue
        if m.group(2):
            yield "punct", m.group(2), i
        elif m.group(3) is not None:
            yield "name", _name(m.group(3)), i
        elif m.group(4):
            h = re.sub(rb"\s", b"", m.group(4)[1:-1])
            if len(h) % 2:
                h += b"0"
            yield "str", bytes.fromhex(h.decode()), i
        elif m.group(5):
            s, i = _literal(data, i)
            yield "str", s, i
        elif m.group(6):
            yield "num", float(m.group(6)), i
        else:
            yield "op", m.group(7), i


def parse_object(data: bytes, i: int = 0):
    """One object starting at i: returns (value, index after it)."""
    it = tokens(data, i)
    first = next(it, None)
    if first is None:
        return None, len(data)
    kind, val, pos = first
    if kind == "num":
        # a plain number, or the start of a reference "n g R"
        save = pos
        a, b = next(it, None), next(it, None)
        if a and b and a[0] == "num" and b[0] == "op" and b[1] == b"R":
            return Ref(int(val)), b[2]
        return val, save
    if kind == "op":
        return {b"true": True, b"false": False, b"null": None}.get(val, val), pos
    if kind != "punct" or val not in (b"[", b"<<"):
        return val, pos
    stack: list = [[] if val == b"[" else ["<<"]]
    for kind, val, pos in it:
        if kind == "punct" and val in (b"[", b"<<"):
            stack.append([] if val == b"[" else ["<<"])
            continue
        if kind == "punct" and val in (b"]", b">>"):
            items = stack.pop()
            if items and items[0] == "<<" and not isinstance(items[0], Name):
                items = items[1:]
                v = {items[k]: items[k + 1] for k in range(0, len(items) - 1, 2)}
            else:
                v = items
            if not stack:
                return v, pos
            stack[-1].append(v)
            continue
        top = stack[-1]
        if kind == "op" and val == b"R" and len(top) >= 2:
            top.pop()
            top.append(Ref(int(top.pop())))
            continue
        if kind == "op":
            top.append({b"true": True, b"false": False, b"null": None}.get(val, val))
        else:
            top.append(val)
    return None, len(data)


class PDF:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        self.offsets: dict[int, int] = {}
        for m in re.finditer(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b", self.data):
            self.offsets[int(m.group(1))] = m.end()  # the last definition wins (incremental updates)
        self.cache: dict[int, object] = {}
        self.streams: dict[int, bytes] = {}
        self._objstm()

    def _objstm(self):
        """Objects packed into object streams (PDF 1.5+)."""
        for n in list(self.offsets):
            obj = self.get(n)
            if isinstance(obj, dict) and obj.get("Type") == "ObjStm":
                raw = self.stream(n)
                first, count = int(obj["First"]), int(obj["N"])
                head = [int(float(x)) for x in raw[:first].split()]
                for k in range(count):
                    num, off = head[2 * k], head[2 * k + 1]
                    if num not in self.offsets:
                        self.cache[num], _ = parse_object(raw, first + off)

    def get(self, n):
        if isinstance(n, Ref):
            n = n.n
        if n in self.cache:
            return self.cache[n]
        if n not in self.offsets:
            return None
        val, end = parse_object(self.data, self.offsets[n])
        self.cache[n] = val
        if isinstance(val, dict):
            m = re.match(rb"\s*stream\r?\n", self.data[end:end + 20])
            if m:
                self.streams[n] = end + m.end()
        return val

    def r(self, v):
        """Follows a reference."""
        return self.get(v) if isinstance(v, Ref) else v

    def stream(self, n) -> bytes:
        if isinstance(n, Ref):
            n = n.n
        d = self.get(n)
        start = self.streams.get(n)
        if start is None:
            return b""
        length = self.r(d.get("Length"))
        if isinstance(length, (int, float)):
            raw = self.data[start:start + int(length)]
        else:
            raw = self.data[start:self.data.find(b"endstream", start)]
        filt = self.r(d.get("Filter"))
        filters = filt if isinstance(filt, list) else [filt] if filt else []
        for f in filters:
            if f == "FlateDecode":
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompressobj().decompress(raw)
            else:
                return b""  # images and the like: not needed for drawings
        return raw

    def pages(self) -> list[dict]:
        root = self.r(self._trailer().get("Root"))
        out = []

        def walk(node, inherited):
            node = self.r(node)
            inh = {**inherited, **{k: node[k] for k in ("Resources", "MediaBox") if k in node}}
            if node.get("Type") == "Pages" or "Kids" in node:
                for kid in self.r(node.get("Kids", [])):
                    walk(kid, inh)
            else:
                out.append({**inh, **node})
        walk(root["Pages"], {})
        return out

    def _trailer(self) -> dict:
        i = self.data.rfind(b"trailer")
        if i >= 0:
            t, _ = parse_object(self.data, i + 7)
            if isinstance(t, dict) and "Root" in t:
                return t
        for n in self.offsets:  # a cross-reference stream carries the trailer itself
            o = self.get(n)
            if isinstance(o, dict) and o.get("Type") == "XRef" and "Root" in o:
                return o
        for n in self.offsets:
            o = self.get(n)
            if isinstance(o, dict) and o.get("Type") == "Catalog":
                return {"Root": Ref(n)}
        raise ValueError("PDF без каталога")


# ---------------- fonts ----------------

def _cmap(raw: bytes) -> dict[int, str]:
    out: dict[int, str] = {}

    def uni(h: bytes) -> str:
        b = bytes.fromhex(h.decode())
        try:
            return b.decode("utf-16-be")
        except UnicodeDecodeError:
            return ""
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", raw, re.S):
        for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(a, 16)] = uni(b)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", raw, re.S):
        for a, b, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            lo, hi = int(a, 16), int(b, 16)
            if rest.startswith(b"["):
                for k, h in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", rest)):
                    out[lo + k] = uni(h)
            else:
                base = bytes.fromhex(rest[1:-1].decode())
                for k in range(hi - lo + 1):
                    v = int.from_bytes(base, "big") + k
                    out[lo + k] = uni(v.to_bytes(len(base), "big").hex().encode())
    return out


class Font:
    def __init__(self, pdf: PDF, d: dict):
        d = pdf.r(d)
        self.two = d.get("Subtype") == "Type0"
        self.map = _cmap(pdf.stream(d["ToUnicode"])) if "ToUnicode" in d else {}
        if not self.map and "Symbol_A" in str(d.get("BaseFont", "")):
            # KOMPAS's sign font: «G» is its diameter sign
            self.map = {71: "Ø"}
        self.widths: dict[int, float] = {}
        self.dw = 1000.0 if self.two else 500.0
        if self.two:
            desc = pdf.r(pdf.r(d.get("DescendantFonts"))[0])
            self.dw = float(pdf.r(desc.get("DW", 1000)))
            w = pdf.r(desc.get("W", []))
            k = 0
            while k < len(w):
                c = int(w[k])
                nxt = pdf.r(w[k + 1])
                if isinstance(nxt, list):
                    for j, v in enumerate(nxt):
                        self.widths[c + j] = float(v)
                    k += 2
                else:
                    for j in range(c, int(nxt) + 1):
                        self.widths[j] = float(w[k + 2])
                    k += 3
        else:
            fc = int(d.get("FirstChar", 0))
            for j, v in enumerate(pdf.r(d.get("Widths", [])) or []):
                self.widths[fc + j] = float(pdf.r(v))

    def decode(self, s: bytes):
        """(unicode text, advance in text space /1000) of a string shown with this font."""
        codes = [int.from_bytes(s[k:k + 2], "big") for k in range(0, len(s) - 1, 2)] if self.two else list(s)
        text = "".join(self.map.get(c, chr(c) if not self.two and 32 <= c < 127 else "") for c in codes)
        adv = sum(self.widths.get(c, self.dw) for c in codes)
        return text, adv, len(codes)


# ---------------- page content ----------------

def _mul(a, b):
    """Affine matrices [a b c d e f] composed: first a, then b."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _bezier(p0, p1, p2, p3, n=8):
    out = []
    for k in range(1, n + 1):
        t = k / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def read_page(pdf: PDF, page: dict) -> dict:
    """Strokes ({pts (pt), width (pt), dash}) and texts ({text, x, y, size, angle}) of one page."""
    res = pdf.r(page.get("Resources", {})) or {}
    fonts_d = pdf.r(res.get("Font", {})) or {}
    fonts: dict[str, Font] = {}
    contents = pdf.r(page.get("Contents"))
    refs = contents if isinstance(contents, list) else [page.get("Contents")]
    data = b"\n".join(pdf.stream(r) for r in refs if isinstance(r, Ref))
    xobjs = pdf.r(res.get("XObject", {})) or {}

    strokes, texts = [], []
    gs = {"ctm": [1, 0, 0, 1, 0, 0], "lw": 1.0, "dash": []}
    stack = []
    path: list[list] = []
    cur = None
    operands: list = []
    tm = tlm = [1, 0, 0, 1, 0, 0]
    font, fsize, tz, tl, tc, tw, trise = None, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0
    run = None  # the text being collected: consecutive strings on one line join

    def show(s: bytes):
        nonlocal tm, run
        if font is None:
            return
        text, adv, count = font.decode(s)
        trm = _mul([fsize * tz, 0, 0, fsize, 0, trise], _mul(tm, gs["ctm"]))
        x, y = trm[4], trm[5]
        size = math.hypot(trm[2], trm[3])
        ang = math.atan2(trm[1], trm[0])
        width = (adv / 1000.0 * fsize + count * tc) * tz
        tm = _mul([1, 0, 0, 1, width, 0], tm)
        end = _apply(_mul(tm, gs["ctm"]), 0, trise)
        if not text.strip():
            if run is not None:
                run["text"] += text
                run["end"] = end
            return
        if run is not None and abs(run["angle"] - ang) < 0.05:
            # along the line from the previous end, at most about a letter away and not far off the baseline
            dx, dy = x - run["end"][0], y - run["end"][1]
            along = dx * math.cos(ang) + dy * math.sin(ang)
            across = -dx * math.sin(ang) + dy * math.cos(ang)
            if -0.3 * run["size"] <= along <= 0.9 * run["size"] and abs(across) <= 0.75 * run["size"]:
                run["text"] += (" " if along > 0.25 * run["size"] else "") + text
                run["end"] = end
                return
        run = {"text": text, "x": x, "y": y, "size": size, "angle": ang, "end": end}
        texts.append(run)

    def flush_path(stroke: bool):
        nonlocal path, cur
        if stroke and not gs.get("white"):
            m = gs["ctm"]
            scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
            for sub in path:
                if len(sub) >= 2:
                    strokes.append({"pts": [_apply(m, *p) for p in sub], "width": gs["lw"] * scale,
                                    "dash": [d * scale for d in gs["dash"]]})
        path, cur = [], None

    def run_content(data: bytes, depth=0):
        nonlocal path, cur, operands, tm, tlm, font, fsize, tz, tl, tc, tw, trise, run, gs, stack, fonts_d, xobjs, fonts
        it = tokens(data)
        arr = None
        for kind, val, _pos in it:
            if kind == "punct":
                if val == b"[":
                    arr = []
                elif val == b"]":
                    operands.append(arr)
                    arr = None
                continue
            if kind != "op":
                (arr if arr is not None else operands).append(val)
                continue
            op, o = val, operands
            operands = []
            try:
                if op == b"q":
                    stack.append({**gs, "dash": list(gs["dash"])})
                elif op == b"Q":
                    if stack:
                        gs = stack.pop()
                elif op == b"cm":
                    gs["ctm"] = _mul([float(v) for v in o[-6:]], gs["ctm"])
                elif op == b"w":
                    gs["lw"] = float(o[-1])
                elif op in (b"RG", b"G", b"K", b"SC", b"SCN"):
                    # the stroke colour: KOMPAS's PDF paints white scan lines over areas (under the
                    # arrowheads, round texts) — they are no lines of the drawing
                    v = [float(x) for x in o if isinstance(x, float)]
                    if op == b"K" and len(v) == 4:
                        gs["white"] = max(v) < 0.05
                    elif v:
                        gs["white"] = min(v) > 0.95
                elif op == b"d":
                    gs["dash"] = [float(v) for v in (o[0] or [])]
                elif op == b"m":
                    cur = [(float(o[-2]), float(o[-1]))]
                    path.append(cur)
                elif op == b"l" and cur is not None:
                    cur.append((float(o[-2]), float(o[-1])))
                elif op in (b"c", b"v", b"y") and cur is not None:
                    v = [float(x) for x in o]
                    p0 = cur[-1]
                    if op == b"c":
                        p1, p2, p3 = (v[0], v[1]), (v[2], v[3]), (v[4], v[5])
                    elif op == b"v":
                        p1, p2, p3 = p0, (v[0], v[1]), (v[2], v[3])
                    else:
                        p1, p2, p3 = (v[0], v[1]), (v[2], v[3]), (v[2], v[3])
                    cur.extend(_bezier(p0, p1, p2, p3))
                elif op == b"h" and cur:
                    cur.append(cur[0])
                elif op == b"re":
                    x, y, w, h = (float(v) for v in o[-4:])
                    cur = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
                    path.append(cur)
                elif op in (b"S", b"s"):
                    if op == b"s" and cur:
                        cur.append(cur[0])
                    flush_path(True)
                elif op in (b"B", b"B*", b"b", b"b*"):
                    flush_path(True)
                elif op in (b"f", b"F", b"f*", b"n"):
                    flush_path(False)  # fills are arrowheads, hatches drawn as areas, text strokes
                elif op == b"BT":
                    tm = tlm = [1, 0, 0, 1, 0, 0]
                    run = None
                elif op == b"ET":
                    run = None
                elif op == b"Tf":
                    name, fsize = o[-2], float(o[-1])
                    if name not in fonts and name in fonts_d:
                        try:
                            fonts[name] = Font(pdf, fonts_d[name])
                        except Exception:  # noqa: BLE001 - an odd font loses its texts, not the page
                            fonts[name] = None
                    font = fonts.get(name)
                elif op == b"Tz":
                    tz = float(o[-1]) / 100.0
                elif op == b"TL":
                    tl = float(o[-1])
                elif op == b"Tc":
                    tc = float(o[-1])
                elif op == b"Tw":
                    tw = float(o[-1])
                elif op == b"Ts":
                    trise = float(o[-1])
                elif op == b"Tm":
                    tm = tlm = [float(v) for v in o[-6:]]
                elif op in (b"Td", b"TD"):
                    tx, ty = float(o[-2]), float(o[-1])
                    if op == b"TD":
                        tl = -ty
                    tm = tlm = _mul([1, 0, 0, 1, tx, ty], tlm)
                elif op == b"T*":
                    tm = tlm = _mul([1, 0, 0, 1, 0, -tl], tlm)
                elif op == b"Tj":
                    show(o[-1])
                elif op == b"'":
                    tm = tlm = _mul([1, 0, 0, 1, 0, -tl], tlm)
                    show(o[-1])
                elif op == b'"':
                    tm = tlm = _mul([1, 0, 0, 1, 0, -tl], tlm)
                    show(o[-1])
                elif op == b"TJ":
                    for part in o[-1] or []:
                        if isinstance(part, bytes):
                            show(part)
                        elif isinstance(part, float):
                            tm = _mul([1, 0, 0, 1, -part / 1000.0 * fsize * tz, 0], tm)
                elif op == b"Do" and depth < 4:
                    x = pdf.r(xobjs.get(o[-1])) if o else None
                    ref = xobjs.get(o[-1]) if o else None
                    if isinstance(x, dict) and x.get("Subtype") == "Form" and isinstance(ref, Ref):
                        saved = {**gs, "dash": list(gs["dash"])}
                        if "Matrix" in x:
                            gs["ctm"] = _mul([float(v) for v in x["Matrix"]], gs["ctm"])
                        # a form keeps its own fonts and pictures (the gear's parameter table is one:
                        # its text was lost with the page's font names)
                        saved_res = (fonts_d, xobjs, fonts)
                        fres = pdf.r(x.get("Resources")) if x.get("Resources") is not None else None
                        if isinstance(fres, dict):
                            fonts_d = pdf.r(fres.get("Font", {})) or fonts_d
                            xobjs = pdf.r(fres.get("XObject", {})) or xobjs
                            fonts = {}
                        run_content(pdf.stream(ref), depth + 1)
                        fonts_d, xobjs, fonts = saved_res
                        gs = saved
            except (IndexError, TypeError, ValueError):
                continue
    run_content(data)
    strokes = _diameter_signs(strokes, texts)
    texts = _join_runs(texts)
    for t in texts:
        # «200 + 0,185»: the tolerance is written apart from its sign
        t["text"] = re.sub(r"([+\-±])\s+(?=\d)", r"\1", t["text"].strip())
        t.pop("end", None)
    return {"strokes": strokes, "texts": [t for t in texts if t["text"]],
            "box": [float(v) for v in pdf.r(page.get("MediaBox", [0, 0, 595, 842]))]}


def _local(t, x, y):
    """(along, across) of a point relative to a text's start, in its own direction."""
    dx, dy = x - t["x"], y - t["y"]
    c, s = math.cos(t["angle"]), math.sin(t["angle"])
    return dx * c + dy * s, -dx * s + dy * c


def _diameter_signs(strokes: list, texts: list) -> list:
    """SOLIDWORKS draws the diameter sign as a little circle crossed by a slash, not as a letter: such
    a pair just before a number becomes «Ø» in that text, and the two strokes are dropped."""
    rings = []
    for k, s in enumerate(strokes):
        p = s["pts"]
        if len(p) < 12 or math.dist(p[0], p[-1]) > 0.05 * max(1e-9, max(math.dist(p[0], q) for q in p)):
            continue
        cx, cy = sum(q[0] for q in p) / len(p), sum(q[1] for q in p) / len(p)
        rs = [math.dist(q, (cx, cy)) for q in p]
        r = sum(rs) / len(rs)
        if r > 0 and max(rs) - min(rs) < 0.12 * r:
            rings.append((k, cx, cy, r))
    drop = set()
    for k, cx, cy, r in rings:
        for t in texts:
            if not re.match(r"\s*\d", t["text"]) or not 0.12 * t["size"] <= r <= 0.5 * t["size"]:
                continue
            along, across = _local(t, cx, cy)
            if -1.2 * t["size"] <= along <= 0 and -0.1 * t["size"] <= across <= 0.7 * t["size"]:
                def slanted(s):  # the sign's slash leans; a position-tolerance cross is square to the text
                    (ax, ay), (bx, by) = s["pts"]
                    a = math.degrees(math.atan2(by - ay, bx - ax) - t["angle"]) % 90
                    return 20 <= a <= 85
                slash = next((j for j, s in enumerate(strokes) if len(s["pts"]) == 2 and j not in drop and
                              math.dist(((s["pts"][0][0] + s["pts"][1][0]) / 2, (s["pts"][0][1] + s["pts"][1][1]) / 2),
                                        (cx, cy)) < 0.5 * r and slanted(s)), None)
                if slash is None:
                    continue
                drop |= {k, slash}
                t["text"] = "Ø" + t["text"].lstrip()
                # the text now starts at the sign
                c, s_ = math.cos(t["angle"]), math.sin(t["angle"])
                back = along - r
                t["x"], t["y"] = t["x"] + back * c, t["y"] + back * s_
                break
    return [s for k, s in enumerate(strokes) if k not in drop]


def _join_runs(texts: list) -> list:
    """«3 отв.» and «Ø14 -0,43» are drawn as separate strings with the sign between them: strings on
    one line, a short gap apart, are one text."""
    out: list = []
    for t in texts:
        prev = next((p for p in reversed(out[-6:]) if abs(p["angle"] - t["angle"]) < 0.05), None)
        if prev is not None and prev.get("end"):
            along, across = _local(prev, t["x"], t["y"])
            e_along, _ = _local(prev, *prev["end"])
            gap = along - e_along
            if -0.3 * prev["size"] <= gap <= 0.9 * prev["size"] and abs(across) <= 0.3 * prev["size"]:
                # KOMPAS's PDF puts every letter in a text block of its own: letters a letter's
                # spacing apart are one word («10», not «1 0»)
                sep = " " if gap > 0.25 * prev["size"] else ""
                prev["text"] = prev["text"].rstrip() + sep + t["text"].lstrip()
                prev["end"] = t.get("end", prev["end"])
                continue
        out.append(t)
    return out


def bookmarks(pdf: PDF) -> list[dict]:
    """SOLIDWORKS names every view in the bookmarks with a FitR box: [{title, box (pt)}]."""
    out = []
    try:
        root = pdf.r(pdf._trailer().get("Root"))
        node = pdf.r(root.get("Outlines"))
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(node, dict):
        return out
    seen = set()

    def walk(ref):
        while isinstance(ref, Ref) and ref.n not in seen:
            seen.add(ref.n)
            item = pdf.r(ref)
            if not isinstance(item, dict):
                return
            title = item.get("Title", b"")
            if isinstance(title, bytes):
                title = title[2:].decode("utf-16-be", "replace") if title[:2] == b"\xfe\xff" else title.decode("latin-1")
            dest = pdf.r(item.get("Dest"))
            if dest is None and item.get("A") is not None:
                dest = pdf.r(pdf.r(item["A"]).get("D"))
            if isinstance(dest, list) and len(dest) >= 6 and dest[1] == "FitR":
                out.append({"title": title, "box": [float(v) for v in dest[2:6]]})
            if "First" in item:
                walk(item["First"])
            ref = item.get("Next")
    walk(node.get("First"))
    return out


# ---------------- geometry clean-up ----------------

def _circle3(a, b, c):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _turn(a, b, c):
    return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])


def _circle_ls(pts: list):
    """The circle nearest to the points (least squares, Kåsa): (cx, cy, r) or None."""
    n = len(pts)
    if n < 3:
        return None
    mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    u = [p[0] - mx for p in pts]
    v = [p[1] - my for p in pts]
    suu, svv, suv = sum(a * a for a in u), sum(b * b for b in v), sum(a * b for a, b in zip(u, v))
    suuu, svvv = sum(a ** 3 for a in u), sum(b ** 3 for b in v)
    suvv, svuu = sum(a * b * b for a, b in zip(u, v)), sum(b * a * a for a, b in zip(u, v))
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    uc = (0.5 * (suuu + suvv) * svv - 0.5 * (svvv + svuu) * suv) / det
    vc = (0.5 * (svvv + svuu) * suu - 0.5 * (suuu + suvv) * suv) / det
    return mx + uc, my + vc, math.sqrt(uc * uc + vc * vc + (suu + svv) / n)


def fit_arcs(pts: list, tol: float = 0.02, ratio: float = 1.6, ls: bool = False) -> list:
    """A polyline (mm) as vertices with bulges: runs of 4+ points lying on one circle, turning the same
    way in even steps, become one arc. Returns [(x, y, bulge)] — bulge on the vertex the arc starts at.
    ls: the circle fitted to all points of the run (chords on a coarse grid: three points of them
    give a circle too far off)."""
    n = len(pts)
    if ls and n >= 8 and math.dist(pts[0], pts[-1]) < 0.35:
        # a whole closed chain on one circle is that circle (fitting run by run can lose its first
        # chord to the grid and leave the rest an arc short of closing)
        c = _circle_ls(pts)
        if c and c[2] < 5000 and all(abs(math.dist(p, c[:2]) - c[2]) <= tol for p in pts) and \
                max(math.dist(a, b) for a, b in zip(pts, pts[1:])) < 0.5 * c[2]:
            return [("circle", *c)]
    out = []
    i = 0
    while i < n - 1:
        best = None
        # the circle through three points spread over the run, never its last one: on a full circle
        # that point is the first one again
        through = (lambda i, j: _circle_ls(pts[i:j + 1] if math.dist(pts[i], pts[j]) > 1e-6 else pts[i:j])) if ls else \
            (lambda i, j: _circle3(pts[i], pts[i + (j - i) // 3], pts[i + 2 * (j - i) // 3]))  # noqa: E731
        if i + 3 < n:
            j = i + 3
            c = through(i, j)
            while c and j < n:
                cx, cy, r = c
                seg = [math.dist(pts[k], pts[k + 1]) for k in range(i, j)]
                sign = [_turn(pts[k - 1], pts[k], pts[k + 1]) for k in range(i + 1, j)]
                if ls:   # a grid step can leave two chords in line: only clear turns count
                    sign = [s for s in sign if abs(s) > 0.002] or [1.0]
                ok = (r < 5000 and all(abs(math.hypot(p[0] - cx, p[1] - cy) - r) <= tol for p in pts[i:j + 1])
                      and (all(s > 0 for s in sign) or all(s < 0 for s in sign))
                      and max(seg) <= ratio * min(seg) + 1e-6 and max(seg) < r * (0.5 if ls else 1.2))
                # (ls: a polygon's corners lie on a circle too — a rectangle's four — but its sides
                # are long chords; a curve's chords span 30° at most)
                if not ok:
                    break
                best = (j, c)
                if j + 1 >= n:
                    break
                j += 1
                c = through(i, j)
        if best:
            j, (cx, cy, r) = best
            a0 = math.atan2(pts[i][1] - cy, pts[i][0] - cx)
            sweep = 0.0
            for k in range(i, j):
                a1 = math.atan2(pts[k][1] - cy, pts[k][0] - cx)
                a2 = math.atan2(pts[k + 1][1] - cy, pts[k + 1][0] - cx)
                sweep += (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
            # KOMPAS leaves its circles open by a grid step or two (the ends 0.17 mm apart)
            if abs(sweep) >= 2 * math.pi - 1e-3 or math.dist(pts[i], pts[j]) < 1e-6 or \
                    (ls and abs(sweep) >= 2 * math.pi - 0.15 and math.dist(pts[i], pts[j]) < 0.35):
                out.append(("circle", cx, cy, r))
                i = j
                continue
            out.append((pts[i][0], pts[i][1], math.tan(sweep / 4), (cx, cy, r, sweep)))
            i = j
            continue
        out.append((pts[i][0], pts[i][1], 0.0, None))
        i += 1
    out.append((pts[-1][0], pts[-1][1], 0.0, None))
    return out


def _chain_pieces(strokes: list, short: float = 6.0 / PT) -> list:
    """KOMPAS's PDF draws every piece of a line as a path of its own, and a circle or an arc as a run
    of tiny separate chords: short pieces, one after the other, each starting where the last one
    ended, with the same pen, are one polyline again — so the arcs can be fitted. Only on such pages
    (nearly every stroke one or two pieces long); other PDFs keep their strokes as they are."""
    if not strokes or sum(len(s["pts"]) <= 3 for s in strokes) < 0.9 * len(strokes):
        return strokes, False
    length = lambda pts: sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))  # noqa: E731
    out: list = []
    for s in strokes:
        if len(s["pts"]) == 3 and math.dist(s["pts"][0], s["pts"][2]) < 1e-6:
            s = {**s, "pts": s["pts"][:2]}   # drawn there and back again: one piece
        prev = out[-1] if out else None
        if (prev is not None and prev["width"] == s["width"] and prev["dash"] == s["dash"]
                and math.dist(prev["pts"][-1], s["pts"][0]) < 1e-3 * short
                and length(s["pts"]) < short and length(prev["pts"][-2:]) < short
                and (len(prev["pts"]) < 3 or math.dist(prev["pts"][0], prev["pts"][-1]) > 1e-6)):   # a closed circle ends
            prev["pts"] = prev["pts"] + s["pts"][1:]
            continue
        out.append({**s, "pts": list(s["pts"])})
    return out, True


def _dedupe(pts, eps=1e-4):
    out = []
    for p in pts:
        if not out or math.dist(out[-1], p) > eps:
            out.append(p)
    return out


SCALE_RE = re.compile(r"\(\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)\s*\)")


def _view_scales(page: dict, marks: list[dict]) -> list[dict]:
    """Views drawn at their own scale: their box (pt) and the scale, from the bookmarks' boxes and the
    view label above each box («Б-Б (2 : 1)», «Ж (3 : 1)»); bookmarks may say it themselves."""
    out = []
    for b in marks:
        x0, y0, x1, y1 = b["box"]
        m = SCALE_RE.search(b["title"])
        if not m:
            # the label: text with a scale just above the box (within a few letters), overlapping it
            near = [t for t in page["texts"] if SCALE_RE.search(t["text"]) and x0 - 20 <= t["x"] <= x1
                    and y1 - 10 <= t["y"] <= y1 + 60 and abs(t["angle"]) < 0.1]
            near.sort(key=lambda t: t["y"] - y1)
            m = SCALE_RE.search(near[0]["text"]) if near else None
        if m:
            k = float(m.group(1).replace(",", ".")) / float(m.group(2).replace(",", "."))
            out.append({"box": b["box"], "scale": k, "title": b["title"] or m.group(0)})
    return out


GEAR_ROWS = (("m", r"^модуль"), ("z", r"^число\s+зуб"), ("beta", r"^угол\s+наклона"),
             ("hand", r"^направлени\w*\s+лини"), ("alpha", r"^угол\s+профил"), ("x", r"^коэффициент\s+смещени"),
             ("d", r"^делительн\w*\s+диаметр"))


def gear_table(pdf_path: str, page_no: int = 0) -> dict | None:
    """A gear wheel's parameter table (ГОСТ 2.403): module, number of teeth, helix angle and hand,
    profile angle, shift, pitch diameter — each value is the number written in the row's last
    column (to the right of the label, on its line). None when the sheet has no such table."""
    pdf = PDF(pdf_path)
    page = read_page(pdf, pdf.pages()[page_no])
    texts = [t for t in page["texts"] if t["text"].strip()]
    out = {}
    for key, pat in GEAR_ROWS:
        label = next((t for t in texts if re.search(pat, t["text"].strip(), re.I) and abs(t["angle"]) < 0.1), None)
        if label is None:
            continue
        row = [t for t in texts if t is not label and t["x"] > label["x"] + 2 * label["size"]
               and abs(t["y"] - label["y"]) < 0.8 * label["size"] and abs(t["angle"]) < 0.1]
        for t in sorted(row, key=lambda t: -t["x"]):   # the value column is the rightmost
            s = t["text"].strip()
            if key == "hand":
                if re.search(r"прав", s, re.I):
                    out[key] = "right"
                elif re.search(r"лев", s, re.I):
                    out[key] = "left"
                if key in out:
                    break
                continue
            m = re.match(r"^([+-]?\d+(?:[.,]\d+)?)", s)
            if m:
                out[key] = float(m.group(1).replace(",", "."))
                break
    return out if "m" in out and "z" in out else None


def _sheet_scale(texts: list[dict]) -> float:
    """The title block's scale cell, e.g. «1:1» / «2:1» standing alone (default 1)."""
    for t in texts:
        m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)\s*", t["text"])
        if m:
            return float(m.group(1).replace(",", ".")) / float(m.group(2).replace(",", "."))
    return 1.0


def _dim_ratios(page: dict) -> list[tuple]:
    """How long each dimension is drawn against the number written on it: (x, y of the text in pt,
    paper mm per mm of the part). A dimension's own line runs along its text, just under it."""
    import dxf  # the same rule for what a dimension text is
    lines = [s["pts"] for s in page["strokes"] if len(s["pts"]) == 2 and not s["dash"]]
    out = []
    for t in page["texts"]:
        m = dxf._DIM_TEXT.fullmatch(t["text"])
        if not m or "°" in t["text"] or (m.group("pre") or "").upper() in ("R", "M"):
            continue
        if page.get("kompas") and (m.group("pre") or ""):
            continue   # KOMPAS writes Ø… on a leader's shelf: that line is no dimension line
        v = float(m.group("num").replace(",", "."))
        if v < 3:
            continue
        size = t["size"]
        best = None
        for a, b in lines:
            (a1, c1), (a2, c2) = _local(t, *a), _local(t, *b)
            if abs(c1 - c2) > 0.02 * size or not (-0.9 * size <= c1 <= 0.1 * size):
                continue  # not parallel to the text, or not right under it
            lo, hi = min(a1, a2), max(a1, a2)
            if not (lo <= 0.3 * size <= hi):
                continue
            if best is None or abs(c1) < best[0]:
                best = (abs(c1), hi - lo)
        if best and best[1] > 0:
            out.append((t["x"], t["y"], best[1] * PT / v))
    return out


NICE = (1, 2, 2.5, 4, 5, 10, 20, 0.5, 0.4, 0.25, 0.2, 0.1, 0.05)


def _view_boxes_pt(strokes: list, limit: float) -> list[list]:
    """Boxes (pt) of the views when the PDF carries no bookmarks: groups of main lines."""
    import dxf
    ents = [{"type": "LWPOLYLINE", "kind": "solid", "pts": [[x * PT, y * PT] for x, y in s["pts"]],
             "bulge": [0.0] * len(s["pts"])} for s in strokes if not s["dash"] and s["width"] >= limit]
    return [[v / PT for v in b] for b in dxf.view_boxes({"entities": ents})]


def to_dxf(pdf_path: str, dxf_path: str, page_no: int = 0) -> dict | None:
    """Writes the page's drawing as a DXF in true millimetres. None when the page is not a vector
    drawing (a scan: hardly any strokes)."""
    pdf = PDF(pdf_path)
    pages = pdf.pages()
    if not pages:
        return None
    page = read_page(pdf, pages[page_no])
    if len(page["strokes"]) < 50:
        return None
    page["strokes"], chained = _chain_pieces(page["strokes"])
    page["kompas"] = chained
    # KOMPAS's chords sit on a 1/300″ grid (0.085 mm): their points are off the true circle by up
    # to half of that and the chords are of uneven length
    fit_kw = {"tol": 0.06, "ratio": 4.0, "ls": True} if chained else {}
    # main lines: the widest strokes in use (by drawn length); everything clearly thinner is thin
    by_w: dict[float, float] = {}
    for s in page["strokes"]:
        if not s["dash"]:
            L = sum(math.dist(a, b) for a, b in zip(s["pts"], s["pts"][1:]))
            by_w[round(s["width"], 2)] = by_w.get(round(s["width"], 2), 0) + L
    widths = sorted(w for w, L in by_w.items() if L > 0.03 * sum(by_w.values()))
    thick = widths[-1] if widths else 1.0
    if len(widths) >= 2 and widths[-1] > 1.6 * widths[-2] and by_w[widths[-1]] < 0.25 * sum(by_w.values()):
        # a few very heavy strokes (sheet frame / title block) next to the outline weight
        thick = widths[-2]
    limit = 0.75 * thick
    marks = bookmarks(pdf) or [{"title": "", "box": b} for b in _view_boxes_pt(page["strokes"], limit)]
    labelled = _view_scales(page, marks)
    # the scale the main (biggest) view is drawn in, measured on its own dimensions: the title
    # block's cell is sometimes plainly wrong (SOLIDWORKS sheet property left at «1:5»)
    sheet = _sheet_scale(page["texts"])
    how = "штамп"
    if marks:
        mb = max(marks, key=lambda b: (b["box"][2] - b["box"][0]) * (b["box"][3] - b["box"][1]))["box"]
        pad = 40 / PT
        ratios = _dim_ratios(page)
        rs = sorted(r for x, y, r in ratios
                    if mb[0] - pad <= x <= mb[2] + pad and mb[1] - pad <= y <= mb[3] + pad)
        nice = [v["scale"] for v in labelled] + list(NICE)

        def common(rs):
            """The most common ratio (a dimension may pick a wrong line now and then), a scale
            named on the sheet or a standard one counting extra."""
            best = None
            for r in rs:
                agree = [q for q in rs if abs(q - r) <= 0.02 * r]
                k = sum(agree) / len(agree)
                near = min(nice, key=lambda c: abs(c - k))
                score = len(agree) + (1.5 if abs(near - k) <= 0.02 * k else 0)
                if len(agree) >= 2 and (best is None or score > best[0]):
                    best = (score, near if abs(near - k) <= 0.02 * k else round(k, 4))
            return best
        best = common(rs)
        if best is None:
            # the biggest view carries hardly any dimensions (a gear's face view): the views at the
            # sheet's own scale — all but those labelled with a scale of their own — are measured
            # instead (a PDF page printed larger than the format shows up only here: the gear's
            # A3 sheet came out √2 too big against the title block's «5:1»)
            inside = lambda x, y: any(v["box"][0] <= x <= v["box"][2] and v["box"][1] <= y <= v["box"][3]  # noqa: E731
                                      for v in labelled)
            best = common(sorted(r for x, y, r in ratios if not inside(x, y)))
        if best and abs(best[1] - sheet) > 0.01 * sheet:
            sheet, how = best[1], "размеры"
        elif best:
            how = "штамп и размеры"
    # every other view: brought from its own scale to the main view's
    scaled = [v for v in labelled if abs(v["scale"] - sheet) > 0.01 * sheet]

    def place(x, y):
        """pt on the sheet → mm of the part (views at their own scale brought to true size around
        where their middle lands at the main view's scale)."""
        for v in scaled:
            x0, y0, x1, y1 = v["box"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                k = v["scale"]
                return ((cx / sheet + (x - cx) / k) * PT, (cy / sheet + (y - cy) / k) * PT)
        return x * PT / sheet, y * PT / sheet

    lines = ["0", "SECTION", "2", "HEADER", "0", "ENDSEC",
             "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER"]
    for name, lt in (("MAIN", "CONTINUOUS"), ("THIN", "CONTINUOUS"), ("HIDDEN", "HIDDEN"), ("AXIS", "CENTER"),
                     ("TEXT", "CONTINUOUS")):
        lines += ["0", "LAYER", "2", name, "70", "0", "6", lt]
    lines += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    counts = {"MAIN": 0, "THIN": 0, "HIDDEN": 0, "AXIS": 0, "circles": 0, "arcs": 0}
    items = []  # (layer, fitted vertices)
    for s in page["strokes"]:
        if s["dash"]:
            layer = "AXIS" if len(s["dash"]) >= 4 else "HIDDEN"
        else:
            layer = "MAIN" if s["width"] >= limit else "THIN"
        pts = _dedupe([place(*p) for p in s["pts"]])
        if len(pts) < 2:
            continue
        counts[layer] += 1
        verts = fit_arcs(pts, **fit_kw) if layer in ("MAIN", "HIDDEN", "THIN") and len(pts) >= 4 else \
            [(x, y, 0.0, None) for x, y in pts]
        items.append((layer, verts))
    # circles drawn as several separate arcs (SOLIDWORKS: 2–4 pieces) become one circle
    pieces: dict = {}
    for n, (layer, verts) in enumerate(items):
        if len(verts) == 2 and verts[0][0] != "circle" and verts[0][3]:
            cx, cy, r, sw = verts[0][3]
            key = next((k for k in pieces if k[0] == layer and math.dist(k[1:3], (cx, cy)) < 0.02
                        and abs(k[3] - r) < 0.02), (layer, cx, cy, r))
            pieces.setdefault(key, []).append((n, sw))
    whole = set()
    for (layer, cx, cy, r), ps in pieces.items():
        if len(ps) >= 2 and abs(sum(abs(sw) for _, sw in ps) - 2 * math.pi) < 0.03:
            whole |= {n for n, _ in ps}
            lines.extend(["0", "CIRCLE", "8", layer, "10", f"{cx:.5f}", "20", f"{cy:.5f}", "40", f"{r:.5f}"])
            counts["circles"] += 1

    def emit(layer, poly):
        if len(poly) < 2:
            return
        closed = math.dist(poly[0][:2], poly[-1][:2]) < 1e-4 and len(poly) > 2
        if closed:
            poly = poly[:-1]
        lines.extend(["0", "LWPOLYLINE", "8", layer, "90", str(len(poly)), "70", "1" if closed else "0"])
        for x, y, b, _ in poly:
            lines.extend(["10", f"{x:.5f}", "20", f"{y:.5f}"])
            if b:
                lines.extend(["42", f"{b:.8f}"])
    for n, (layer, verts) in enumerate(items):
        if n in whole:
            continue
        poly: list = []
        for v in verts:
            if v[0] == "circle":
                emit(layer, poly)
                poly = []
                _, cx, cy, r = v
                lines.extend(["0", "CIRCLE", "8", layer, "10", f"{cx:.5f}", "20", f"{cy:.5f}", "40", f"{r:.5f}"])
                counts["circles"] += 1
                continue
            if v[2]:
                counts["arcs"] += 1
            poly.append(v)
        emit(layer, poly)
    for t in page["texts"]:
        x, y = place(t["x"], t["y"])
        lines.extend(["0", "TEXT", "8", "TEXT", "10", f"{x:.4f}", "20", f"{y:.4f}", "40", f"{t['size'] * PT:.3f}",
                      "50", f"{math.degrees(t['angle']):.2f}", "1", t["text"].replace("\n", " ")])
    lines += ["0", "ENDSEC", "0", "EOF"]
    with open(dxf_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"strokes": len(page["strokes"]), "texts": len(page["texts"]), "thick_pt": thick, **counts,
            "scaled_views": [{"title": v["title"], "scale": v["scale"]} for v in scaled],
            "views": [b["title"] for b in marks if b["title"]], "sheet_scale": sheet, "scale_from": how}
