"""Thin wrapper over the KOMPAS-3D COM API (API5 + API7).

All functions here must run on the single COM thread created in server.py.
"""
from __future__ import annotations

import json
import math
import os
import sys
import pythoncom
import win32com.client as wc
from win32com.client import gencache

API5_TLB = "{0422828C-F174-495E-AC5D-D31014DBBE87}"
API7_TLB = "{69AC2981-37C0-4379-84FD-5DD2F3C0A520}"

# --- KOMPAS API5 constants (ksConstants / ksConstants3D) ---
pTop_Part = -1
o3d_planeXOY, o3d_planeXOZ, o3d_planeYOZ = 1, 2, 3
o3d_sketch = 5
o3d_planeOffset = 14
o3d_baseExtrusion, o3d_bossExtrusion, o3d_cutExtrusion = 24, 25, 26
o3d_baseRotated, o3d_bossRotated, o3d_cutRotated = 27, 28, 29
o3d_baseLoft, o3d_bossLoft, o3d_cutLoft = 30, 31, 32
o3d_axis2Planes, o3d_circularCopy = 9, 36
o3d_edge, o3d_fillet = 7, 34
etBlind, etThroughAll = 0, 1
dtNormal, dtReverse, dtBoth, dtMiddlePlane = 0, 1, 2, 3
ko_DocumentParam = 35
lt_DocSheetStandart, lt_DocFragment = 1, 3

PLANES = {"XY": o3d_planeXOY, "XZ": o3d_planeXOZ, "YZ": o3d_planeYOZ}
RASTER_FORMATS = {"bmp": 0, "gif": 1, "jpg": 2, "png": 3, "tif": 4}


class KompasError(RuntimeError):
    pass


