"""Local MCP server for KOMPAS-3D. Run: python server.py (stdio transport)."""
from __future__ import annotations

import asyncio
import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

import pythoncom
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kompas_api import Kompas, KompasError

# COM objects are apartment-bound: every KOMPAS call goes through this one thread.
_executor = ThreadPoolExecutor(max_workers=1, initializer=pythoncom.CoInitialize)
_k = Kompas()

OUT_DIR = os.environ.get("KOMPAS_MCP_OUT", os.path.join(os.path.expanduser("~"), "Documents", "kompas-mcp"))

mcp = MCPServer(
    "kompas-3d",
    instructions=(
        "Управление КОМПАС-3D v23 через COM. Единицы — миллиметры. "
        "3D: стройте деталь целиком одним вызовом build_part (список операций); sketch/extrude/revolve — "
        "для правок существующей детали. Вложенные замкнутые контуры в одном эскизе дают отверстия. "
        "2D: new_fragment -> draw_2d -> save/export (dxf, pdf)."
    ),
)


async def _run(fn, *args, **kwargs) -> Any:
    # ToolError text reaches the model; any other exception is reported as a bare crash.
    def call():
        try:
            return fn(*args, **kwargs)
        except KompasError as e:
            raise ToolError(str(e)) from e
        except pythoncom.com_error as e:
            raise ToolError(f"Ошибка COM: {e}") from e
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return await asyncio.get_running_loop().run_in_executor(_executor, call)


def _out(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(OUT_DIR, path)


Shape = dict[str, Any]
SHAPES_DOC = (
    "Список примитивов: {type:'rect',x,y,w,h} | {type:'circle',x,y,r или d} | "
    "{type:'line',x1,y1,x2,y2} | {type:'polyline',points:[[x,y],...],closed} | "
    "{type:'arc',x,y,r,a1,a2} | {type:'text',x,y,text,height}. "
    "Необязательный style: 1 основная, 2 тонкая, 3 осевая (ось вращения)."
)


@mcp.tool()
async def diagnostics(launch_if_needed: bool = False) -> dict:
    """Проверить связь с КОМПАС-3D: запущен ли, версия, открытые документы."""
    def go():
        if launch_if_needed:
            _k.connect(launch=True)
        st = _k.status()
        st["output_dir"] = OUT_DIR
        return st
    try:
        return await _run(go)
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc(limit=3)}


@mcp.tool()
async def launch_kompas(visible: bool = True) -> dict:
    """Запустить КОМПАС-3D (или подключиться к уже запущенному)."""
    await _run(_k.connect, True, visible)
    return await _run(_k.status)


@mcp.tool()
async def new_part(name: str | None = None) -> dict:
    """Создать новую 3D-деталь и сделать её активной."""
    return await _run(_k.new_part, name)


@mcp.tool(description="Создать эскиз на плоскости XY/XZ/YZ, смещённой на offset мм вдоль нормали "
          "(для XY offset — это высота Z, на которой лежит эскиз: чтобы поставить второй элемент "
          "на верх тела высотой H, задайте offset=H). " + SHAPES_DOC +
          " Возвращает sketch_id для extrude/revolve.")
async def sketch(plane: Literal["XY", "XZ", "YZ"], shapes: list[Shape], offset: float = 0.0) -> dict:
    sid = await _run(_k.sketch, plane, shapes, offset)
    return {"sketch_id": sid}


@mcp.tool()
async def extrude(sketch_id: int, depth: float, cut: bool = False, reverse: bool = False,
                  through_all: bool = False, symmetric: bool = False) -> dict:
    """Выдавить эскиз на depth мм по нормали плоскости (reverse — в обратную сторону; cut=True — вырезать;
    through_all — насквозь; symmetric — в обе стороны). Возвращает объём и его изменение;
    поле warning означает, что операция ничего не изменила и её надо исправить."""
    return await _run(_checked, _k.extrude, "выдавливание", sketch_id, depth, cut, reverse, through_all, symmetric)


def _checked(fn, what, *args):
    """A feature KOMPAS marks with an error (broken topology) never stays: removed, reported."""
    r = fn(*args)
    _k._drop_if_invalid(what)
    return r


@mcp.tool()
async def revolve(sketch_id: int, angle: float = 360.0, cut: bool = False) -> dict:
    """Вращение эскиза вокруг осевой линии (примитив со style=3) на angle градусов."""
    return await _run(_checked, _k.revolve, "вращение", sketch_id, angle, cut)