class Kompas:
    def __init__(self):
        self.k5 = None
        self.k7 = None
        self.sketches: dict[int, object] = {}
        self.sketch_planes: dict[int, str] = {}
        self._next_id = 1
        self.cur = None        # ksDocument3D / ksDocument2D created by this session
        self.cur_kind = None   # "3d" | "2d"
        self.constraints = True        # sketch constraints (merge, tangent, horizontal, vertical)
        self.constraint_count: dict[str, int] = {}

    # ---------- connection ----------
    def connect(self, launch: bool = True, visible: bool = True):
        if self.k5 is not None:
            try:
                _ = self.k5.Visible
                return
            except pythoncom.com_error:
                self.k5 = self.k7 = None
        try:
            raw5 = wc.GetActiveObject("Kompas.Application.5")
        except pythoncom.com_error:
            if not launch:
                raise KompasError("КОМПАС-3D не запущен")
            raw5 = wc.Dispatch("Kompas.Application.5")
        # Early binding via the KOMPAS type libraries: late binding mixes up
        # properties and methods on API5 objects.
        api5 = gencache.EnsureModule(API5_TLB, 0, 1, 0)
        api7 = gencache.EnsureModule(API7_TLB, 0, 1, 0)
        self.k5 = api5.KompasObject(raw5._oleobj_.QueryInterface(api5.KompasObject.CLSID, pythoncom.IID_IDispatch))
        raw7 = wc.Dispatch("Kompas.Application.7")
        self.k7 = api7.IApplication(raw7._oleobj_.QueryInterface(api7.IApplication.CLSID, pythoncom.IID_IDispatch))
        self.k5.Visible = visible

    def status(self) -> dict:
        info: dict = {"python_com": "ok"}
        try:
            self.connect(launch=False)
        except KompasError as e:
            info["kompas_running"] = False
            info["message"] = str(e)
            return info
        k7 = self.k7
        info["kompas_running"] = True
        info["visible"] = bool(self.k5.Visible)
        docs = k7.Documents
        info["open_documents"] = [docs.Item(i).PathName or docs.Item(i).Name or "(не сохранён)"
                                  for i in range(docs.Count)]
        active = k7.ActiveDocument
        info["active_document"] = (active.PathName or active.Name or "(не сохранён)") if active else None
        info["active_document_type"] = int(active.DocumentType) if active else None
        return info

    # ---------- current document ----------
    # KOMPAS's "active document" can silently switch to another open window, so the document the
    # session created last is remembered and re-activated before every operation.
    def _alive_ref(self, d) -> int:
        try:
            return int(d.reference) if d is not None else 0
        except pythoncom.com_error:
            return 0

    def _doc7_by_ref(self, ref: int):
        docs = self.k7.Documents
        for i in range(docs.Count):
            doc = docs.Item(i)
            if doc is not None and int(doc.Reference) == ref:
                return doc
        return None

    def _activate(self):
        """Makes the session's current document active again; returns its API7 object (or None)."""
        if not self._alive_ref(self.cur):
            self.cur = None
            return None
        doc7 = self._doc7_by_ref(self._alive_ref(self.cur))
        if doc7 is not None and not doc7.Active:
            doc7.Active = True
        return doc7

    def active_doc7(self):
        doc7 = self._activate()
        doc7 = doc7 or self.k7.ActiveDocument
        if doc7 is None:
            raise KompasError("Нет активного документа")
        return doc7

    # ---------- 3D ----------
    def doc3d(self):
        self._activate()
        if self.cur is not None and self.cur_kind == "3d":
            return self.cur
        d = self.k5.ActiveDocument3D()
        if d is None:
            raise KompasError("Нет активной 3D-детали. Вызовите new_part.")
        return d

    def part(self):
        return self.doc3d().GetPart(pTop_Part)

    def new_part(self, name: str | None = None) -> dict:
        self.connect()
        d = self.k5.Document3D()
        if not d.Create(False, True):
            raise KompasError("Не удалось создать деталь")
        self.cur, self.cur_kind = d, "3d"
        p = d.GetPart(pTop_Part)
        if name:
            p.name = name
            p.Update()
        self.sketches.clear()
        return {"created": True, "name": name}

    def close_current(self) -> bool:
        """Closes the document this session made, without saving (a test build)."""
        doc7 = self._activate()
        if doc7 is None and self.cur is not None and self._alive_ref(self.cur):
            # just built, it is the active one (KOMPAS's Documents list can miss it); API5's close()
            # is not used: it may ask whether to save, and that dialog blocks KOMPAS
            doc7 = self.k7.ActiveDocument
        if doc7 is None:
            return False
        doc7.Close(0)   # kdDoNotSaveChanges
        self.cur, self.cur_kind = None, None
        self.sketches.clear()
        return True

    def _plane(self, plane: str, offset: float):
        part = self.part()
        key = plane.upper()
        if key not in PLANES:
            raise KompasError(f"Плоскость {plane}: допустимо XY, XZ, YZ")
        base = part.GetDefaultEntity(PLANES[key])
        if not offset:
            return base
        ent = part.NewEntity(o3d_planeOffset)
        pd = ent.GetDefinition()
        pd.SetPlane(base)
        pd.direction = offset > 0
        pd.offset = abs(offset)
        ent.hidden = True
        ent.Create()
        return ent

    def sketch(self, plane: str, shapes: list[dict], offset: float = 0.0) -> int:
        """Shapes use natural coordinates: XY → (X, Y), XZ → (X, Z), YZ → (Y, Z); the plane's normal
        (+Z, +Y, +X) is the positive direction for offset and extrusion. KOMPAS's own sketch axes
        differ on XZ and YZ (see tests/plane_axes.py), so shapes are converted here."""
        key = plane.upper()
        part = self.part()
        sk = part.NewEntity(o3d_sketch)
        sd = sk.GetDefinition()
        sd.SetPlane(self._plane(key, -offset if key == "YZ" else offset))
        sk.Create()
        d2 = sd.BeginEdit()
        try:
            pieces = []
            for s in shapes:
                pieces += draw_shape(d2, to_sketch_coords(key, s), style=1)
            if self.constraints:
                try:
                    c = constrain(self.k5, d2, pieces)
                    if c["сдвиг"] > 1e-5:
                        # the solver moved the drawing's geometry: the sketch is drawn again as it
                        # was, without constraints (the exact geometry matters more)
                        for p in pieces:
                            d2.ksDeleteObj(p[1])
                        for s in shapes:
                            draw_shape(d2, to_sketch_coords(key, s), style=1)
                        c = {"эскизов без связей": 1}
                    c.pop("сдвиг", None)
                    for name, n in c.items():
                        self.constraint_count[name] = self.constraint_count.get(name, 0) + n
                except Exception:  # noqa: BLE001 - constraints are a bonus; the sketch is already exact
                    self.constraint_count["не наложено"] = self.constraint_count.get("не наложено", 0) + 1
        finally:
            sd.EndEdit()
        sid = self._next_id
        self._next_id += 1
        self.sketches[sid] = sk
        self.sketch_planes[sid] = key
        return sid

    def _get_sketch(self, sid: int):
        if sid not in self.sketches:
            raise KompasError(f"Эскиз {sid} не найден (известны: {list(self.sketches)})")
        return self.sketches[sid]

    def _volume(self) -> float:
        return self.part().CalcMassInertiaProperties(0).v * 1e3  # cm3 -> mm3

    def _with_volume_check(self, op: str, fn) -> dict:
        """Run a feature and report the volume change: a feature that changes nothing is
        almost always a mistake (wrong offset, direction or a sketch inside existing material)."""
        before = self._volume()
        fn()
        after = self._volume()
        out = {"created": True, "operation": op, "volume_mm3": round(after, 3),
               "volume_change_mm3": round(after - before, 3)}
        if abs(after - before) < 1e-6:
            out["warning"] = ("Объём не изменился: операция ничего не добавила и не вырезала. "
                              "Проверьте offset эскиза (высоту плоскости), направление reverse и "
                              "расположение контура относительно существующего тела.")
        return out

    def extrude(self, sid: int, depth: float, cut: bool = False, reverse: bool = False,
                through_all: bool = False, symmetric: bool = False) -> dict:
        if not through_all and not depth > 0:
            raise KompasError("Не задана глубина: укажите depth > 0 (мм) или through_all=true.")
        return self._with_volume_check("cut" if cut else "extrude",
                                       lambda: self._extrude(sid, depth, cut, reverse, through_all, symmetric))

    def _extrude(self, sid, depth, cut, reverse, through_all, symmetric):
        part = self.part()
        ent = part.NewEntity(o3d_cutExtrusion if cut else (o3d_bossExtrusion if self._has_body() else o3d_baseExtrusion))
        ed = ent.GetDefinition()
        if cut:
            # KOMPAS cuts in the "normal" direction go against the sketch normal
            # (checked by tests/cut_direction.py); flip so reverse=False always means +normal.
            reverse = not reverse
        if self.sketch_planes.get(sid) == "YZ":
            reverse = not reverse  # KOMPAS's YZ plane faces −X; ours faces +X
        if symmetric:
            ed.directionType = dtMiddlePlane
        else:
            ed.directionType = dtReverse if reverse else dtNormal
        end_type = etThroughAll if through_all else etBlind
        forward = not reverse
        ed.SetSideParam(forward, end_type, float(depth), 0.0, False)
        if cut:
            ed.cut = True
        ed.SetSketch(self._get_sketch(sid))
        ok = ent.Create()
        self._last_entity = ent
        if not ok:
            raise KompasError("Операция выдавливания не выполнена (проверьте замкнутость контура)")

    def _drop_if_invalid(self, what: str) -> None:
        """Checked after every feature, on any drawing: one KOMPAS marks with an error (a red «!»,
        e.g. «Тело с нарушенной топологией») is removed and reported as not built — a body with
        an error never stays in the model."""
        if self.feature_valid() is False:
            try:
                self.doc3d().DeleteObject(self._last_entity)
            finally:
                self._last_entity = None
            raise KompasError(f"{what}: КОМПАС пометил элемент ошибкой (нарушена топология тела) — элемент убран")

    def feature_valid(self, ent=None) -> bool | None:
        """False when KOMPAS marks the feature with an error (a red «!» in the tree, e.g. «Тело с
        нарушенной топологией» — the feature is created and changes the volume, so nothing else
        shows it). None when it cannot be told."""
        ent = ent if ent is not None else getattr(self, "_last_entity", None)
        if ent is None:
            return None
        try:
            obj = self.k5.TransferInterface(ent, 2, 0)   # ksAPI7Dual: the API7 object of the feature
            return bool(wc.Dispatch(obj).Valid)
        except Exception:  # noqa: BLE001
            return None

    def loft(self, plane: str, sections: list, cut: bool = False, auto_path: bool = False) -> dict:
        """Operation «по сечениям» through sketches on parallel planes: sections = [(offset, shapes)]
        in the plane's natural coordinates (a helical gear: the tooth outline turned a little more on
        each plane). The sections must have their pieces in the same order."""
        if len(sections) < 2:
            raise KompasError("по сечениям: нужно не меньше двух сечений")
        sids = [self.sketch(plane, shapes, float(off)) for off, shapes in sections]

        def go():
            part = self.part()
            kind = o3d_cutLoft if cut else (o3d_bossLoft if self._has_body() else o3d_baseLoft)
            ent = part.NewEntity(kind)
            ld = ent.GetDefinition()
            # autoPath off by default: piece k of a section goes to piece k of the next — with it on,
            # KOMPAS paired a whole gear's outline crosswise and the body came out torn; a short
            # symmetric contour (a bevel gear's tooth space) needs it on instead (op "auto_path")
            ld.SetLoftParam(False, False, bool(auto_path))
            arr = ld.Sketchs()
            for sid in sids:
                arr.Add(self._get_sketch(sid))
            self._last_entity = ent
            if not ent.Create():
                raise KompasError("Операция по сечениям не выполнена (сечения не сопоставились)")
        return self._with_volume_check("cut_loft" if cut else "loft", go)

    def circular_copy(self, entities: list, plane: str, u: float, v: float, count: int) -> dict:
        """The given features copied count times evenly round an axis normal to the plane through
        its point (u, v) in the plane's natural coordinates (a gear's tooth round the gear)."""
        part = self.part()
        key = plane.upper()
        # the axis: where the two planes square to this one through (u, v) meet
        pu, pv = {"XY": ("YZ", "XZ"), "XZ": ("YZ", "XY"), "YZ": ("XZ", "XY")}[key]
        ax = part.NewEntity(o3d_axis2Planes)
        ad = ax.GetDefinition()
        # an offset plane from YZ by +u lands at X = −u (measured: the gear's teeth went round a
        # centre mirrored in X); from XY by +v at Z = +v
        ad.SetPlane(1, self._plane(pu, -u if pu == "YZ" else u))
        ad.SetPlane(2, self._plane(pv, -v if pv == "YZ" else v))
        ax.hidden = True
        if not ax.Create():
            raise KompasError("массив по кругу: ось не построена")

        def go():
            ent = part.NewEntity(o3d_circularCopy)
            cd = ent.GetDefinition()
            cd.SetAxis(ax)
            cd.count1 = 1
            cd.SetCopyParamAlongDir(int(count), 360.0, True, False)
            arr = cd.GetOperationArray()
            for e in entities:
                arr.Add(e)
            self._last_entity = ent
            if not ent.Create():
                raise KompasError("массив по кругу не выполнен")
        return self._with_volume_check("circular_copy", go)

    def revolve(self, sid: int, angle: float = 360.0, cut: bool = False, reverse: bool = False) -> dict:
        return self._with_volume_check("cut_revolve" if cut else "revolve",
                                       lambda: self._revolve(sid, angle, cut, reverse))

    def _revolve(self, sid, angle, cut, reverse=False):
        part = self.part()
        ent = part.NewEntity(o3d_cutRotated if cut else (o3d_bossRotated if self._has_body() else o3d_baseRotated))
        rd = ent.GetDefinition()
        rd.directionType = dtReverse if reverse else dtNormal
        rd.SetThinParam(False, 0, 0.0, 0.0)
        rd.SetSideParam(not reverse, float(angle))
        rd.SetSketch(self._get_sketch(sid))
        if not ent.Create():
            raise KompasError("Вращение не выполнено (нужна осевая линия style=3 в эскизе)")

    def fillet(self, radius: float, points: list, tangent: bool = True) -> dict:
        """Rounds the edges passing through the given points (model mm) with one radius. An item of
        points may also be a list of candidate points for one edge: the first one on an edge counts
        (an edge whose extent along its length is known only roughly)."""
        if radius <= 0 or not points:
            raise KompasError("скругление: нужны radius > 0 и points — точки на скругляемых рёбрах")
        part = self.part()
        edges = []
        for p in points:
            cands = p if p and isinstance(p[0], (list, tuple)) else [p]
            for q in cands:
                coll = part.EntityCollection(o3d_edge)
                if coll.SelectByPoint(float(q[0]), float(q[1]), float(q[2])) and coll.GetCount():
                    edges.append(coll.GetByIndex(0))
                    break
        if not edges:
            raise KompasError("скругление: ни одна точка не лежит на ребре детали")

        def go():
            ent = part.NewEntity(o3d_fillet)
            fd = ent.GetDefinition()
            fd.radius = float(radius)
            fd.tangent = bool(tangent)   # False: only the given edges, not the chain tangent to them
            arr = fd.array()
            for e in edges:
                arr.Add(e)
            self._last_entity = ent
            if not ent.Create():
                raise KompasError(f"скругление R{radius:g} не выполнено")
        r = self._with_volume_check("fillet", go)
        return {**r, "edges": len(edges)}

    def build_part(self, operations: list[dict], name: str | None = None, new: bool = True,
                   expected_volume_mm3: float | None = None, keep_going: bool = False) -> dict:
        """Runs a whole feature list in one call (one LLM round trip instead of one per feature).
        Each operation: {op: extrude|cut|revolve, plane, offset, shapes, depth, reverse, through_all,
        symmetric, angle}. Stops at the first failing operation and reports what was done; with
        keep_going a failing cut or fillet is skipped (listed in "skipped") and the rest is built."""
        if new:
            self.new_part(name)
        self.constraint_count = {}
        results, failed, skipped = [], None, []
        made = []   # (operation number, its feature) — checked for KOMPAS's error marks at the end
        operations = [normalize_op(op) for op in operations]
        for i, op in enumerate(operations, 1):
            kind = op.get("op", "extrude") if isinstance(op, dict) else "?"
            self._last_entity = None
            try:
                if isinstance(op, dict) and kind == "fillet":
                    r = self.fillet(float(op.get("radius", 0)), op.get("points") or [], bool(op.get("tangent", True)))
                    self._drop_if_invalid("скругление")
                    results.append({"n": i, "op": kind, "volume_change_mm3": r["volume_change_mm3"],
                                    "edges": r["edges"]})
                    made.append((i, self._last_entity))
                    continue
                if isinstance(op, dict) and kind == "circular":
                    # copies of earlier operations (their numbers in "of", the one before by default)
                    want = op.get("of") or [i - 1]
                    ents = [e for n, e in made if n in want and e is not None]
                    if len(ents) != len(want):
                        raise KompasError("массив по кругу: копируемая операция не построена")
                    r = self.circular_copy(ents, op.get("plane", "XY"), float(op.get("x", 0)),
                                           float(op.get("y", 0)), int(op.get("count", 2)))
                    self._drop_if_invalid("массив по кругу")
                    results.append({"n": i, "op": kind, "volume_change_mm3": r["volume_change_mm3"]})
                    made.append((i, self._last_entity))
                    continue
                if isinstance(op, dict) and kind == "loft":
                    secs = [(float(s.get("offset", 0)), clean_shapes(s["shapes"])) for s in op.get("sections") or []]
                    try:
                        r = self.loft(op.get("plane", "XY"), secs, bool(op.get("cut")), bool(op.get("auto_path")))
                        self._drop_if_invalid("по сечениям")
                    except KompasError:
                        # the same with every arc as a fine polyline (KOMPAS sometimes cannot match arcs)
                        flat = [(o, arcs_to_lines(sh) or sh) for o, sh in secs]
                        r = self.loft(op.get("plane", "XY"), flat, bool(op.get("cut")), bool(op.get("auto_path")))
                        self._drop_if_invalid("по сечениям (дуги ломаной)")
                        r["warning"] = "дуги сечений заменены ломаной с шагом 2°"
                    results.append({"n": i, "op": kind, "volume_change_mm3": r["volume_change_mm3"],
                                    **({"warning": r["warning"]} if "warning" in r else {})})
                    made.append((i, self._last_entity))
                    continue
                if not isinstance(op, dict) or not op.get("shapes"):
                    raise KompasError("у операции нет списка shapes с примитивами эскиза; каждая операция — "
                                      "отдельный объект {op, plane, offset, shapes, depth} в общем списке operations")
                op["shapes"] = clean_shapes(op["shapes"])
                sid = self.sketch(op.get("plane", "XY"), op["shapes"], float(op.get("offset", 0)))
                if kind == "revolve":
                    r = self.revolve(sid, float(op.get("angle", 360)), bool(op.get("cut", False)),
                                     bool(op.get("reverse", False)))
                    self._drop_if_invalid("вращение")
                else:
                    def run(sid):
                        return self.extrude(sid, float(op.get("depth", 0)), cut=(kind == "cut" or bool(op.get("cut"))),
                                            reverse=bool(op.get("reverse", False)),
                                            through_all=bool(op.get("through_all", False)),
                                            symmetric=bool(op.get("symmetric", False)))
                    try:
                        r = run(sid)
                        if r.get("volume_mm3", 1) <= 0:
                            # KOMPAS took the contour the wrong way round (seen with a short arc
                            # ending a hair off a line): the body turned inside out — undo, retry
                            self.doc3d().DeleteObject(self._last_entity)
                            raise KompasError("тело вывернулось")
                        self._drop_if_invalid("выдавливание")
                    except KompasError:
                        # KOMPAS sometimes will not close a contour where a short arc meets its
                        # neighbours, or builds it with broken topology: the same contour with the
                        # arcs as fine polylines (2° steps)
                        flat = arcs_to_lines(op["shapes"])
                        if flat is None:
                            raise
                        r = run(self.sketch(op.get("plane", "XY"), flat, float(op.get("offset", 0))))
                        self._drop_if_invalid("выдавливание (дуги ломаной)")
                        r["warning"] = ("дуги контура заменены ломаной с шагом 2° (с дугами КОМПАС не замкнул контур "
                                        "или пометил элемент ошибкой)")
                item = {"n": i, "op": kind, "volume_change_mm3": r["volume_change_mm3"],
                        **({"warning": r["warning"]} if "warning" in r else {})}
                # A boss that grew the body by less than profile area × depth went partly into material
                # that already existed — almost always a forgotten offset.
                area = profile_area(op["shapes"])
                depth = float(op.get("depth", 0))
                if (kind == "extrude" and not op.get("cut") and area and depth > 0 and not op.get("symmetric")
                        and "warning" not in item and r["volume_change_mm3"] < 0.98 * area * depth):
                    item["warning"] = (f"Тело выросло на {r['volume_change_mm3']:.1f} мм³, а площадь эскиза × depth = "
                                       f"{area * depth:.1f} мм³: часть элемента ушла внутрь уже существующего тела. "
                                       "Обычно это забытый offset: элемент поверх тела высотой H строится на offset=H.")
                results.append(item)
                made.append((i, self._last_entity))
            except (KompasError, pythoncom.com_error) as e:
                is_cut = isinstance(op, dict) and (kind in ("cut", "fillet") or bool(op.get("cut")))
                if keep_going and i > 1 and is_cut:
                    # a feature KOMPAS could not build stays in the tree with an error mark: away
                    if self._last_entity is not None:
                        try:
                            self.doc3d().DeleteObject(self._last_entity)
                        except Exception:  # noqa: BLE001
                            pass
                    skipped.append({"n": i, "op": kind, "error": str(e),
                                    "comment": op.get("comment", "") if isinstance(op, dict) else ""})
                    continue
                failed = {"n": i, "op": kind, "error": str(e)}
                break
            except Exception as e:  # noqa: BLE001 - the model needs a hint, not a bare Python error
                failed = {"n": i, "op": kind, "error": f"{type(e).__name__}: {e}. Проверьте поля операции: "
                          "op, plane, offset, depth — числа; shapes — список примитивов нужного формата."}
                break
        out = {"operations_done": len(results), "operations_total": len(operations), "results": results,
               **self.mass_properties(), **self.gabarit()}
        if skipped:
            out["skipped"] = skipped
        # features built but marked with an error by KOMPAS (a red «!» in the tree, e.g. «Тело с
        # нарушенной топологией»): the volume looks fine, only this shows them
        invalid = []
        for n, ent in made:
            if ent is not None and self.feature_valid(ent) is False:
                op = operations[n - 1] if isinstance(operations[n - 1], dict) else {}
                invalid.append({"n": n, "op": op.get("op", "?"), "comment": op.get("comment", "")})
                for it in results:
                    if it["n"] == n:
                        it["warning"] = "КОМПАС пометил элемент ошибкой (например, нарушена топология тела)"
        if invalid:
            out["invalid"] = invalid
        if any(self.constraint_count.values()):
            out["sketch_constraints"] = {k: v for k, v in self.constraint_count.items() if v}
        if failed:
            out["failed"] = failed
        if expected_volume_mm3:
            diff = out["volume_mm3"] - float(expected_volume_mm3)
            out["volume_diff_mm3"] = round(diff, 3)
            out["volume_matches"] = abs(diff) <= max(1.0, 0.005 * float(expected_volume_mm3))
        return out

    def _has_body(self) -> bool:
        return self.part().CalcMassInertiaProperties(0).v > 0

    def mass_properties(self) -> dict:
        # With flags 0 API5 returns cm / cm2 / cm3 / g; convert lengths to mm.
        mp = self.part().CalcMassInertiaProperties(0)
        return {"volume_mm3": round(mp.v * 1e3, 3), "area_mm2": round(mp.F * 1e2, 3),
                "mass_g": round(mp.m, 3),
                "center_mm": [round(v * 10, 4) for v in (mp.xc, mp.yc, mp.zc)]}

    def gabarit(self) -> dict:
        """Overall size of the body along X, Y, Z (mm) — what the drawing's overall dimensions must match."""
        try:
            ok, x1, y1, z1, x2, y2, z2 = self.part().GetGabarit(True, True)
        except (pythoncom.com_error, ValueError, TypeError):
            return {}
        if not ok or self._volume() <= 0 or x2 < x1:  # no body: KOMPAS returns ±1e300 sentinels
            return {}
        r = lambda v: round(v, 3) + 0.0  # noqa: E731  (+0.0 turns -0.0 into 0.0)
        return {"size_mm": [r(x2 - x1), r(y2 - y1), r(z2 - z1)], "bbox_min_mm": [r(x1), r(y1), r(z1)]}

    def feature_tree(self) -> list[str]:
        out = []
        coll = self.part().EntityCollection(0)
        for i in range(coll.GetCount()):
            e = coll.GetByIndex(i)
            if e.name and int(e.type) > 4 and int(e.type) not in (71, 72, 73):
                out.append(e.name)
        return out

    # ---------- 2D ----------
    def new_fragment(self, sheet: bool = False) -> dict:
        self.connect()
        d = self.k5.Document2D()
        p = self.k5.GetParamStruct(ko_DocumentParam)
        p.Init()
        p.type = lt_DocSheetStandart if sheet else lt_DocFragment
        if not d.ksCreateDocument(p):
            raise KompasError("Не удалось создать 2D-документ")
        self.cur, self.cur_kind = d, "2d"
        return {"created": True, "type": "sheet" if sheet else "fragment"}

    def doc2d(self):
        self._activate()
        if self.cur is not None and self.cur_kind == "2d":
            return self.cur
        d = self.k5.ActiveDocument2D()
        if d is None:
            raise KompasError("Нет активного 2D-документа. Вызовите new_fragment.")
        return d

    def draw_2d(self, shapes: list[dict]) -> int:
        d = self.doc2d()
        for s in shapes:
            draw_shape(d, s, style=1)
        return len(shapes)

    # ---------- files ----------
    def _converter(self, name: str):
        """dwgdxfImp / dwgdxfExp: KOMPAS's DXF/DWG converter libraries (SaveAs/Open by extension
        do not use them: SaveAs('x.dxf') writes a native KOMPAS file under that name)."""
        bin_dir = self.k5.ksSystemPath(0)  # sptBINFILES
        lib = os.path.join(os.path.dirname(bin_dir.rstrip("\\/")), "Libs", "ImpExp", name + ".rtw")
        if not os.path.exists(lib):
            raise KompasError(f"Нет конвертера КОМПАС {lib}")
        return self.k7.Converter(lib)

    def _close_docs_in(self, folder: str) -> None:
        """Closes, without saving, the documents KOMPAS has open from a (temporary) folder."""
        folder = os.path.normcase(os.path.abspath(folder))
        docs = self.k7.Documents
        for i in range(docs.Count - 1, -1, -1):
            try:
                d = docs.Item(i)
                if os.path.normcase(str(d.PathName or "")).startswith(folder):
                    d.Close(0)
            except Exception:  # noqa: BLE001
                continue

    @staticmethod
    def _check_foreign(path: str) -> None:
        if not os.path.exists(path):
            raise KompasError(f"КОМПАС не создал файл {path}")
        with open(path, "rb") as f:
            if f.read(2) == b"PK":  # a native KOMPAS document (zip), not the requested format
                os.remove(path)
                raise KompasError(f"КОМПАС сохранил свой формат вместо {os.path.splitext(path)[1]}")

    def convert(self, src: str, dst: str) -> str:
        """Converts a 2D drawing by extension: .dxf/.dwg -> .frw/.cdw (import),
        .cdw/.frw -> .dxf/.dwg (export), otherwise opens invisibly and saves as dst."""
        self.connect()
        src, dst = os.path.abspath(src), os.path.abspath(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        se, de = (os.path.splitext(p)[1].lower() for p in (src, dst))
        foreign = (".dxf", ".dwg")
        if se in foreign or de in foreign:
            # The DXF/DWG converters fail silently for some folders (the app's uploads under
            # %LOCALAPPDATA% for one) while the same file converts in %TEMP%: always convert
            # temp copies and move the result.
            import shutil
            import tempfile
            work = tempfile.mkdtemp(prefix="kompasmcp-")
            a = os.path.join(work, "in" + se)
            shutil.copyfile(src, a)
            b = os.path.join(work, "out" + de)
            try:
                if se in foreign and de in foreign:  # dwg <-> dxf through a KOMPAS fragment
                    frw = os.path.join(work, "mid.frw")
                    if not self._converter("dwgdxfImp").Convert(a, frw, 0, False) or not os.path.exists(frw):
                        raise KompasError(f"КОМПАС не импортировал {src}")
                    a = frw
                if de in foreign:
                    cmd = 2 if de == ".dwg" else 1  # dwgdxfExp commands: 1 = DXF, 2 = DWG
                    if not self._converter("dwgdxfExp").Convert(a, b, cmd, False):
                        raise KompasError(f"КОМПАС не экспортировал {src} в {de}")
                    self._check_foreign(b)
                elif not self._converter("dwgdxfImp").Convert(a, b, 0, False):
                    raise KompasError(f"КОМПАС не импортировал {src}")
            finally:
                self._close_docs_in(work)  # the converters leave what they opened as tabs in KOMPAS
            if not os.path.exists(b):
                raise KompasError(f"КОМПАС не создал файл {dst}")
            shutil.move(b, dst)
        else:
            doc = self.k7.Documents.Open(src, False, True)
            if doc is None:
                raise KompasError(f"КОМПАС не открыл файл {src}")
            try:
                doc.SaveAs(dst)
            finally:
                doc.Close(0)  # kdDoNotSaveChanges
        if not os.path.exists(dst):
            raise KompasError(f"КОМПАС не создал файл {dst}")
        return dst

    def save(self, path: str) -> str:
        doc = self.active_doc7()
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc.SaveAs(path)
        return path

    def export(self, path: str) -> str:
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        doc = self.active_doc7()
        if ext in RASTER_FORMATS:
            return self.screenshot(path)
        if ext in ("dxf", "dwg"):
            # through a temporary copy of the drawing: the DXF/DWG converter works on files
            import tempfile
            native = {1: ".cdw", 2: ".frw"}.get(doc.DocumentType)  # ksDocumentDrawing / Fragment
            if native is None:
                raise KompasError("DXF/DWG — для 2D-документов (чертёж или фрагмент); для детали используйте step/stl")
            tmp = os.path.join(tempfile.mkdtemp(prefix="kompasmcp-"), "export" + native)
            doc.SaveAs(tmp)
            return self.convert(tmp, path)
        if os.path.exists(path):
            os.remove(path)
        fmt = {"step": 3, "stp": 3, "igs": 4, "iges": 4, "stl": 6, "x_t": 2, "x_b": 2, "sat": 1,
               "wrl": 5, "vrml": 5, "c3d": 7, "jt": 8}.get(ext)
        d3 = self.k5.ActiveDocument3D() if fmt else None
        if d3 is not None:
            # 3D exchange formats go through the model converter: SaveAs('x.step') would write a
            # native KOMPAS file under that name
            par = d3.AdditionFormatParam()
            par.Init()
            par.format = fmt
            if not d3.SaveAsToAdditionFormat(path, par) or not os.path.exists(path):
                raise KompasError(f"КОМПАС не записал {os.path.basename(path)}")
            self._check_foreign(path)
            return path
        # other formats (pdf for drawings …): API7 SaveAs by extension
        doc.SaveAs(path)
        self._check_foreign(path)
        return path

    def screenshot(self, path: str, iso: bool = True) -> str:
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
        self._activate()
        if self.cur is not None:
            d3 = self.cur if self.cur_kind == "3d" else None
            d2 = self.cur if self.cur_kind == "2d" else None
        else:
            d3 = self.k5.ActiveDocument3D()
            d2 = None if d3 is not None else self.k5.ActiveDocument2D()
        if d3 is not None:
            if iso:
                self.set_view("iso")
            rp = d3.RasterFormatParam()
            target = d3
        else:
            if d2 is None:
                raise KompasError("Нет активного документа")
            rp = d2.RasterFormatParam()
            target = d2
        rp.Init()
        rp.format = RASTER_FORMATS.get(ext, 3)
        rp.colorBPP = 24
        rp.extResolution = 120
        rp.extScale = 1.0
        rp.greyScale = False
        rp.onlyThinLine = False
        rp.colorType = 3
        rp.rangeIndex = 0
        if not target.SaveAsToRasterFormat(path, rp):
            raise KompasError("Не удалось сохранить изображение")
        return path

    def hide_construction(self, hide: bool = True) -> None:
        """Hides the default planes/axes/origin so snapshots show only the body."""
        part = self.part()
        for t in (o3d_planeXOY, o3d_planeXOZ, o3d_planeYOZ, 71, 72, 73, 4):
            try:
                e = part.GetDefaultEntity(t)
                if e is not None:
                    e.hidden = hide
                    e.Update()
            except pythoncom.com_error:
                pass

    # How the drawing views of ESKD (first-angle projection; X right, Y depth, Z up) map onto KOMPAS's
    # named orientations; measured on an asymmetric test part. The top view comes out with X pointing
    # down and has to be turned 90° counter-clockwise.
    GOST_VIEWS = {"главный": ("left", 0), "сверху": ("top", 90), "слева": ("back", 0), "изометрия": ("iso", 0)}

    def projections(self, out_dir: str) -> dict[str, str]:
        """Snapshots of the current part as the standard drawing views."""
        from PIL import Image as PILImage
        os.makedirs(out_dir, exist_ok=True)
        self.hide_construction(True)
        out = {}
        try:
            for name, (view, rot) in self.GOST_VIEWS.items():
                self.set_view(view)
                p = os.path.join(out_dir, f"proj_{view}.png")
                self.screenshot(p, iso=False)
                if rot:
                    with PILImage.open(p) as im:
                        im.rotate(rot, expand=True, fillcolor="white").save(p)
                out[name] = p
        finally:
            self.hide_construction(False)
            self.set_view("iso")
        return out

    def set_view(self, name: str) -> None:
        names = {"iso": "#Изометрия", "dimetric": "#Диметрия", "front": "#Спереди", "top": "#Сверху",
                 "left": "#Слева", "right": "#Справа", "back": "#Сзади", "bottom": "#Снизу"}
        d3 = self.doc3d()
        coll = d3.GetViewProjectionCollection()
        target = names.get(name, name)
        for i in range(coll.GetCount()):
            vp = coll.GetByIndex(i)
            if vp.name.lstrip("#") == target.lstrip("#"):
                vp.SetCurrent()
                d3.ZoomPrevNextOrAll(2)  # zoom to fit
                return
        raise KompasError(f"Ориентация {name} не найдена")


_REQUIRED = {"rect": ("x", "y", "w", "h"), "circle": ("x", "y"), "line": ("x1", "y1", "x2", "y2"),
             "arc": ("x", "y", "r", "a1", "a2"), "text": ("x", "y", "text"), "polyline": ("points",),
             "spline": ("points",)}


def _num(v, what):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise KompasError(f"{what}: ожидалось число, получено {v!r}") from None


def _points(raw, what):
    """Accepts [[x, y], ...], [{"x":, "y":}, ...], a flat [x1, y1, x2, y2, ...] list or a JSON string."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise KompasError(f"{what}: points должен быть списком [[x, y], ...]") from None
    if not isinstance(raw, list) or not raw:
        raise KompasError(f"{what}: points должен быть непустым списком [[x, y], ...]")
    if all(isinstance(v, (int, float, str)) for v in raw):
        if len(raw) % 2:
            raise KompasError(f"{what}: в плоском списке points нечётное число координат ({len(raw)})")
        raw = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    pts = []
    for k, p in enumerate(raw, 1):
        if isinstance(p, dict):
            p = [p.get("x"), p.get("y")]
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise KompasError(f"{what}: точка {k} = {p!r}, нужна пара [x, y]")
        pts.append([_num(p[0], f"{what}, точка {k}"), _num(p[1], f"{what}, точка {k}")])
    if len(pts) < 2:
        raise KompasError(f"{what}: в ломаной меньше двух точек")
    return pts


def clean_shapes(shapes) -> list[dict]:
    """Validates sketch primitives and converts numbers given as strings, so a malformed shape
    produces a message the model can act on instead of a Python IndexError/KeyError."""
    if isinstance(shapes, dict):
        shapes = [shapes]
    out = []
    for j, s in enumerate(shapes, 1):
        if not isinstance(s, dict):
            raise KompasError(f"примитив {j}: ожидался объект вида {{type: ..., ...}}, получено {s!r}")
        t = s.get("type")
        what = f"примитив {j} ({t})"
        if t not in _REQUIRED:
            raise KompasError(f"{what}: неизвестный тип, допустимо {', '.join(_REQUIRED)}")
        missing = [k for k in _REQUIRED[t] if k not in s]
        if t == "circle" and "r" not in s and "d" not in s:
            missing.append("r или d")
        if missing:
            raise KompasError(f"{what}: не хватает полей {', '.join(missing)}")
        c = dict(s)
        for k in ("x", "y", "w", "h", "x1", "y1", "x2", "y2", "r", "d", "a1", "a2", "height"):
            if k in c:
                c[k] = _num(c[k], f"{what}, поле {k}")
        if t in ("polyline", "spline"):
            c["points"] = _points(c["points"], what)
        out.append(c)
    return out


def normalize_op(op):
    """Small models often wrap the sketch of an operation in a nested object, e.g.
    {op, depth, operations: [{plane, offset, shapes}]} or {op, depth, sketch: {plane, shapes}}.
    Merging it back saves a whole round trip to the model."""
    if not isinstance(op, dict) or op.get("shapes"):
        return op
    for key in ("sketch", "operations", "profile"):
        inner = op.get(key)
        if isinstance(inner, list) and len(inner) == 1:
            inner = inner[0]
        if isinstance(inner, dict) and inner.get("shapes"):
            merged = {k: v for k, v in op.items() if k != key}
            for k, v in inner.items():
                merged.setdefault(k, v)
            if "op" not in merged:
                merged["op"] = "cut" if merged.get("cut") else "extrude"
            return merged
    return op


def profile_area(shapes: list[dict]) -> float | None:
    """Area of a sketch made of closed rect/circle/polyline contours, where a contour inside another
    is a hole. Returns None for sketches with open lines or arcs (area unknown)."""
    import math
    contours = []
    for s in shapes:
        t = s.get("type")
        if s.get("style", 1) == 3:
            continue
        if t == "rect":
            x, y, w, h = s["x"], s["y"], s["w"], s["h"]
            contours.append((abs(w * h), (min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))))
        elif t == "circle":
            r = s["r"] if "r" in s else s["d"] / 2
            contours.append((math.pi * r * r, (s["x"] - r, s["y"] - r, s["x"] + r, s["y"] + r)))
        elif t == "polyline" and s.get("closed", True):
            p = s["points"]
            a = abs(sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1] for i in range(len(p)))) / 2
            xs, ys = [q[0] for q in p], [q[1] for q in p]
            contours.append((a, (min(xs), min(ys), max(xs), max(ys))))
        elif t == "text":
            continue
        else:
            return None
    total = 0.0
    for a, (x1, y1, x2, y2) in contours:
        depth = sum(1 for _, (u1, v1, u2, v2) in contours
                    if (u1, v1, u2, v2) != (x1, y1, x2, y2) and u1 <= x1 and v1 <= y1 and u2 >= x2 and v2 >= y2)
        total += a if depth % 2 == 0 else -a
    return total or None


def arcs_to_lines(shapes: list[dict], step_deg: float = 2.0) -> list[dict] | None:
    """The contour with every arc given by its end points as a run of short lines (ends kept exact);
    None when there is no such arc to replace."""
    out, changed = [], False
    for s in shapes:
        if s.get("type") != "arc" or "x1" not in s:
            out.append(s)
            continue
        changed = True
        cx, cy, r = s["x"], s["y"], s["r"]
        a1 = math.atan2(s["y1"] - cy, s["x1"] - cx)
        a2 = math.atan2(s["y2"] - cy, s["x2"] - cx)
        sweep = (a2 - a1) % (2 * math.pi) or 2 * math.pi  # counter-clockwise from 1 to 2
        n = max(1, math.ceil(math.degrees(sweep) / step_deg))
        pts = [(s["x1"], s["y1"])] + [(cx + r * math.cos(a1 + sweep * k / n), cy + r * math.sin(a1 + sweep * k / n))
                                      for k in range(1, n)] + [(s["x2"], s["y2"])]
        out += [{"type": "line", "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]} for a, b in zip(pts, pts[1:])]
    return out if changed else None


def to_sketch_coords(plane: str, s: dict) -> dict:
    """Converts a shape from natural plane coordinates (u, v) to KOMPAS sketch coordinates.
    Measured: XZ sketch (x, y) → model (X, −Z); YZ sketch (x, y) → model (Z, Y) both negated."""
    if plane == "XY":
        return s
    if plane == "XZ":
        pt = lambda u, v: (u, -v)                                   # noqa: E731
        ang = lambda a1, a2: (-a2, -a1)                             # mirror y → −y
    else:  # YZ
        pt = lambda u, v: (-v, -u)                                  # noqa: E731
        ang = lambda a1, a2: (270 - a2, 270 - a1)                   # mirror about y = x, then rotate
    s = dict(s)
    t = s["type"]
    if t == "rect":
        x1, y1 = pt(s["x"], s["y"])
        x2, y2 = pt(s["x"] + s["w"], s["y"] + s["h"])
        s.update(x=min(x1, x2), y=min(y1, y2), w=abs(x2 - x1), h=abs(y2 - y1))
    elif t == "line":
        (s["x1"], s["y1"]), (s["x2"], s["y2"]) = pt(s["x1"], s["y1"]), pt(s["x2"], s["y2"])
    elif t in ("polyline", "spline"):
        s["points"] = [list(pt(*p)) for p in s["points"]]
    elif t in ("circle", "text"):
        s["x"], s["y"] = pt(s["x"], s["y"])
    elif t == "arc":
        s["x"], s["y"] = pt(s["x"], s["y"])
        s["a1"], s["a2"] = ang(s["a1"], s["a2"])
        if "x1" in s:  # end points given: both planes mirror, so counter-clockwise start and end swap
            (s["x1"], s["y1"]), (s["x2"], s["y2"]) = pt(s["x2"], s["y2"]), pt(s["x1"], s["y1"])
    return s


def draw_shape(d2, s: dict, style: int = 1) -> list:
    """Draw one primitive. Units: mm. style: 1 main, 2 thin, 3 axial. Returns the drawn pieces for
    sketch constraints: ("line", ref, start, end) / ("arc", ref, start, end, centre)."""
    t = s["type"]
    st = int(s.get("style", style))
    out = []

    def seg(a, b):
        ref = d2.ksLineSeg(a[0], a[1], b[0], b[1], st)
        if st == 1:
            out.append(("line", ref, (a[0], a[1]), (b[0], b[1])))
    if t == "line":
        seg((s["x1"], s["y1"]), (s["x2"], s["y2"]))
    elif t == "circle":
        d2.ksCircle(s["x"], s["y"], s["r"] if "r" in s else s["d"] / 2.0, st)
    elif t == "rect":
        x, y, w, h = s["x"], s["y"], s["w"], s["h"]
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        for i in range(4):
            seg(pts[i], pts[(i + 1) % 4])
    elif t == "polyline":
        pts = s["points"]
        n = len(pts) if s.get("closed", True) else len(pts) - 1
        for i in range(n):
            seg(pts[i], pts[(i + 1) % len(pts)])
    elif t == "arc":
        if "x1" in s:
            # by its end points (counterclockwise from 1 to 2): they meet the neighbouring lines exactly,
            # where angles rounded to a few decimals leave micro gaps that open the contour
            ref = d2.ksArcByPoint(s["x"], s["y"], s["r"], s["x1"], s["y1"], s["x2"], s["y2"], 1, st)
            if st == 1:
                out.append(("arc", ref, (s["x1"], s["y1"]), (s["x2"], s["y2"]), (s["x"], s["y"])))
        else:
            # center, radius, start/end angles in degrees, counterclockwise
            d2.ksArcByAngle(s["x"], s["y"], s["r"], s["a1"], s["a2"], 1, st)
    elif t == "spline":
        # a smooth curve through the points (a gear tooth's involute flank)
        d2.ksBezier(0, st)
        for x, y in s["points"]:
            d2.ksPoint(float(x), float(y), 0)
        d2.ksEndObj()
    elif t == "text":
        d2.ksText(s["x"], s["y"], 0, s.get("height", 5), 0, 0, s["text"])
    else:
        raise KompasError(f"Неизвестный тип примитива: {t}")
    return out


# KOMPAS's sketch constraint types for ksSetObjConstraint — no SDK header here, found by experiment
# (tests/constraints_test.py checks them): merge points 11, tangent 15, horizontal 3, vertical 4
C_HORIZONTAL, C_VERTICAL, C_MERGE, C_TANGENT = 3, 4, 11, 15
ko_ConstraintParam = 89


def constrain(k5, d2, pieces: list) -> dict:
    """Sketch constraints for the drawn pieces: ends that meet are merged, a line and an arc (or two
    arcs) meeting smoothly are tangent, lines along the axes are horizontal or vertical — the sketch
    keeps the drawing's intent when edited, and the solver holds the contour closed.
    Point indices: a line's start 0 and end 1; an arc's centre 0, start 1, end 2."""
    mod = sys.modules[type(k5).__module__]
    cls = mod.ksConstraintParam
    count = {"совпадение": 0, "касание": 0, "горизонталь": 0, "вертикаль": 0, "не наложено": 0}

    def put(obj, kind, name, index=0, partner=0, pidx=0):
        c = cls(k5.GetParamStruct(ko_ConstraintParam)._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch))
        c.Init()
        c.constrType = kind
        c.index, c.partner, c.partnerIndex = index, partner, pidx
        count[name if d2.ksSetObjConstraint(obj, c) else "не наложено"] += 1

    ends = []   # (point, piece, point index, unit direction leaving the point along the piece)
    for p in pieces:
        if p[0] == "line":
            (x1, y1), (x2, y2) = p[2], p[3]
            L = math.hypot(x2 - x1, y2 - y1)
            if L < 1e-9:
                continue
            if abs(y2 - y1) <= 1e-9 * max(1.0, L):
                put(p[1], C_HORIZONTAL, "горизонталь")
            elif abs(x2 - x1) <= 1e-9 * max(1.0, L):
                put(p[1], C_VERTICAL, "вертикаль")
            ends += [(p[2], p, 0, ((x2 - x1) / L, (y2 - y1) / L)), (p[3], p, 1, ((x1 - x2) / L, (y1 - y2) / L))]
        else:
            cx, cy = p[4]
            for pt, idx, sgn in ((p[2], 1, 1), (p[3], 2, -1)):   # counter-clockwise from start to end
                rx, ry = pt[0] - cx, pt[1] - cy
                r = math.hypot(rx, ry) or 1e-12
                ends.append((pt, p, idx, (-ry / r * sgn, rx / r * sgn)))
    used = set()
    for i, (pa, A, ia, da) in enumerate(ends):
        if (id(A), ia) in used:
            continue
        for pb, B, ib, db in ends[i + 1:]:
            if B is A or (id(B), ib) in used or math.dist(pa, pb) > 1e-6:
                continue
            put(A[1], C_MERGE, "совпадение", ia, B[1], ib)
            used.add((id(B), ib))
            # leaving the shared point the two pieces go opposite ways along one tangent: smooth.
            # Only where the drawing is already tangent to a hair, so the solver moves nothing
            if (A[0] == "arc" or B[0] == "arc") and da[0] * db[0] + da[1] * db[1] < -math.cos(TANGENT_TOL):
                put(A[1], C_TANGENT, "касание", 0, B[1], 0)
            break
    count["сдвиг"] = _moved(k5, d2, pieces)
    return count


TANGENT_TOL = 1e-6   # rad: fillets are made exactly tangent before drawing; nothing else is forced


def _moved(k5, d2, pieces: list) -> float:
    """How far the constraint solver moved any end of the pieces (mm)."""
    mod = sys.modules[type(k5).__module__]
    worst = 0.0
    for p in pieces:
        if p[0] == "line":
            q = mod.ksLineSegParam(k5.GetParamStruct(11)._oleobj_.QueryInterface(mod.ksLineSegParam.CLSID, pythoncom.IID_IDispatch))
            d2.ksGetObjParam(p[1], q, -1)
            got = [(q.x1, q.y1), (q.x2, q.y2)]
        else:
            q = mod.ksArcByAngleParam(k5.GetParamStruct(12)._oleobj_.QueryInterface(mod.ksArcByAngleParam.CLSID, pythoncom.IID_IDispatch))
            d2.ksGetObjParam(p[1], q, -1)
            got = [(q.xc + q.rad * math.cos(math.radians(a)), q.yc + q.rad * math.sin(math.radians(a))) for a in (q.ang1, q.ang2)]
        for pt in (p[2], p[3]):
            worst = max(worst, min(math.dist(pt, g) for g in got))
    return worst