@mcp.tool(description=(
    "ГЛАВНЫЙ ИНСТРУМЕНТ: построить всю деталь одним вызовом по списку операций — быстрее, чем "
    "sketch+extrude по одной. operations: [{op: 'extrude'|'cut'|'revolve', plane: 'XY'|'XZ'|'YZ', "
    "offset: мм вдоль нормали плоскости, shapes: [примитивы], depth: мм (обязательно для extrude/cut, "
    "если не through_all), reverse, through_all, "
    "symmetric, angle (для revolve, нужна ось style=3)}]. Координаты примитивов: XY → (X, Y), "
    "XZ → (X, Z), YZ → (Y, Z); нормали +Z, +Y, +X. Первая операция — основное тело, дальше бобышки "
    "и вырезы. expected_volume_mm3 — твой расчёт объёма, сервер сверит. Возвращает итог по каждой "
    "операции (warning = операция ничего не изменила), объём, массу и снимок. " + SHAPES_DOC))
async def build_part(operations: list[dict[str, Any]], name: str | None = None, new: bool = True,
                     expected_volume_mm3: float | None = None, screenshot: bool = True,
                     keep_going: bool = False) -> list:
    def go():
        res = _k.build_part(operations, name, new, expected_volume_mm3, keep_going)
        shot = _k.screenshot(_out("build_part.png"), True) if screenshot else None
        return res, shot
    res, shot = await _run(go)
    out: list = [json.dumps(res, ensure_ascii=False, indent=1)]
    if shot:
        with open(shot, "rb") as f:
            out.append(Image(data=f.read(), format="png"))
    return out


@mcp.tool()
async def plate_with_holes(length: float, width: float, thickness: float, hole_diameter: float,
                           edge_offset: float, name: str | None = None) -> dict:
    """Готовый сценарий: прямоугольная пластина с 4 отверстиями по углам (отступ от краёв до центра отверстия)."""
    def go():
        _k.new_part(name)
        r = hole_diameter / 2
        shapes = [{"type": "rect", "x": 0, "y": 0, "w": length, "h": width}]
        for x in (edge_offset, length - edge_offset):
            for y in (edge_offset, width - edge_offset):
                shapes.append({"type": "circle", "x": x, "y": y, "r": r})
        sid = _k.sketch("XY", shapes)
        _checked(_k.extrude, "пластина", sid, thickness)
        return {"created": True, **_k.mass_properties()}
    return await _run(go)


@mcp.tool()
async def model_info() -> dict:
    """Масса/объём/площадь активной детали, её габариты по X/Y/Z (size_mm) и список элементов дерева."""
    def go():
        return {"mass": _k.mass_properties(), **_k.gabarit(), "tree": _k.feature_tree()}
    return await _run(go)


@mcp.tool()
async def new_fragment(sheet: bool = False) -> dict:
    """Создать 2D-документ: фрагмент (по умолчанию) или чертёж (sheet=True)."""
    return await _run(_k.new_fragment, sheet)


@mcp.tool(description="Нарисовать примитивы в активном 2D-документе. " + SHAPES_DOC)
async def draw_2d(shapes: list[Shape]) -> dict:
    return {"drawn": await _run(_k.draw_2d, shapes)}


@mcp.tool()
async def save(path: str) -> dict:
    """Сохранить активный документ (.m3d / .frw / .cdw). Относительный путь — в папку вывода."""
    return {"saved": await _run(_k.save, _out(path))}


@mcp.tool()
async def export(path: str) -> dict:
    """Экспорт активного документа; формат по расширению: step/stp, igs, stl, x_t, dxf, dwg, pdf, png, jpg."""
    return {"exported": await _run(_k.export, _out(path))}


@mcp.tool()
async def set_view(view: Literal["iso", "front", "top", "left", "right", "back", "bottom"]) -> dict:
    """Повернуть 3D-модель в стандартную ориентацию."""
    await _run(_k.set_view, view)
    return {"view": view}


@mcp.tool()
async def convert_drawing(src: str, dst: str) -> dict:
    """Открыть чертёж в КОМПАС (.cdw/.frw/.dwg/.dxf) без показа и сохранить в другом формате (по расширению dst)."""
    return {"converted": await _run(_k.convert, src, dst)}


@mcp.tool()
async def projections() -> list:
    """Виды активной детали по ЕСКД: главный, сверху, слева и изометрия (для сверки с чертежом)."""
    paths = await _run(_k.projections, _out("projections"))
    out: list = [json.dumps(list(paths), ensure_ascii=False)]
    for p in paths.values():
        with open(p, "rb") as f:
            out.append(Image(data=f.read(), format="png"))
    return out


@mcp.tool()
async def screenshot(path: str = "screenshot.png", iso: bool = True) -> Image:
    """Снимок активного документа (для 3D по умолчанию в изометрии). Возвращает картинку."""
    p = await _run(_k.screenshot, _out(path), iso)
    with open(p, "rb") as f:
        return Image(data=f.read(), format=os.path.splitext(p)[1].lstrip(".") or "png")


if __name__ == "__main__":
    mcp.run()
