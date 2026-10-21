"""Веб-интерфейс для kompas-mcp: чат с локальной LLM, которая вызывает инструменты КОМПАС.

Запуск: python ui\\app.py  ->  http://127.0.0.1:8765

LLM подключается по OpenAI-совместимому API (LM Studio, Ollama, llama.cpp server, vLLM и т.п.).
Инструменты берутся у MCP-сервера ..\\server.py, который запускается дочерним процессом по stdio.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import anyio
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image as PILImage

import arrange
import drawing
import dxf
import pdfvec
import revolve
import shaft
import steel
import profile_raster
import vision
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server.py")
SETTINGS_PATH = os.environ.get("KOMPAS_UI_SETTINGS", os.path.join(HERE, "settings.json"))
UPLOADS = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "kompas-mcp", "uploads")
SERVER_LOG = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "kompas-mcp", "server.log")
MAX_IMAGE_SIDE = 1600  # long side of drawing images sent to the model, px

DEFAULT_SETTINGS = {
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "",
    "api_key": "",
    "temperature": 0.2,
    "max_steps": 25,
    "vision": False,
    # model for reading drawings (needs vision and an exact JSON answer); "" = the main model
    "drawing_model": "",
    "system_prompt": (
        "Ты инженер-конструктор и работаешь в КОМПАС-3D через инструменты. Единицы — миллиметры. "
        "Оси: X — длина, Y — ширина, Z — высота. Строй деталь ОДНИМ вызовом build_part со списком "
        "операций и передай expected_volume_mm3 — это намного быстрее пошагового построения. "
        "Эскиз на XY с offset=H лежит на высоте H; на XZ координаты (X, Z), на YZ — (Y, Z); "
        "выдавливание и вырез идут по нормали (+Z, +Y, +X), reverse=true — в обратную сторону. "
        "Вложенные замкнутые контуры в одном эскизе дают отверстия. Профиль сложной формы удобно "
        "задать ломаной на XZ (вид спереди) и выдавить на ширину детали. "
        "Перед вызовом кратко перечисли размеры и посчитай ожидаемый объём, думай коротко. "
        "Если build_part вернул failed, warning или volume_matches=false — исправь план и вызови build_part "
        "снова (new=true), не сообщай об успехе при несовпадении. "
        "По чертежу: сопоставь все виды (главный, сверху, слева) и разрезы, не упрощай форму до коробки; "
        "если в штампе указана масса, сравни её с mass_g (сталь 7,85 г/см³). "
        "Отвечай по-русски, кратко, без формул LaTeX (пиши Ø20, мм³ обычным текстом)."
    ),
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    return s


def save_settings(s: dict) -> None:
    # Only keep values that differ from the defaults, so improved defaults reach existing installs.
    diff = {k: v for k, v in s.items() if DEFAULT_SETTINGS.get(k) != v}
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=2)


# ---------------- MCP ----------------

class McpBridge:
    """Keeps one MCP session to server.py alive for the whole app lifetime."""

    def __init__(self):
        self.session: ClientSession | None = None
        self.tools: list[dict] = []
        self.lock = anyio.Lock()

    @contextlib.asynccontextmanager
    async def run(self):
        # Full environment: the default one drops variables pywin32/user-site packages rely on.
        # Under pythonw.exe (desktop shortcut) start the server with the console python.exe next to it.
        exe = sys.executable
        if os.path.basename(exe).lower() == "pythonw.exe":
            exe = os.path.join(os.path.dirname(exe), "python.exe")
        params = StdioServerParameters(command=exe, args=[SERVER], env=dict(os.environ),
                                       cwd=os.path.dirname(SERVER))
        os.makedirs(os.path.dirname(SERVER_LOG), exist_ok=True)
        async with contextlib.AsyncExitStack() as stack:
            # The server's stderr goes to a log file: there is no console under the desktop shortcut.
            errlog = stack.enter_context(open(SERVER_LOG, "a", encoding="utf-8"))
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
            listed = await session.list_tools()
            self.tools = [{"name": t.name, "description": t.description or "",
                           "input_schema": t.input_schema} for t in listed.tools]
            yield self

    # Used by the app itself (status bar, drawing upload); hiding them keeps the model's prompt shorter.
    SERVICE_TOOLS = {"diagnostics", "convert_drawing", "close_document"}

    def openai_tools(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"][:1000],
                              "parameters": t["input_schema"]}}
                for t in self.tools if t["name"] not in self.SERVICE_TOOLS]

    async def call(self, name: str, args: dict) -> tuple[str, list[dict], bool]:
        """Returns (text for the model, images [{mime, data}], is_error)."""
        async with self.lock:
            res = await self.session.call_tool(name, args)
        texts, images = [], []
        for c in res.content:
            if c.type == "text":
                texts.append(c.text)
            elif c.type == "image":
                images.append({"mime": c.mime_type, "data": c.data})
        if images and not texts:
            texts.append(f"Скриншот получен ({len(images)} шт.), пользователь его видит.")
        return "\n".join(texts), images, bool(res.is_error)


bridge = McpBridge()


# ---------------- LLM ----------------

def _http_json(url: str, payload: dict | None, api_key: str, timeout: float = 600) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM недоступна по адресу {url}: {e.reason}") from e


IDLE_TIMEOUT = 300  # s without a single streamed byte (covers loading a big model) before giving up
stop_requested = False


async def llm_stream(settings: dict, messages: list[dict], tools: list[dict] | None,
                     response_format: dict | None = None, max_reasoning_chars: int | None = None):
    """Streams one chat completion. Yields ("progress", info) while the model works and finally
    ("message", {"content", "reasoning", "tool_calls"}). Reasoning models can think for many
    minutes, so there is no overall time limit, only an idle one, and the user can stop it."""
    payload = {"model": settings["model"], "messages": messages,
               "temperature": settings["temperature"], "stream": True}
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    if response_format:
        payload["response_format"] = response_format
    url = settings["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if settings["api_key"]:
        req.add_header("Authorization", f"Bearer {settings['api_key']}")
    try:
        resp = await anyio.to_thread.run_sync(lambda: urllib.request.urlopen(req, timeout=IDLE_TIMEOUT))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM недоступна по адресу {url}: {e.reason}") from e

    content, reasoning, calls = [], [], {}
    t0 = last = time.time()
    try:
        while True:
            if stop_requested:
                raise RuntimeError("Остановлено пользователем.")
            try:
                raw = await anyio.to_thread.run_sync(resp.readline)
            except TimeoutError as e:
                raise RuntimeError(f"Модель не отвечает больше {IDLE_TIMEOUT} с.") from e
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise RuntimeError(f"LLM: {chunk['error']}")
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                r = delta.get("reasoning") or delta.get("reasoning_content")
                if r:
                    reasoning.append(r)
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                                 "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
            if max_reasoning_chars and not content and sum(map(len, reasoning)) > max_reasoning_chars:
                raise RuntimeError("модель слишком долго рассуждает, не начиная ответ")
            if time.time() - last > 1:
                last = time.time()
                yield "progress", {"seconds": round(last - t0), "reasoning_chars": sum(map(len, reasoning)),
                                   "content_chars": sum(map(len, content)), "tool_calls": len(calls)}
    finally:
        resp.close()
    tool_calls = []
    for i, c in sorted(calls.items()):
        c["id"] = c["id"] or f"call_{int(t0)}_{i}"
        tool_calls.append(c)
    yield "message", {"content": "".join(content), "reasoning": "".join(reasoning), "tool_calls": tool_calls}


# ---------------- conversation ----------------

conversation: list[dict] = []


def _json_default(o):
    """numpy numbers and arrays from the picture analysis, sets — as plain JSON."""
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _ev(kind: str, **kw) -> bytes:
    return (json.dumps({"type": kind, **kw}, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8")


def _upload_path(url: str) -> str:
    m = re.fullmatch(r"/uploads/([0-9a-f]{32})/([\w.-]+\.(?:png|jpe?g))", url)
    if not m:
        raise ValueError(f"Недопустимый путь к вложению: {url}")
    return os.path.join(UPLOADS, m.group(1), m.group(2))


def image_part(url: str) -> dict:
    """Downscale an uploaded page/image and wrap it as an OpenAI image_url content part."""
    with PILImage.open(_upload_path(url)) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return {"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


CALLS_LOG = os.path.join(os.path.dirname(SERVER_LOG), "calls.jsonl")


async def _guarded(gen, body: dict):
    """A streamed request that fails inside ends with a readable error event (and the traceback in
    the journal) instead of a dropped connection, which the window can only show as
    «TypeError: network error»."""
    try:
        async for ev in gen:
            yield ev
    except Exception as e:  # noqa: BLE001
        import traceback
        log_call("stream_error", json.dumps({k: body.get(k) for k in ("image", "id")}, ensure_ascii=False),
                 traceback.format_exc(), True)
        yield _ev("error", text=f"Внутренняя ошибка разбора: {type(e).__name__}: {e}. "
                                f"Подробности записаны в журнал calls.jsonl")


def log_call(name: str, raw_args: str, result: str, is_error: bool) -> None:
    """Keeps every tool call the model made with its exact arguments: the only way to replay
    a model's mistake later (LM Studio truncates arguments in its own log)."""
    line = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": name, "args": raw_args,
                       "error": is_error, "result": result[:1500]}, ensure_ascii=False) + "\n"
    # a log file once written by an elevated process cannot be appended to by the normal one
    # (Windows integrity levels): fall back to a second file instead of losing the record
    for path in (CALLS_LOG, CALLS_LOG.replace(".jsonl", f"-{os.getpid()}.jsonl")):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            continue


def compact_history(conv: list[dict]) -> list[dict]:
    """Only the newest message with pictures keeps them. Nothing else is rewritten: LM Studio and
    llama.cpp reuse the computed prefix of a request, and any change to an earlier message makes
    them re-read everything after it. Tool outputs are already capped when they are stored."""
    last_img = max((i for i, m in enumerate(conv) if isinstance(m.get("content"), list)), default=-1)
    out = []
    for i, m in enumerate(conv):
        if isinstance(m.get("content"), list) and i != last_img:
            text = " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
            m = {**m, "content": text + " [изображения убраны из истории]"}
        out.append(m)
    return out


async def agent_loop(user_text: str, images: list[str] | None = None):
    settings = load_settings()
    if not settings["model"]:
        yield _ev("error", text="Не выбрана модель. Откройте настройки.")
        return
    if images:
        try:
            parts = [image_part(u) for u in images]
        except Exception as e:  # noqa: BLE001
            yield _ev("error", text=f"Не удалось прочитать вложение: {e}")
            return
        note = (f"\n\n[Приложено изображений чертежа: {len(parts)}. Прочитай по ним размеры и форму "
                "детали, перечисли их, затем строй.]")
        conversation.append({"role": "user", "content": [{"type": "text", "text": user_text + note}, *parts]})
    else:
        conversation.append({"role": "user", "content": user_text})
    global stop_requested
    stop_requested = False
    tools = bridge.openai_tools()
    for step in range(int(settings["max_steps"])):
        msgs = [{"role": "system", "content": settings["system_prompt"]}] + compact_history(conversation)
        yield _ev("thinking", step=step + 1)
        t0 = time.time()
        msg = None
        try:
            async for kind, data in llm_stream(settings, msgs, tools):
                if kind == "progress":
                    yield _ev("progress", step=step + 1, **data)
                else:
                    msg = data
        except Exception as e:  # noqa: BLE001 - shown to the user as is
            text = str(e)
            if re.search(r"context (size|length|window)|too many tokens|exceeds? the context", text, re.I):
                text = ("Диалог не поместился в контекст модели. Нажмите «Новый», чтобы начать заново, или "
                        "увеличьте Context Length при загрузке модели в LM Studio (рекомендуется 16384 и больше).\n\n"
                        + text[:300])
            yield _ev("error", text=text)
            return
        content = msg["content"]
        calls = msg["tool_calls"]
        conversation.append({"role": "assistant", "content": content,
                             **({"tool_calls": calls} if calls else {})})
        if msg["reasoning"]:
            yield _ev("reasoning", text=msg["reasoning"][-6000:], seconds=round(time.time() - t0, 1))
        if content:
            yield _ev("assistant", text=content, seconds=round(time.time() - t0, 1))
        if not calls:
            yield _ev("done")
            return
        vision_parts = []
        for call in calls:
            fn = call["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            yield _ev("tool_call", id=call["id"], name=fn["name"], args=args)
            try:
                text, images, is_error = await bridge.call(fn["name"], args)
            except Exception as e:  # noqa: BLE001
                text, images, is_error = f"Ошибка вызова: {e}", [], True
            yield _ev("tool_result", id=call["id"], name=fn["name"], text=text[:4000],
                      error=is_error, images=images)
            conversation.append({"role": "tool", "tool_call_id": call["id"], "content": text[:3000]})
            log_call(fn["name"], fn.get("arguments") or "{}", text, is_error)
            if settings["vision"]:
                vision_parts += [{"type": "image_url",
                                  "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"}}
                                 for im in images]
        if vision_parts:
            conversation.append({"role": "user", "content": [
                {"type": "text", "text": "Скриншот модели после последней операции:"}, *vision_parts]})
    yield _ev("error", text=f"Достигнут лимит шагов ({settings['max_steps']}).")


# ---------------- HTTP ----------------

async def index(_: Request):
    # never from the browser's cache: after an update the window must get the new page
    return FileResponse(os.path.join(HERE, "static", "index.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


async def get_settings(_: Request):
    return JSONResponse(load_settings())


async def get_defaults(_: Request):
    return JSONResponse(DEFAULT_SETTINGS)


async def put_settings(request: Request):
    s = load_settings()
    body = await request.json()
    s.update({k: v for k, v in body.items() if k in DEFAULT_SETTINGS})
    save_settings(s)
    return JSONResponse(s)


async def models(request: Request):
    s = load_settings()
    base = request.query_params.get("base_url") or s["base_url"]
    key = request.query_params.get("api_key", s["api_key"])
    try:
        data = await anyio.to_thread.run_sync(_http_json, base.rstrip("/") + "/models", None, key, 10)
        return JSONResponse({"models": sorted(m["id"] for m in data.get("data", []))})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"models": [], "error": str(e)})


async def tools(_: Request):
    return JSONResponse({"tools": [{"name": t["name"], "description": t["description"]} for t in bridge.tools]})


async def status(_: Request):
    text, _, err = await bridge.call("diagnostics", {})
    return JSONResponse({"error": err, "status": json.loads(text) if not err else text})


async def call_tool(request: Request):
    body = await request.json()
    text, images, err = await bridge.call(body["name"], body.get("args", {}))
    return JSONResponse({"text": text, "images": images, "error": err})


async def chat(request: Request):
    body = await request.json()
    return StreamingResponse(agent_loop(body["text"], body.get("images")), media_type="application/x-ndjson")


def _render_pdf(pdf: str, out_dir: str, width: int = 2400) -> None:
    script = os.path.join(HERE, "pdf2png.ps1")
    os.makedirs(out_dir, exist_ok=True)
    for _attempt in range(2):  # a busy machine sometimes fails to start PowerShell: once more
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                            "-Pdf", pdf, "-OutDir", out_dir, "-Width", str(width)],
                           capture_output=True, text=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            break
    if r.returncode != 0:
        raise RuntimeError(f"PDF не открылся: {(r.stderr or r.stdout).strip()[:500]}")


VECTOR_EXT = (".dxf", ".cdw", ".frw", ".dwg")


def _prepare_vector(dxf_path: str, folder: str) -> None:
    """Renders a vector drawing at an exact scale and stores what was read from the file itself."""
    doc = dxf.read_dxf(dxf_path)
    st = steel.analyze(doc)  # a steel detail (rolled profile / plate named in the specification)?
    if st:
        files, _ = steel.render_views(doc, st, folder)
        meta = {"steel": st, "steel_views": files, "numbers": dxf.numbers(doc)}
        with open(os.path.join(folder, "vector.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        return
    r = dxf.render(doc, os.path.join(folder, "source_vector.png"))
    meta = {"px_per_mm": r["px_per_mm"], "numbers": dxf.numbers(doc),
            "hidden_lines": sum(e["kind"] == "hidden" for e in doc["entities"]),
            # where the sheet picture lies in the drawing's mm (for marking views on it)
            "sheet": {"px_per_mm": r["px_per_mm"], "origin": list(r["origin"])}}
    # the views straight from the geometry (no frame, no title block), each drawn on its own
    boxes = dxf.view_boxes(doc)
    if boxes:
        ppm, top = 8.0, max(b[3] for b in boxes)
        views = []
        for k, b in enumerate(boxes, 1):
            name = f"vview{k}.png"
            dxf.render_box(doc, b, os.path.join(folder, name), ppm)
            views.append({"file": name, "box": [round(b[0] * ppm), round((top - b[3]) * ppm),
                                                round(b[2] * ppm), round((top - b[1]) * ppm)],
                          "box_mm": list(b), "outline": dxf.outer_loop(doc, b), "section": dxf.hatched(doc, b),
                          "label": dxf.view_label(doc, b)})
        meta.update({"px_per_mm": ppm, "views": views})
        # a turned part: a longitudinal section with its axis gives the body of revolution exactly
        for k, v in enumerate(views):
            if v["section"]:
                prof = shaft.detect(doc, tuple(v["box_mm"]))
                if prof:
                    meta["shaft"] = {"view": k, **prof}
                    break
    with open(os.path.join(folder, "vector.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)


def _refresh_vector(folder: str) -> None:
    """A drawing uploaded before the reading code changed is read again from its file: its
    vector.json holds what the old code made of it («Колесо» once taken for a steel plate 0.1)."""
    vj = os.path.join(folder, "vector.json")
    if not os.path.exists(vj):
        return
    code = max(os.path.getmtime(os.path.join(HERE, f)) for f in ("app.py", "pdfvec.py", "dxf.py", "steel.py",
                                                                    "shaft.py", "arrange.py") if os.path.exists(os.path.join(HERE, f)))
    if os.path.getmtime(vj) >= code:
        return
    try:
        pdf, dx = os.path.join(folder, "source.pdf"), os.path.join(folder, "source.dxf")
        if os.path.exists(pdf):
            _vector_pdf(pdf, folder)
        elif os.path.exists(dx):
            _prepare_vector(dx, folder)
    except Exception as e:  # noqa: BLE001 — the old reading stays
        print("refresh vector:", e, flush=True)


def _vector_pdf(pdf: str, folder: str) -> bool:
    """A one-page PDF with the drawing as vector lines and real text (not a scan): written out as
    source.dxf and prepared like a DXF upload. False when the PDF is a picture or can't be read so."""
    try:
        p = pdfvec.PDF(pdf)
        if len(p.pages()) != 1:
            return False
        info = pdfvec.to_dxf(pdf, os.path.join(folder, "source.dxf"))
        if not info or info["texts"] < 5:
            return False
        _prepare_vector(os.path.join(folder, "source.dxf"), folder)
        with open(os.path.join(folder, "vector.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        if not meta.get("views") and not meta.get("steel"):
            return False
        bev = pdfvec.bevel_table(pdf)
        if bev:
            # a bevel gear's table (ГОСТ 2.405): its teeth are cut by it
            meta["bevel"] = bev
            with open(os.path.join(folder, "vector.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
            info["bevel"] = bev
        gear = None if bev else pdfvec.gear_table(pdf)
        if gear:
            # a gear wheel's table (module, number of teeth, helix angle…): the teeth are built from it
            meta["gear"] = gear
            with open(os.path.join(folder, "vector.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
            info["gear"] = gear
        print("vector PDF:", json.dumps(info, ensure_ascii=False), flush=True)
        return True
    except Exception:  # noqa: BLE001 — any trouble: the page is rendered and read as a picture
        import traceback
        print("vector PDF failed, read as a picture:", traceback.format_exc(), flush=True)
        for name in ("source.dxf", "vector.json", "source_vector.png"):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
        return False


def _vector_meta(image_path: str) -> dict | None:
    p = os.path.join(os.path.dirname(image_path), "vector.json")
    if os.path.basename(image_path).startswith("source_vector") and os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return None


async def upload(request: Request):
    form = await request.form()
    f = form["file"]
    name = os.path.basename(f.filename or "file")
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg") + VECTOR_EXT:
        return JSONResponse({"error": "Поддерживаются PDF, PNG, JPG, DXF и чертежи КОМПАС (CDW, FRW) или DWG"},
                            status_code=400)
    uid = uuid.uuid4().hex
    folder = os.path.join(UPLOADS, uid)
    os.makedirs(folder, exist_ok=True)
    src = os.path.join(folder, "source" + ext)
    with open(src, "wb") as out:
        out.write(await f.read())
    try:
        if ext in VECTOR_EXT:
            if ext != ".dxf":  # KOMPAS itself turns its own drawings (and DWG) into DXF
                dxf_path = os.path.join(folder, "source.dxf")
                text, _, err = await bridge.call("convert_drawing", {"src": src, "dst": dxf_path})
                if err:
                    raise RuntimeError(text)
            await anyio.to_thread.run_sync(_prepare_vector, os.path.join(folder, "source.dxf"), folder)
            pages = ["source_vector.png"]
        elif ext == ".pdf" and await anyio.to_thread.run_sync(_vector_pdf, src, folder):
            # a PDF printed from a CAD system: its lines and numbers are read as they are
            pages = ["source_vector.png"]
        elif ext == ".pdf":
            await anyio.to_thread.run_sync(_render_pdf, src, folder)
            pages = sorted((p for p in os.listdir(folder) if re.fullmatch(r"page\d+\.png", p)),
                           key=lambda p: int(re.sub(r"\D", "", p)))
        else:
            pages = ["source" + ext]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"id": uid, "name": name, "pages": [f"/uploads/{uid}/{p}" for p in pages]})


async def uploaded_file(request: Request):
    try:
        path = _upload_path(request.url.path)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


# ---------------- drawing analysis ----------------

analyses: dict[str, dict] = {}


def _drawing_settings() -> dict:
    """Settings for reading drawings: a separate (fast, vision) model can be chosen for it."""
    s = load_settings()
    if s.get("drawing_model"):
        s = {**s, "model": s["drawing_model"], "temperature": 0.1}
    return s


def _scaled_part(path: str, target: int = 1280) -> dict:
    """Image part for the reading request: small crops are enlarged so digits stay legible."""
    with PILImage.open(path) as im:
        im = im.convert("RGB")
        k = target / max(im.size)
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), PILImage.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
    return {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}


def _analysis_view(a: dict) -> dict:
    g = a.get("geometry", {})
    try:
        issues = drawing.check(drawing.without_excluded(a["reading"]), a["values"])
    except Exception as e:  # noqa: BLE001 — a model's odd answer must not break the review window
        issues = [{"level": "warning", "dims": [], "text": f"Проверка размеров не выполнена: {e}"}]
    if g.get("error"):
        issues.insert(0, {"level": "error", "dims": [], "text": g["error"]})
    issues = g.get("issues", []) + issues
    cc = a.get("crosscheck")
    if cc:
        issues = issues + cc.get("issues", [])
    try:
        feats = _op_features(drawing.resolve_plan((a["reading"].get("plan") or {}).get("operations", []), a["values"]))
    except Exception:  # noqa: BLE001 — a plan with unresolved expressions shows no feature table
        feats = []
    return {"id": a["id"], "views": a["views"], "reading": a["reading"], "values": a["values"],
            "issues": issues, "notes": g.get("notes", []), "questions": g.get("questions", []),
            "answers": a.get("answers", {}), "construction": g.get("construction"),
            "recognised": _part_kind(a), "mass_stamp": a["reading"].get("mass_kg"), "sheet": a.get("sheet"),
            "overlay": _overlay(a), "features": feats, "feature_edits": a.get("feature_edits", {}),
            "cores": _view_cores(a),
            **({"crosscheck": {"model": cc.get("model"), "features": cc.get("features", []), "error": cc.get("error")}}
               if cc else {})}


def _part_kind(a: dict) -> str:
    """How the drawing was recognised, in the user's words."""
    ops = (a["reading"].get("plan") or {}).get("operations") or []
    if a.get("steel"):
        return "Металлоконструкция: профиль из спецификации, вырезы и отверстия с видов"
    if a.get("profile"):
        return f"Деталь из проката: {a['reading'].get('title', '')}, длина и элементы по размерам на видах"
    if a.get("revolve"):
        return "Тело вращения по продольному разрезу"
    vec = a.get("vector") or {}
    if vec.get("shaft"):
        return "Вал по продольному разрезу (векторный чертёж)"
    if vec and ops and ops[0].get("op") == "revolve":
        return "Тело вращения" + (" с зубьями" if any(o.get("op") == "circular" for o in ops) else "") + \
            " (векторный чертёж)"
    if vec:
        return "Деталь по видам векторного чертежа (размеры из файла)"
    return "Деталь по видам картинки (размеры прочитаны моделью ИИ)"


def _view_cores(a: dict) -> list:
    """Each view's drawn part inside its picture (fractions of the picture), when known."""
    out = []
    for box, core in zip(a.get("boxes") or [], a.get("cores") or []):
        if not box or not core:
            out.append(None)
            continue
        w, h = box[2] - box[0], box[3] - box[1]
        out.append([(core[0] - box[0]) / w, (core[1] - box[1]) / h, (core[2] - box[0]) / w, (core[3] - box[1]) / h])
    return out


def _overlay(a: dict) -> dict | None:
    """What was found where on the sheet, as fractions of the sheet picture: views, and for the
    parts read off the picture by the program itself also the dimensions, holes, slots and ends
    (`op` = the operation it became)."""
    if not a.get("sheet"):
        return None
    size = a.get("sheet_size")
    if not size:
        try:
            with PILImage.open(_upload_path(a["sheet"])) as im:
                size = list(im.size)
        except Exception:  # noqa: BLE001
            return None
        a["sheet_size"] = size
    W, H = size
    names = {v.get("image"): v.get("name") for v in a["reading"].get("views", [])}
    items = []
    if a.get("marks"):
        for m in a["marks"]:
            if m["kind"] == "view":
                continue
            items.append({**{k: v for k, v in m.items() if k not in ("box", "text")},
                          "box": [m["box"][0] / W, m["box"][1] / H, m["box"][2] / W, m["box"][3] / H]})
    boxes = a.get("boxes") or []
    sh = (a.get("vector") or {}).get("sheet")
    vviews = (a.get("vector") or {}).get("views") or []
    if sh and vviews and len(vviews) == len(boxes):
        # vector views: their boxes in mm, placed on the sheet picture
        k, (ox, oy) = sh["px_per_mm"], sh["origin"]
        boxes = [[(v["box_mm"][0] - ox) * k, (oy - v["box_mm"][3]) * k, (v["box_mm"][2] - ox) * k,
                  (oy - v["box_mm"][1]) * k] if v.get("box_mm") else None for v in vviews]
    elif a.get("vector"):
        boxes = []   # (steel views and older files: not placed on the sheet)
    for i, b in enumerate(boxes):
        if b:
            items.insert(0, {"kind": "view", "label": f"{i + 1}: {names.get(i + 1) or 'вид'}", "image": i + 1,
                             "box": [b[0] / W, b[1] / H, b[2] / W, b[3] / H]})
    return {"image": a["sheet"], "size": size, "items": items} if items else None


def _op_features(ops: list[dict]) -> list[dict]:
    """The plan's operations as rows the user can switch off or correct: a hole's centre and Ø,
    a rectangular slot's centre and sizes (in the operation's sketch plane)."""
    out = []
    for n, op in enumerate(ops):
        shapes = op.get("shapes") or []
        f = {"id": f"op{n}", "n": n + 1, "op": op.get("op"), "plane": op.get("plane"),
             "label": op.get("comment") or op.get("op", ""), "base": n == 0, "params": {}}
        num = lambda v: isinstance(v, (int, float))  # noqa: E731
        if shapes and all(s.get("type") == "circle" for s in shapes):
            d0 = shapes[0].get("d") if num(shapes[0].get("d")) else (2 * shapes[0]["r"] if num(shapes[0].get("r")) else None)
            if len(shapes) == 1 and num(shapes[0].get("x")) and num(shapes[0].get("y")) and d0:
                f["params"] = {"x": round(shapes[0]["x"], 3), "y": round(shapes[0]["y"], 3), "d": round(d0, 3)}
            elif d0:
                f["params"] = {"d": round(d0, 3)}
            f["shape"] = "circle"
        elif len(shapes) == 1 and shapes[0].get("type") == "polyline":
            pts = shapes[0].get("points") or []
            if len(pts) == 4 and all(len(p) == 2 and num(p[0]) and num(p[1]) for p in pts):
                xs, ys = sorted({round(p[0], 6) for p in pts}), sorted({round(p[1], 6) for p in pts})
                if len(xs) == 2 and len(ys) == 2:
                    f["params"] = {"x": round((xs[0] + xs[1]) / 2, 3), "y": round((ys[0] + ys[1]) / 2, 3),
                                   "w": round(xs[1] - xs[0], 3), "h": round(ys[1] - ys[0], 3)}
                    f["shape"] = "rect"
        out.append(f)
    return out


def _apply_feature_edits(ops: list[dict], edits: dict) -> list[dict]:
    """The user's corrections to the feature table applied to the resolved operations: switched
    off rows are dropped, a hole's centre / Ø and a slot's centre / sizes replaced."""
    if not edits:
        return ops
    feats = {f["id"]: f for f in _op_features(ops)}
    out = []
    for n, op in enumerate(ops):
        e, f = edits.get(f"op{n}") or {}, feats.get(f"op{n}")
        if e.get("on") is False and n > 0:
            continue
        p = {k: float(v) for k, v in (e.get("params") or {}).items()
             if k in (f or {}).get("params", {}) and isinstance(v, (int, float, str)) and str(v).strip() != ""}
        if p and f:
            op = json.loads(json.dumps(op))
            cur = {**f["params"], **p}
            if f.get("shape") == "circle":
                for s in op["shapes"]:
                    s.pop("r", None)
                    s["d"] = cur["d"]
                    if "x" in cur:
                        s["x"], s["y"] = cur["x"], cur["y"]
            elif f.get("shape") == "rect":
                x, y, w, h = cur["x"], cur["y"], cur["w"], cur["h"]
                op["shapes"][0]["points"] = [[x - w / 2, y - h / 2], [x + w / 2, y - h / 2],
                                             [x + w / 2, y + h / 2], [x - w / 2, y + h / 2]]
            op["comment"] = (op.get("comment") or op.get("op", "")) + " (исправлено)"
        out.append(op)
    return out


def _vector_layout(vec: dict, skip: set | None = None) -> dict | None:
    """Main, top and left views of a vector drawing from where the views lie on the sheet: the
    biggest view is the main one, the top view lies under it and the left view to its right (ESKD).
    Small detail views ("Б (2:1)") elsewhere on the sheet are left out, as are views the user
    excluded (skip: 0-based indices)."""
    vs = vec.get("views") or []
    skip = skip or set()
    if not vs or not all(v.get("box_mm") for v in vs):
        return None
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1]) if b else -1  # noqa: E731
    boxes = [None if i in skip else v["box_mm"] for i, v in enumerate(vs)]
    if all(b is None for b in boxes):
        return None
    def overlap(a0, a1, b0, b1):
        return max(0.0, min(a1, b1) - max(a0, b0)) / max(1e-9, min(a1 - a0, b1 - b0))

    def lined_up(i):
        """How many views stand in projection with view i: the same length above/below it or the
        same height beside it."""
        b, n = boxes[i], 0
        for j, c in enumerate(boxes):
            if j == i or not c:
                continue
            same_x = abs((c[2] - c[0]) - (b[2] - b[0])) <= 0.02 * (b[2] - b[0]) and abs(c[0] - b[0]) <= 1.0
            same_y = abs((c[3] - c[1]) - (b[3] - b[1])) <= 0.02 * (b[3] - b[1]) and abs(c[1] - b[1]) <= 1.0
            n += same_x or same_y
        return n
    # the main view: the biggest of the views the others are lined up with; a view labelled as a
    # view of its own («В (1 : 1)», «А-А») is placed freely and is no main view
    biggest = max(area(b) for b in boxes if b)
    # (a view standing in projection with others counts even when a pictorial view is bigger — the
    # gear's isometric pictures are twice its main view)
    cands = [i for i, b in enumerate(boxes) if b and area(b) >= 0.6 * biggest]
    if not any(lined_up(i) for i in cands):
        cands += [i for i, b in enumerate(boxes) if b and i not in cands and lined_up(i)]
    main = max(cands, key=lambda i: (lined_up(i), not vs[i].get("label"), area(boxes[i])))
    m = boxes[main]
    # a plain view in projection lines up with the main one; a section may sit anywhere near (and
    # one drawn at its own scale is brought to size around its middle)
    def ok(i, a0, a1, b0, b1):
        # …and is never longer than the main view along the edge they share: a picture much wider
        # (an isometric view of a gear, its shading taken for hatching) is no projection
        return overlap(a0, a1, b0, b1) > 0.6 and (a1 - a0) <= 1.1 * (b1 - b0) + 1.0 and \
            (vs[i].get("section") or abs(a0 - b0) <= 1.5)
    below = [i for i, b in enumerate(boxes) if b and i != main and b[3] <= m[1] + 0.5 and ok(i, b[0], b[2], m[0], m[2])]
    right = [i for i, b in enumerate(boxes) if b and i != main and b[0] >= m[2] - 0.5 and ok(i, b[1], b[3], m[1], m[3])]
    top = min(below, key=lambda i: m[1] - boxes[i][3], default=None)
    # a section (hatched) to the right shows the depths inside; a plain view only the outside
    full = lambda i: vs[i].get("section") and boxes[i][3] - boxes[i][1] >= 0.9 * (m[3] - m[1])  # noqa: E731
    side = min(right, key=lambda i: (not full(i), boxes[i][0] - m[2]), default=None)
    if top is None and side is None and not skip:
        return None
    # with views excluded by the user, the main view alone still makes a part (its outline
    # through the width from the table)
    # the part's true outline seen from above or below: a plain view lined up over or under the main
    # one (a section only shows the part where it is cut). Over the main view it is the view from
    # below, its front face at its top edge — flipped to read like a view from above
    plain = [(i, b[1] >= m[3] - 0.5) for i, b in enumerate(boxes) if b and i != main and not vs[i].get("section")
             and (b[1] >= m[3] - 0.5 or b[3] <= m[1] + 0.5) and abs(b[0] - m[0]) <= 1.5 and abs(b[2] - m[2]) <= 1.5]
    silhouette = min(plain, key=lambda p: min(abs(boxes[p[0]][1] - m[3]), abs(m[1] - boxes[p[0]][3])), default=None)
    return {"front": main, "top": top, "side": side, "silhouette": silhouette}


def _vector_reading(vec: dict, view_paths: list[str], boxes: list, skip: set | None = None) -> dict:
    """The dimension table for a vector drawing: every number written in the file, and the overall
    sizes measured on the views themselves (exact, since the scale is known)."""
    n = vec["numbers"]
    dims, k = [], 0

    def view_size(i):
        """The view's size in mm from its picture's silhouette; where that matches no number on the
        drawing but the view's box from the file's main lines does, the box (a leader's arrowhead
        reaching past the outline made «45» read as 46.5)."""
        L, H = dxf.view_size_mm(view_paths[i], vec["px_per_mm"], n["values"])
        b = (vec["views"][i] if i < len(vec.get("views") or []) else {}).get("box_mm")
        if not b:
            return L, H
        out = []
        for v, bv in ((L, b[2] - b[0]), (H, b[3] - b[1])):
            near = min(n["values"], key=lambda c: abs(c - bv), default=None)
            out.append(float(near) if v not in n["values"] and near is not None and abs(near - bv) <= 0.3 else v)
        return out[0], out[1]

    def add(value, meaning, axis, overall=False):
        nonlocal k
        k += 1
        dims.append({"id": f"d{k}", "value": value, "view": "из файла", "meaning": meaning, "axis": axis,
                     "overall": overall, "confidence": 1.0})
    reading = {"views": [], "dimensions": dims, "overall": {}, "features": []}
    lay = _vector_layout(vec, skip)
    pair = (lay["front"], lay["top"]) if lay and lay["top"] is not None else \
        None if lay else drawing.pick_views(reading, len(view_paths), boxes)
    sizes = {}
    sh = vec.get("shaft")
    if sh and sh["view"] not in (skip or set()):
        # a turned part: its length and biggest diameter straight from the section
        snap = lambda v, c: min(c, key=lambda q: abs(q - v), default=v) if c and \
            abs(min(c, key=lambda q: abs(q - v)) - v) <= 0.3 else round(v, 2)  # noqa: E731
        L = snap(sh["length"], n["values"])
        D = snap(2 * max(sh["r_out"]), n["diameters"] + n["values"])
        sizes = {"length": L, "width": D, "height": D}
        pv = shaft.plain_view(None, vec["views"], sh["view"], sh["length"])
        reading["views"] = [{"image": sh["view"] + 1, "name": "другое"}] + \
            ([{"image": pv + 1, "name": "главный"}] if pv is not None else [])
    elif lay and lay["top"] is None and lay["side"] is None:
        # only the main view is left (the user excluded the others): the width has to be typed in
        L, H = view_size(lay["front"])
        sizes = {"length": L, "height": H}
        reading["views"] = [{"image": lay["front"] + 1, "name": "главный"}]
    elif lay and lay["top"] is None:
        # main view + left view (no top view): the width is the left view's width
        L, H = view_size(lay["front"])
        W, _ = view_size(lay["side"])
        sizes = {"length": L, "width": W, "height": H}
        reading["views"] = [{"image": lay["front"] + 1, "name": "главный"}, {"image": lay["side"] + 1, "name": "слева"}]
    elif pair:
        L, H = view_size(pair[0])
        _, W = view_size(pair[1])
        sizes = {"length": L, "width": W, "height": H}
        reading["views"] = [{"image": pair[0] + 1, "name": "главный"}, {"image": pair[1] + 1, "name": "сверху"}]
        if lay and lay["side"] is not None:
            reading["views"].append({"image": lay["side"] + 1, "name": "слева"})
    labels = {"length": "длина детали", "width": "ширина детали", "height": "высота детали"}
    for axis, v in sizes.items():
        add(v, labels[axis] + (" (габарит, по виду)" if v not in n["values"] else " (габарит)"), axis, True)
    used = set(sizes.values())
    for v in n["values"]:
        if v not in used:
            add(v, "размер из файла", "other")
    for v in n["diameters"]:
        add(v, f"диаметр Ø{v:g}", "diameter")
    for v in n["radii"]:
        add(v, f"радиус R{v:g}", "radius")
    return reading


def _vector_numbers(a: dict) -> dict | None:
    dims = drawing.without_excluded(a["reading"])["dimensions"]
    values = [float(a["values"].get(d["id"], d["value"])) for d in dims
              if d.get("axis") not in ("diameter", "radius")]
    out = {"x": values, "y": values, "z": values, "diameters": [], "radii": []}
    for d in dims:
        v = float(a["values"].get(d["id"], d["value"]))
        if d.get("axis") == "diameter":
            out["diameters"].append(v)
        elif d.get("axis") == "radius":
            out["radii"].append(v)
        elif d.get("overall") and d.get("axis") in ("length", "width", "height"):
            out[d["axis"]] = v
    return out if all(k in out for k in ("length", "width", "height")) else None


def _centroid(pts) -> tuple:
    """Centre of area of a closed polygon (its first point for a degenerate one)."""
    a = cx = cy = 0.0
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i - 1], pts[i]
        k = x1 * y2 - x2 * y1
        a += k
        cx += (x1 + x2) * k
        cy += (y1 + y2) * k
    if abs(a) < 1e-12:
        return tuple(pts[0])
    return cx / (3 * a), cy / (3 * a)


def _turned(shapes, t, cx, cz):
    """Sketch shapes turned by t (rad) round (cx, cz)."""
    c, sn = math.cos(t), math.sin(t)
    rot = lambda x, y: (cx + (x - cx) * c - (y - cz) * sn, cz + (x - cx) * sn + (y - cz) * c)  # noqa: E731
    out = []
    for sh in shapes:
        q = dict(sh)
        if sh["type"] == "line":
            (q["x1"], q["y1"]), (q["x2"], q["y2"]) = rot(sh["x1"], sh["y1"]), rot(sh["x2"], sh["y2"])
        elif sh["type"] == "arc":
            q["x"], q["y"] = rot(sh["x"], sh["y"])
            q["a1"], q["a2"] = sh["a1"] + math.degrees(t), sh["a2"] + math.degrees(t)
            if "x1" in sh:
                (q["x1"], q["y1"]), (q["x2"], q["y2"]) = rot(sh["x1"], sh["y1"]), rot(sh["x2"], sh["y2"])
        elif sh["type"] in ("circle", "text"):
            q["x"], q["y"] = rot(sh["x"], sh["y"])
        elif sh["type"] in ("polyline", "spline"):
            q["points"] = [list(rot(*p)) for p in sh["points"]]
        out.append(q)
    return out


def _gear_hubs(ops: list) -> None:
    """A hub in front of the teeth or a shoulder behind them, read as a cut round a circle across
    the face («вокруг ступицы Ø7 на глубину 2.5», «буртик Ø9 сзади»), is built the other way round
    for a gear: the teeth go only between them, the hub and the shoulder are bosses of their own.
    A flat cut across the twisted teeth all but touching their roots left KOMPAS's body broken."""
    loft = next((op for op in ops if op.get("_gear")), None)
    if loft is None:
        return
    g = loft.pop("_gear")
    y0, y1 = 0.0, g["depth"]
    bosses = []
    for op in list(ops):
        sh = op.get("shapes") or []
        if op.get("op") != "cut" or op.get("plane") != "XZ" or len(sh) != 2 or                 [s.get("type") for s in sh] != ["rect", "circle"]:
            continue
        c = sh[1]
        if math.hypot(c["x"] - g["cx"], c["y"] - g["cz"]) > 0.05:
            continue
        a, b = float(op.get("offset", 0)), float(op.get("offset", 0)) + float(op.get("depth", 0))
        if a <= 0.01 and b < g["depth"]:          # in front: the hub from the face to b
            y0 = max(y0, b)
            bosses.append((0.0, b, c))
        elif b >= g["depth"] - 0.01 and a > 0:    # behind: the shoulder from a to the back
            y1 = min(y1, a)
            bosses.append((a, g["depth"], c))
        else:
            continue
        ops.remove(op)
    if y1 - y0 < 0.1:
        return
    at = ops.index(loft)
    if g.get("p"):
        # the root cylinder, one tooth through sections along the helix, copied round the gear
        import gear as gearmod
        p, n = g["p"], len(loft["sections"])
        ys = [y0 + (y1 - y0) * k / (n - 1) for k in range(n)]
        r_in = p["rf"] - 0.2 * p["m"]
        ops[at:at + 1] = [
            {"op": "extrude", "plane": "XZ", "offset": y0, "depth": y1 - y0,
             "shapes": [{"type": "circle", "x": g["cx"], "y": g["cz"], "d": 2 * p["rf"]}],
             "comment": f"зубчатый венец: цилиндр впадин Ø{2 * p['rf']:.2f} от {y0:g} до {y1:g}"},
            {"op": "loft", "plane": "XZ",
             "sections": [{"offset": y, "shapes": gearmod.tooth_shapes(p, g["phase"] + g["omega"] * y,
                                                                      g["cx"], g["cz"], r_in)} for y in ys],
             "comment": "зуб по сечениям вдоль винтовой линии"},
            {"op": "circular", "plane": "XZ", "x": g["cx"], "y": g["cz"], "count": p["z"],
             "comment": f"зубья: {p['z']} шт. по кругу"}]
        at += 3
    else:
        n = len(loft["sections"])
        loft["sections"] = [{"offset": y0 + (y1 - y0) * k / (n - 1),
                             "shapes": _turned(g["base"], g["omega"] * (y0 + (y1 - y0) * k / (n - 1)), g["cx"], g["cz"])}
                            for k in range(n)]
        loft["comment"] = f"контур главного вида — косые зубья по сечениям (от {y0:g} до {y1:g})"
        at += 1
    for a, b, c in bosses:
        ops.insert(at, {"op": "extrude", "plane": "XZ", "offset": a, "depth": b - a,
                        "shapes": [{"type": "circle", "x": c["x"], "y": c["y"], "d": c["d"]}],
                        "comment": f"{'ступица' if a == 0 else 'буртик'} Ø{c['d']:g} от {a:g} до {b:g}"})
        at += 1


def _helical_gear(ops: list, src_dxf: str, views: list, side_i, numbers: dict, table: dict | None = None) -> list[str]:
    """A gear wheel drawn as on ESKD drawings — the main view its face with every tooth, the view
    beside it with the teeth's lines slanting: the helix is read off the drawing itself. The teeth
    are counted on the main outline, and the slope of the tooth lines on the view beside (near its
    middle, where they face the viewer) gives the twist per millimetre. The main outline then goes
    into an operation through sections, each turned by that twist (instead of a straight extrusion)."""
    main = next((op for op in ops if op.get("comment") == "контур главного вида"), None)
    if main is None or side_i is None or not os.path.exists(src_dxf):
        return []
    sys.path.insert(0, os.path.dirname(HERE))
    import kompas_api
    import numpy as np
    from gear import module_from
    L, W, H = numbers["length"], numbers["width"], numbers["height"]
    flat = kompas_api.arcs_to_lines(main["shapes"], 1.0) or main["shapes"]
    pts = []
    for s in flat:
        if s.get("type") == "line":   # points every 0.05 mm along the outline
            n = max(1, int(math.dist((s["x1"], s["y1"]), (s["x2"], s["y2"])) / 0.05))
            pts += [(s["x1"] + (s["x2"] - s["x1"]) * t / n, s["y1"] + (s["y2"] - s["y1"]) * t / n) for t in range(n)]
    if len(pts) < 200:
        return []
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cz = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    # the outline's radius round the centre, the farthest point in each half degree
    r_of = np.zeros(720)
    for x, y in pts:
        k = int((math.degrees(math.atan2(y - cz, x - cx)) % 360) * 2) % 720
        r_of[k] = max(r_of[k], math.hypot(x - cx, y - cz))
    if (r_of == 0).any():
        r_of[r_of == 0] = np.interp(np.nonzero(r_of == 0)[0], np.nonzero(r_of)[0], r_of[r_of > 0])
    ra, rf = float(r_of.max()), float(r_of.min())
    if ra - rf < 0.04 * ra:
        return []
    # teeth: the strongest repetition round the circle (8…200 times), well above any other
    spec = np.abs(np.fft.rfft(r_of - r_of.mean()))
    z = int(np.argmax(spec[8:201])) + 8
    others = np.delete(spec[3:201], z - 3)
    if spec[z] < 10 * np.median(others):
        return []  # no even teeth all round: no gear (a splined end, a few slots…)
    # every tooth goes out and back in once, and is one to three modules deep as drawn (a
    # rectangle's corners gave a strong 12th harmonic too)
    dev = r_of - (ra + rf) / 2
    crossings = int(np.count_nonzero(np.sign(dev) != np.sign(np.roll(dev, 1))))
    if not 1.5 * z <= crossings <= 2.5 * z or not 0.8 <= (ra - rf) / (2 * ra / (z + 2)) <= 3.5:
        return []
    # the tooth lines on the view beside, near its middle height: slope = ra · twist per mm
    vb = views[side_i].get("box_mm")
    doc = dxf.read_dxf(src_dxf)
    zmid = (vb[1] + vb[3]) / 2
    slopes = []
    for e in doc["entities"]:
        if e["kind"] != "solid" or e.get("from_dimension") or e["type"] not in ("LINE", "LWPOLYLINE"):
            continue
        for (ax, ay), (bx, by) in dxf._segments(e):
            if not all(vb[0] - 0.1 <= x <= vb[2] + 0.1 and abs(y - zmid) <= 0.35 * ra for x, y in ((ax, ay), (bx, by))):
                continue
            dx, dy = bx - ax, by - ay
            if math.hypot(dx, dy) < 0.15 or abs(dx) < 1e-9:
                continue
            ang = math.degrees(math.atan2(abs(dy), abs(dx)))
            if 3 <= ang <= 45:
                slopes.append(dy / dx)
    table = table or {}
    if len(slopes) < 6 and not table.get("beta"):
        return [f"Зубчатое колесо: {z} зубьев, вершины Ø{2 * ra:.2f}; зубья прямые (наклона на виде сбоку нет)"]
    k_y = W / (vb[2] - vb[0])
    k_z = H / (vb[3] - vb[1])
    if slopes:
        slopes.sort()
        s = slopes[len(slopes) // 2]
        omega_drawn = s * k_z / (k_y * ra)        # rad per mm along the depth (+Y), see the docstring
    else:
        omega_drawn = 0.0
    beta_tip = math.degrees(math.atan(abs(omega_drawn) * ra))
    notes = []
    # the involute teeth from the gear's parameters (the table, the model's reading of it, or what
    # the drawing shows): the tip diameter as dimensioned, the number of teeth counted, the helix
    # angle on the pitch cylinder from the one seen at the tips, the module from the standard row
    da = min((v for v in numbers.get("x", []) + numbers.get("z", []) + numbers.get("diameters", [])
              if abs(v - 2 * ra) <= 0.02 * 2 * ra), key=lambda v: abs(v - 2 * ra), default=2 * ra)
    if table.get("z") and int(table["z"]) != z:
        notes.append(f"Число зубьев: по чертежу {z}, в таблице {int(table['z'])} — взято по чертежу")
    x_sh = float(table.get("x") or 0.0)
    alpha = float(table.get("alpha") or 20.0)
    beta = float(table["beta"]) if table.get("beta") else None
    m = float(table["m"]) if table.get("m") else None
    if beta is None:
        # tan β on the pitch cylinder = tan β at the tips · r / ra; r depends on m and β: a few rounds
        beta = beta_tip
        for _ in range(6):
            m_ = module_from(da, z, beta, x_sh) or da / (z / math.cos(math.radians(beta)) + 2)
            r_ = m_ * z / math.cos(math.radians(beta)) / 2
            beta = math.degrees(math.atan(math.tan(math.radians(beta_tip)) * r_ / (da / 2)))
        beta = float(round(beta))
    if m is None:
        m = module_from(da, z, beta, x_sh)
    import gear as gearmod
    p = gearmod.params(m, z, beta, alpha, x_sh, da=da) if m else None
    if p:
        # a root circle a few hundredths off a step the drawing has (a Ø9 shoulder against a Ø9.01
        # root) leaves the cut along that step all but touching the teeth's roots: KOMPAS breaks the
        # body there — the root keeps 0.05 mm clear of it
        for D in numbers.get("diameters", []) + numbers.get("x", []) + numbers.get("z", []):
            if abs(p["rf"] - D / 2) < 0.05:
                p["rf"] = D / 2 + (0.05 if p["rf"] >= D / 2 else -0.05)
    if table.get("hand") in ("right", "left"):
        sign = -1.0 if table["hand"] == "right" else 1.0
    else:
        sign = -1.0 if omega_drawn < 0 else 1.0
    omega = sign * math.tan(math.radians(beta)) / p["r"] if p else omega_drawn
    if p:
        # the teeth placed as drawn: the phase of the repetition round the circle
        F = np.fft.rfft(r_of - r_of.mean())[z]
        phase = -math.atan2(F.imag, F.real) / z
        main["shapes"] = gearmod.outline_shapes(p, phase, cx, cz)
        notes.append(f"Зубья — эвольвента по параметрам: m {m:g}, z {z}, β {beta:g}°, α {alpha:g}°, x {x_sh:g}; "
                     f"делительный Ø{2 * p['r']:.3f}, вершины Ø{2 * p['ra']:.2f}, впадины Ø{2 * p['rf']:.2f}"
                     + ("" if table else " (модуль — из Ø вершин по стандартному ряду, β — по виду сбоку)"))
    # sections every 3° of turn or so: the straight ruling between them stays on the helix within
    # a few microns at the tips
    n = max(3, int(math.ceil(abs(omega) * W / math.radians(3))) + 1)

    depth = float(main.get("depth", W))
    base = main["shapes"]
    secs = [{"offset": depth * k / (n - 1), "shapes": _turned(base, omega * depth * k / (n - 1), cx, cz)}
            for k in range(n)]
    # a gear is round: what the view beside shows at its back — a shoulder (Ø9, 0.2) — is cut as a
    # ring round it, not by the side outline cut through everything along X: that cut runs a few
    # hundredths past the teeth's roots and KOMPAS breaks the body there (the front's hub is cut the
    # same way where the areas are read)
    side_out = (views[side_i].get("outline") or {}).get("points") or []
    if side_out:
        zc = (vb[1] + vb[3]) / 2

        def back_at(rr):   # how far back the material reaches at radius rr (model mm)
            y = zc + rr / k_z
            best = None
            for (x1, y1), (x2, y2) in zip(side_out, side_out[1:] + side_out[:1]):
                if (y1 - y) * (y2 - y) < 0:
                    xx = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                    dd = (vb[2] - xx) * k_y
                    best = dd if best is None else max(best, dd)
            return best
        rings = []
        rr, prev = 0.3, back_at(0.3)
        while rr < ra - 0.1 and prev is not None:
            cur = back_at(rr + 0.05)
            if cur is not None and prev - cur > 0.05:
                rings.append((rr + 0.025, cur))
            prev, rr = cur, rr + 0.05
        for r_s, b in rings:
            d_s = min(numbers.get("diameters", []), key=lambda v: abs(v - 2 * r_s), default=None)
            d_s = d_s if d_s is not None and abs(d_s - 2 * r_s) <= 0.15 else round(2 * r_s, 2)
            b_s = min(numbers.get("y", []) + numbers.get("x", []), key=lambda v: abs(v - b), default=None)
            b_s = b_s if b_s is not None and abs(b_s - b) <= 0.1 else round(b, 2)
            ops.append({"op": "cut", "plane": "XZ", "offset": b_s, "depth": W - b_s + 1,
                        "shapes": [{"type": "rect", "x": -1, "y": -1, "w": L + 2, "h": H + 2},
                                   {"type": "circle", "x": cx, "y": cz, "d": d_s}],
                        "comment": f"буртик Ø{d_s:g} сзади: вокруг него снято от {b_s:g}"})
            notes.append(f"Сзади буртик Ø{d_s:g} (по виду сбоку): вокруг него деталь кончается на {b_s:g} мм")
        side_cut = next((op for op in ops if op.get("comment") == "контур вида слева"), None)
        if side_cut is not None:
            ops.remove(side_cut)
    main.clear()
    main.update({"op": "loft", "plane": "XZ", "sections": secs,
                 "comment": "контур главного вида — косые зубья по сечениям",
                 "_gear": {"base": base, "omega": omega, "cx": cx, "cz": cz, "depth": depth,
                           "p": p, "phase": phase if p else 0.0}})
    # turning against the +Y depth (seen from the front face) is a right-hand helix (the gear's
    # table «правое» with lines sloping down to the right on the view beside)
    hand = "правое" if omega < 0 else "левое"
    src = "по таблице" if table.get("hand") else "по наклону линий зубьев на виде сбоку"
    return [f"Косозубое колесо: {z} зубьев, вершины Ø{2 * ra:.2f}; на виде сбоку наклон на вершинах {beta_tip:.1f}°; "
            f"винтовая линия: поворот {math.degrees(omega):.2f}° на мм, по сечениям ({n} шт.); направление {hand} ({src})"] + notes


def _corner_fillets(outline: dict) -> tuple:
    """A fillet arc between two straight edges of a view's outline (tangent to both, 30…150°) is an
    edge rounded along the view's depth: the outline gets the sharp corner back and the corner is
    returned, to be rounded by a fillet operation once every cut is made. Returns (outline,
    [(corner, radius)]) in the drawing's mm."""
    segs = outline.get("segs") if isinstance(outline, dict) else None
    if not segs:
        return outline, []
    out = [dict(s) for s in segs]
    corners = []
    k = 0
    while k < len(out) and len(out) >= 4:
        s, m = out[k], len(out)
        prev, nxt = out[k - 1], out[(k + 1) % m]
        if s["type"] == "arc" and prev["type"] == "line" and nxt["type"] == "line" and s["r"] <= 15 \
                and math.radians(30) <= arrange._sweep(s) <= math.radians(150):
            L1, L2 = math.dist(prev["a"], prev["b"]), math.dist(nxt["a"], nxt["b"])
            if L1 > 1e-6 and L2 > 1e-6:
                u1 = ((prev["b"][0] - prev["a"][0]) / L1, (prev["b"][1] - prev["a"][1]) / L1)
                u2 = ((nxt["b"][0] - nxt["a"][0]) / L2, (nxt["b"][1] - nxt["a"][1]) / L2)
                sg = 1 if s["ccw"] else -1

                def tan(p):
                    rx, ry = p[0] - s["c"][0], p[1] - s["c"][1]
                    r = math.hypot(rx, ry) or 1e-12
                    return (-ry / r * sg, rx / r * sg)
                # where the lines end on the circle (an arc's last hair can be lost where it met
                # another line within the joining tolerance)
                ta, tb = tan(prev["b"]), tan(nxt["a"])
                cross = u1[0] * u2[1] - u1[1] * u2[0]
                if abs(cross) > 0.1 and u1[0] * ta[0] + u1[1] * ta[1] > math.cos(math.radians(0.5)) \
                        and u2[0] * tb[0] + u2[1] * tb[1] > math.cos(math.radians(0.5)):
                    # the corner: where the two edges meet when carried on
                    t = ((nxt["a"][0] - prev["a"][0]) * u2[1] - (nxt["a"][1] - prev["a"][1]) * u2[0]) / cross
                    corner = (prev["a"][0] + u1[0] * t, prev["a"][1] + u1[1] * t)
                    if math.dist(corner, s["a"]) < 2 * s["r"] and math.dist(corner, s["b"]) < 2 * s["r"]:
                        prev["b"] = corner
                        nxt["a"] = corner
                        del out[k]
                        corners.append((corner, s["r"]))
                        continue
        k += 1
    return dict(outline, segs=out), corners


def _spans(lines: list, at: float, axis: int) -> list:
    """Where a closed outline (lines (x1, y1, x2, y2)) is crossed by the line coordinate[axis] = at:
    the inside stretches along the other coordinate."""
    hits = []
    for x1, y1, x2, y2 in lines:
        a1, a2 = (x1, x2) if axis == 0 else (y1, y2)
        b1, b2 = (y1, y2) if axis == 0 else (x1, x2)
        if (a1 - at) * (a2 - at) < 0 or (a1 == at) != (a2 == at):
            if a1 == a2:
                continue
            t = (at - a1) / (a2 - a1)
            if 0 <= t <= 1:
                hits.append(b1 + t * (b2 - b1))
    hits.sort()
    return [(hits[i], hits[i + 1]) for i in range(0, len(hits) - 1, 2) if hits[i + 1] - hits[i] > 0.3]


def _exact_outlines(ops: list, views: list, pair, side_i, numbers: dict) -> list[str]:
    """Vector drawings: the outlines traced on the pictures follow a 0.5 mm grid, so arcs and slopes
    come out as steps. The file has the exact contours — they replace the traced ones. Fillets
    between straight edges of the outlines that are cut around (the view from above, the left view)
    are rounded edges: those get a sharp corner in the cut and a fillet operation of their own."""
    L, W, H = numbers["length"], numbers["width"], numbers["height"]
    done = []
    rounds = []   # (along "z" or "x", (u, v) of the edge in the view's plane, radius)

    def mapped(v, fn):
        out, b = v.get("outline"), v.get("box_mm")
        if not isinstance(out, dict) or not b:
            return None
        # the part's extreme edges exactly on its overall sizes: one view's outline ending 0.014 mm
        # short of another's leaves a sliver that breaks the body's topology
        def snapped(p):
            x, y = fn(p, b)
            ext = {"контур главного вида": (L, H), "контур вида сверху": (L, W), "контур вида слева": (W, H)}.get(c, (L, H))
            # the main outline is extruded: exactly on the sizes. The other outlines are cut around:
            # their edges on the part's own faces go half a millimetre out, so the cut never has to
            # meet a face exactly (KOMPAS then leaves the body untouched or with broken topology)
            out_ = 0.0 if c == "контур главного вида" else 0.5
            x = next((v + (out_ if v else -out_) for v in (0.0, ext[0]) if abs(x - v) < 0.05), x)
            y = next((v + (out_ if v else -out_) for v in (0.0, ext[1]) if abs(y - v) < 0.05), y)
            return x, y
        return dxf.outline_shapes(out, snapped)
    for op in ops:
        c = op.get("comment", "")
        if c == "контур главного вида":
            shapes = mapped(views[pair[0]], lambda p, b: ((p[0] - b[0]) * L / (b[2] - b[0]), (p[1] - b[1]) * H / (b[3] - b[1])))
            if shapes:
                op["shapes"] = shapes
                done.append("главного вида")
        elif c == "контур вида сверху" and pair[1] is not None:
            v = views[pair[1]]
            o2, corners = _corner_fillets(v.get("outline"))
            fn = lambda p, b: ((p[0] - b[0]) * L / (b[2] - b[0]), (p[1] - b[1]) * W / (b[3] - b[1]))  # noqa: E731
            shapes = mapped(dict(v, outline=o2), fn)
            if shapes and v.get("box_mm"):
                b = v["box_mm"]
                rounds += [("z", fn(p, b), r * L / (b[2] - b[0])) for p, r in corners]
            if shapes:
                # cut through everything around the outline: KOMPAS mishandles small arcs ending on
                # the body's own faces here (the body comes out inside out or untouched) — 2° chords
                sys.path.insert(0, os.path.dirname(HERE))
                import kompas_api
                if (views[pair[1]].get("outline") or {}).get("segs"):
                    # exact outline: only the arcs ending on the part's own faces go as chords
                    on_face = lambda s: any(abs(v - e) < 0.05 for v, e in ((s["x1"], 0), (s["x1"], L), (s["x2"], 0), (s["x2"], L),  # noqa: E731
                                                                            (s["y1"], 0), (s["y1"], W), (s["y2"], 0), (s["y2"], W)))
                    shapes = [t for s in shapes for t in ((kompas_api.arcs_to_lines([s]) or [s])
                                                          if s["type"] == "arc" and on_face(s) else [s])]
                else:
                    shapes = kompas_api.arcs_to_lines(shapes) or shapes
                op["shapes"] = [op["shapes"][0]] + shapes
                done.append("вида сверху")
        elif c == "контур вида слева" and side_i is not None:
            # depth grows to the left in the left view: its right edge is the front face
            v = views[side_i]
            o2, corners = _corner_fillets(v.get("outline"))
            fn = lambda p, b: ((b[2] - p[0]) * W / (b[2] - b[0]), (p[1] - b[1]) * H / (b[3] - b[1]))  # noqa: E731
            shapes = mapped(dict(v, outline=o2), fn)
            if shapes and v.get("box_mm"):
                b = v["box_mm"]
                rounds += [("x", fn(p, b), r * W / (b[2] - b[0])) for p, r in corners]
            if shapes:
                op["shapes"] = [op["shapes"][0]] + shapes
                done.append("вида слева")
    notes = [f"Контуры {', '.join(done)} взяты из файла точно (дуги и наклоны без ступенек)"] if done else []
    main = next((op for op in ops if op.get("comment") == "контур главного вида"), None)
    if rounds and main:
        sys.path.insert(0, os.path.dirname(HERE))
        import kompas_api
        flat = kompas_api.arcs_to_lines(main["shapes"]) or main["shapes"]
        lines = [(s["x1"], s["y1"], s["x2"], s["y2"]) for s in flat if s.get("type") == "line"]
        made = {}
        for along, (u, v), r in rounds:
            R = dxf._snap_r(r) or round(r, 3)
            # the edge runs along the main view's depth axis where the main outline has material:
            # points along it (the first one on an edge of the finished body is taken)
            if along == "z":    # view from above: (X, Y), the edge along Z; main view (X, Z)
                cands = [[u, v, lo + t * (hi - lo)] for lo, hi in _spans(lines, u, 0) for t in (0.5, 0.25, 0.75, 0.1, 0.9)]
            else:               # left view: (Y, Z), the edge along X
                cands = [[lo + t * (hi - lo), u, v] for lo, hi in _spans(lines, v, 1) for t in (0.5, 0.25, 0.75, 0.1, 0.9)]
            if not cands:
                continue
            ops.append({"op": "fillet", "radius": R, "points": [cands],
                        "comment": f"скругление ребра R{R:g} ({'вид сверху' if along == 'z' else 'вид слева'})",
                        # this edge only: carried on along edges tangent to it the rounding ran
                        # round half the outline on the clamp and broke the cuts made after it
                        "edge_round": True, "tangent": False})
            made[R] = made.get(R, 0) + 1
        if made:
            # right after the outline cuts: the pockets and end cuts that come later only take
            # material away round the rounded edge (same shape), while KOMPAS cannot round an edge
            # already broken by a notch next to it
            notes.append("Скругления рёбер отдельной операцией после вырезов по контурам: " +
                         ", ".join(f"R{k:g} ×{n}" for k, n in sorted(made.items())))
    return notes


def _pockets(dxf_path: str, views: list, front_i: int, side_i: int, numbers: dict, answers: dict,
             top_i: int | None = None, back: dict | None = None, end_floor: list | None = None) -> dict:
    """Vector drawings with a main view and a left view: the closed areas inside the main view's
    outline are pockets, steps and holes going in from the front face; how deep each goes is read
    off the left view — at the area's top and bottom edge the left view has a horizontal line
    running in from the front face to the floor. Areas the left view does not show become
    questions."""
    L, W, H = numbers["length"], numbers["width"], numbers["height"]
    # back: the view of the back face laid over the main view's place ({"doc", "view"}); its areas
    # go in from the back face, depths read off the sections' other edge
    doc = back["doc"] if back else dxf.read_dxf(dxf_path)
    bm = views[front_i]["box_mm"]
    bs = views[side_i]["box_mm"] if side_i is not None else None  # no left view: depths are asked
    sx, sz = L / (bm[2] - bm[0]), H / (bm[3] - bm[1])
    sy = W / (bs[2] - bs[0]) if bs else 1.0
    # sections: the depth is read off the metal right where the cut runs through the main view
    side_sec = bs is not None and views[side_i].get("section")
    bt = views[top_i]["box_mm"] if top_i is not None else None
    top_sec = bt is not None and views[top_i].get("section")
    x_cut = dxf.cut_position(doc, bm, bs, "x") if side_sec else None
    y_cut = dxf.cut_position(doc, bm, bt, "y") if top_sec else None
    # which edge of each section is the front face: from the arrows of its cutting line
    side_front = dxf.cut_front(doc, bm, bs, "x") if side_sec else "right"
    top_front = dxf.cut_front(doc, bm, bt, "y") if top_sec else "bottom"
    if back:
        opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
        side_front, top_front = opposite[side_front], opposite[top_front]
    side_depth = dxf.section_depths(doc, bs, side_front) if side_sec else None
    top_depth = dxf.section_depths(doc, bt, top_front) if top_sec else None
    if side_depth is None:
        x_cut = None
    if top_depth is None:
        y_cut = None
    # horizontal visible lines of the left view: (z in the main view's drawing units, depth from the front)
    # plain (not cut) views beside the main one: the left view to its right (front face at its right
    # edge) and the right view to its left (front face at its left edge)
    beside, beside_outline = [], {}
    for i, v in (enumerate(views) if not back else []):
        vb = v.get("box_mm")
        if i == front_i or not vb or v.get("section"):
            continue
        over = min(vb[3], bm[3]) - max(vb[1], bm[1])
        if over < 0.9 * (bm[3] - bm[1]) or abs((vb[3] - vb[1]) - (bm[3] - bm[1])) > 0.05 * (bm[3] - bm[1]):
            continue
        if vb[0] >= bm[2] - 0.5:
            beside.append((vb, "right"))
        elif vb[2] <= bm[0] + 0.5:
            beside.append((vb, "left"))
        if beside and beside[-1][0] is vb:
            beside_outline[len(beside) - 1] = (v.get("outline") or {}).get("points")

    def front_at(z: float) -> float | None:
        """How deep the part's front-most material lies at height z (main view's sheet mm), from the
        outline of the view beside: 0 where something reaches the front face, more beside a hub."""
        best = None
        for n, (vb, side) in enumerate(beside):
            pts = beside_outline.get(n)
            if not pts:
                continue
            y = vb[1] + (z - bm[1]) * (vb[3] - vb[1]) / (bm[3] - bm[1])
            k_y = W / (vb[2] - vb[0])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                if (y1 - y) * (y2 - y) < 0:
                    x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                    dpt = (vb[2] - x) * k_y if side == "right" else (x - vb[0]) * k_y
                    best = dpt if best is None else min(best, dpt)
        return best
    hz, vz = [], []  # horizontal lines (z, far, near) and vertical ones (depth, z0, z1) of those views
    for vb, side in beside:
        k_y = W / (vb[2] - vb[0])
        for e in doc["entities"]:
            if e["kind"] != "solid" or e.get("from_dimension") or e["type"] not in ("LINE", "LWPOLYLINE"):
                continue
            for (ax, ay), (bx, by) in dxf._segments(e):
                if abs(ax - bx) < 0.01 and vb[0] - 0.1 <= ax <= vb[2] + 0.1 and \
                        all(vb[1] - 0.1 <= y <= vb[3] + 0.1 for y in (ay, by)):
                    dz = (vb[2] - ax) * k_y if side == "right" else (ax - vb[0]) * k_y
                    zs_ = sorted(bm[1] + (y - vb[1]) * (bm[3] - bm[1]) / (vb[3] - vb[1]) for y in (ay, by))
                    vz.append((dz, zs_[0], zs_[1]))
                if abs(ay - by) < 0.01 and all(vb[0] - 0.1 <= x <= vb[2] + 0.1 for x in (ax, bx)) \
                        and vb[1] - 0.1 <= ay <= vb[3] + 0.1:
                    z = bm[1] + (ay - vb[1]) * (bm[3] - bm[1]) / (vb[3] - vb[1])
                    if side == "right":
                        hz.append((z, (vb[2] - min(ax, bx)) * k_y, (vb[2] - max(ax, bx)) * k_y))
                    else:
                        hz.append((z, (max(ax, bx) - vb[0]) * k_y, (min(ax, bx) - vb[0]) * k_y))
    ops, notes, questions = [], [], []
    fn = lambda p: ((p[0] - bm[0]) * sx, (p[1] - bm[1]) * sz)  # noqa: E731

    def fn_out(p):
        """fn for a cut's own outline: its edges lying on the part's outer faces (a notch open to
        the end, say) go half a millimetre out, so the cut never ends exactly on a face — KOMPAS
        then reports «Тело с нарушенной топологией» (the clamp's back notches at its end)."""
        x, y = fn(p)
        x = next((v + (0.5 if v else -0.5) for v in (0.0, L) if abs(x - v) < 0.05), x)
        y = next((v + (0.5 if v else -0.5) for v in (0.0, H) if abs(y - v) < 0.05), y)
        return x, y
    found = []  # (face, depth from the left view or None)
    rings = [(e.get(10, 0.0), e.get(20, 0.0), e.get(40, 0.0)) for e in doc["entities"]
             if e["type"] == "CIRCLE" and e["kind"] == "solid" and not e.get("from_dimension")]
    probe_x = (x_cut,) if x_cut is not None else ()
    probe_y = (y_cut,) if y_cut is not None else ()
    faces_all = dxf.faces(doc, bm, probe_x=probe_x, probe_y=probe_y)

    # raised round hubs: a circle on the main view where the view beside steps — the part reaches
    # the front face just inside the circle, top and bottom alike, and lies deeper (the same depth on
    # both sides) just outside it (the gear's Ø7 hub). Its circle is no hole and the areas inside it
    # are the hub's end, not pockets
    hub_circles = []
    cands = list(rings)
    for f in faces_all:
        segs = f["outline"].get("segs") or []
        if segs and all(s["type"] == "arc" for s in segs):
            c0, r0 = segs[0]["c"], segs[0]["r"]
            if all(math.dist(s["c"], c0) < 0.02 and abs(s["r"] - r0) < 0.02 for s in segs) and \
                    abs(sum(arrange._sweep(s) for s in segs) - 2 * math.pi) < 0.05:
                cands.append((c0[0], c0[1], r0))
    for cx, cy, r in cands:
        if any(math.hypot(cx - a, cy - b) < 0.05 and abs(r - rr) < 0.05 for a, b, rr, _ in hub_circles):
            continue
        ins = [front_at(cy + r - 0.08), front_at(cy - r + 0.08)]
        outs = [front_at(cy + r + 0.08), front_at(cy - r - 0.08)]
        if None in ins or None in outs:
            continue
        if max(ins) < 0.05 and min(outs) > 0.3 and abs(outs[0] - outs[1]) < 0.05:
            hub_circles.append((cx, cy, r, (outs[0] + outs[1]) / 2))

    def in_hub(f):
        """The area lies inside a hub's circle (its box within the circle's)."""
        b = f["box"]
        return any(cx - r - 0.1 <= b[0] and b[2] <= cx + r + 0.1 and cy - r - 0.1 <= b[1] and b[3] <= cy + r + 0.1
                   for cx, cy, r, _ in hub_circles)

    def thickness(f):  # how wide an area is: twice its area over its outline's length
        p = f["outline"]["points"]
        per = sum(math.dist(a, b) for a, b in zip(p, p[1:] + p[:1])) or 1e-9
        return 2 * f["area"] / per
    def ring_of(f):
        """The circle drawn on the main view that this area fills (inside a hole or a boss, or the
        band between two concentric circles), or None."""
        bb = f["box"]
        for cx, cy, r in rings:
            if math.hypot((bb[0] + bb[2]) / 2 - cx, (bb[1] + bb[3]) / 2 - cy) < 0.3 and \
                    abs(max(bb[2] - bb[0], bb[3] - bb[1]) - 2 * r) <= 0.4:
                return (cx, cy, r)
        return None
    vcx, vcz = (bm[0] + bm[2]) / 2, (bm[1] + bm[3]) / 2

    def outline_box(f):
        """The area's box from its outline, which lies on the drawing's lines (the raster box stops
        short of them by half a line)."""
        xs = [p[0] for p in f["outline"]["points"]]
        ys = [p[1] for p in f["outline"]["points"]]
        return (min(xs), min(ys), max(xs), max(ys))

    def band_kind(f):
        """A narrow band — straight along X or Z ("h" / "v"), or bent round a centre ("arc", centre,
        inner and outer radius, start and end angle) — or None."""
        bb = f["box"]
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        if min(w, h) <= 4.0 and max(w, h) >= 3 * min(w, h) and f["area"] >= 0.85 * w * h:
            return ("v",) if h > w else ("h",)
        pts = f["outline"]["points"]
        tags = {tuple(t) for ts in f["outline"].get("arcs", []) for t in ts if t}
        for cx, cy, _ in tags:
            rs = [math.hypot(x - cx, y - cy) for x, y in pts]
            r0, r1 = min(rs), max(rs)
            if not (0.3 <= r1 - r0 <= 4.0) or sum(1 for t in tags if math.hypot(t[0] - cx, t[1] - cy) < 0.05) < 2:
                continue
            ang = sorted(math.degrees(math.atan2(y - cy, x - cx)) % 360 for x, y in pts)
            gaps = [(ang[(k + 1) % len(ang)] - ang[k]) % 360 for k in range(len(ang))]
            k = max(range(len(gaps)), key=lambda k: gaps[k])
            a0, a1 = ang[(k + 1) % len(ang)], ang[k]           # the band runs from a0 to a1 counter-clockwise
            sweep = (a1 - a0) % 360
            if sweep < 5 or abs(f["area"] - math.radians(sweep) * (r1 * r1 - r0 * r0) / 2) > 0.25 * f["area"]:
                continue
            return ("arc", cx, cy, r0, r1, a0, a0 + sweep)
        return None

    EXT = 0.4  # mm read past a band's edges

    def band_profile(f):
        """The cross-section of a narrow band read off a section cutting across it: [(s, depth)], s
        measured from the band's edge on the side of the view's middle (the pocket side). None when
        no section crosses it or its floor is flat."""
        kind = band_kind(f)
        if not kind or kind[0] == "arc":
            return None
        bb = outline_box(f)
        pts = []
        if kind[0] == "v" and y_cut is not None and bb[1] <= y_cut <= bb[3]:
            inward_hi = (bb[0] + bb[2]) / 2 < vcx
            x = bb[0] - EXT
            while x <= bb[2] + EXT + 1e-9:
                v = top_depth(bt[0] + (x - bm[0]) * (bt[2] - bt[0]) / (bm[2] - bm[0]))
                if v is not None:
                    pts.append(((bb[2] - x) if inward_hi else (x - bb[0]), v * W / (bt[3] - bt[1])))
                x += 0.05
        elif kind[0] == "h" and x_cut is not None and bb[0] <= x_cut <= bb[2]:
            inward_hi = (bb[1] + bb[3]) / 2 < vcz
            z = bb[1] - EXT
            while z <= bb[3] + EXT + 1e-9:
                v = side_depth(bs[1] + (z - bm[1]) * (bs[3] - bs[1]) / (bm[3] - bm[1]))
                if v is not None:
                    pts.append(((bb[3] - z) if inward_hi else (z - bb[1]), v * sy))
                z += 0.05
        # the band's own area stops short of the lines around it (and of a narrow strip between two
        # lines drawn close together): the section is read a little past both edges — on the outer
        # side only while it is still below the front face
        pts.sort()
        w_in = (bb[2] - bb[0]) if kind[0] == "v" else (bb[3] - bb[1])
        while pts and pts[-1][0] > w_in and pts[-1][1] <= 0.05:
            pts.pop()
        if len(pts) < 5:
            return None
        ds = [d for _, d in pts]
        if max(ds) - min(ds) < 0.3:
            return None
        return [(round(s, 3), round(d, 3)) for s, d in dxf._rdp_open(pts, 0.02)]

    sec_cycles = {}

    def band_segs(f, prof):
        """The same cross-section as the section's own lines and arcs (the groove's rounded bottom a
        true arc): the pieces of the section that run along the measured profile, chained, in (s,
        depth) — or None when they do not make one unbroken chain across it."""
        kind = band_kind(f)
        bb = outline_box(f)
        if kind[0] == "v":
            box, front, inward_hi = bt, top_front, (bb[0] + bb[2]) / 2 < vcx
            k_al = (bm[2] - bm[0]) / (bt[2] - bt[0])
            k_d = W / (bt[3] - bt[1])

            def T(p):
                xm = bm[0] + (p[0] - bt[0]) * k_al
                d = (bt[3] - p[1]) if front == "top" else (p[1] - bt[1])
                return ((bb[2] - xm) if inward_hi else (xm - bb[0]), d * k_d)
            uniform = abs(k_al * sx - k_d) <= 0.01 * k_d
        else:
            box, front, inward_hi = bs, side_front, (bb[1] + bb[3]) / 2 < vcz
            k_al = (bm[3] - bm[1]) / (bs[3] - bs[1])
            k_d = sy

            def T(p):
                zm = bm[1] + (p[1] - bs[1]) * k_al
                d = (bs[2] - p[0]) if front == "right" else (p[0] - bs[0])
                return ((bb[3] - zm) if inward_hi else (zm - bb[1]), d * k_d)
            uniform = abs(k_al * sz - k_d) <= 0.01 * k_d
        # (s, depth) is one scale with the model only when the main view is drawn 1:1 (vector views are)
        uniform = uniform and abs(sx - 1) < 0.01 and abs(sz - 1) < 0.01
        if not uniform or front not in (("top", "bottom") if kind[0] == "v" else ("left", "right")):
            return None
        key = tuple(box)
        if key not in sec_cycles:
            sec_cycles[key] = arrange.build(doc, box)
        ps = sorted(prof)
        s0, s1 = ps[0][0], ps[-1][0]

        def depth_at(s):
            for (sa, da), (sb, db) in zip(ps, ps[1:]):
                if sa <= s <= sb:
                    return da if sb - sa < 1e-9 else da + (db - da) * (s - sa) / (sb - sa)
            return None
        def off(pt):
            """How far a point is from the measured profile (a polyline in (s, depth))."""
            best = 9.0
            for (sa, da), (sb, db) in zip(ps, ps[1:]):
                L2 = (sb - sa) ** 2 + (db - da) ** 2
                t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((pt[0] - sa) * (sb - sa) + (pt[1] - da) * (db - da)) / L2))
                best = min(best, math.dist(pt, (sa + t * (sb - sa), da + t * (db - da))))
            return best
        seen, pieces, allp = set(), [], []
        for c in sec_cycles[key]:
            for s in c["segs"]:
                k = tuple(sorted((tuple(round(v, 4) for v in s["a"]), tuple(round(v, 4) for v in s["b"]))))
                if k in seen:
                    continue
                seen.add(k)
                sd = [T(p) for p in arrange.sample(s, 6)]
                allp.append(arrange.map_segs([s], T)[0])
                # along the measured profile all the way (to its accuracy: a pixel, more on a steep
                # wall where the half stroke the measure takes off goes sideways)
                if all(s0 - 0.05 <= u <= s1 + 0.05 and off((u, v)) <= 0.2 for u, v in sd):
                    pieces.append(allp[-1])
        if not pieces:
            return None
        span = lambda p: abs(p["b"][0] - p["a"][0])  # noqa: E731
        JOIN = arrange.TOL + 0.01   # pieces meet within the tolerance their ends were merged with
        # chained from the smallest s; where two pieces could go on (an edge drawn twice, or a tangent
        # edge beside the section's line) the one reaching further
        first = min(min(p["a"][0], p["b"][0]) for p in pieces)
        start = max((p for p in pieces if min(p["a"][0], p["b"][0]) <= first + 0.05), key=span)
        if start["b"][0] < start["a"][0]:
            start = arrange.reverse(start)
        chain = [start]
        rest = [p for p in pieces if p is not start]
        while rest:
            end = chain[-1]["b"]
            cands = [(p, False) for p in rest if math.dist(p["a"], end) < JOIN] + \
                [(p, True) for p in rest if math.dist(p["b"], end) < JOIN]
            cands = [(p, r) for p, r in cands if (p["a"] if r else p["b"])[0] > end[0] - 0.05]
            if not cands:
                break
            p, r = max(cands, key=lambda c: (c[0]["a"] if c[1] else c[0]["b"])[0])
            rest.remove(p)
            chain.append(arrange.reverse(p) if r else p)
        # the measure stops a little past the band's edge, the groove's wall need not: it is
        # followed on along the section's own lines while it climbs, up to the front face. Closed
        # straight up from where the measure stopped, it left a sliver of wall a tenth of a
        # millimetre thick standing to the full height (seen on the plate's grooves)
        used = {id(p) for p in chain}
        while chain[-1]["b"][1] > 0.05:
            end = chain[-1]["b"]
            cands = [(p, False) for p in allp if id(p) not in used and math.dist(p["a"], end) < JOIN] + \
                [(p, True) for p in allp if id(p) not in used and math.dist(p["b"], end) < JOIN]
            # onwards (not back across the groove) and upwards, within a millimetre and a half
            cands = [(p, r) for p, r in cands if (p["a"] if r else p["b"])[0] >= end[0] - 0.02
                     and (p["a"] if r else p["b"])[1] < end[1] - 0.01 and (p["a"] if r else p["b"])[0] <= s1 + 1.5]
            if not cands:
                break
            p, r = min(cands, key=lambda c: (c[0]["a"] if c[1] else c[0]["b"])[1])
            used.add(id(p))
            chain.append(arrange.reverse(p) if r else p)
        lo, hi = chain[0]["a"][0], chain[-1]["b"][0]
        if not any(p["type"] == "arc" for p in chain) or hi - lo < 0.5 * (s1 - s0):
            return None
        # beyond the chain's ends the measured profile must be the floor next to the groove (no
        # deeper than where the measure starts or ends): else the closing line up from the chain's
        # end would leave part of the groove's wall uncut
        if any(d > ps[0][1] + 0.12 for s, d in ps if s < lo - 0.05) or \
                any(d > ps[-1][1] + 0.12 for s, d in ps if s > hi + 0.05):
            return None
        return chain

    # the body's own front face is the biggest area against the outline; other areas against it are
    # steps and recesses open to the side, taken only where a section or a side view shows their depth
    top_face = max((f for f in faces_all if f["body"]), key=lambda f: f["area"], default=None)
    for f in sorted(faces_all, key=lambda f: -f["area"]):
        if f["area"] < 1.0:
            continue
        bb = f["box"]
        if min(bb[2] - bb[0], bb[3] - bb[1]) < 0.3:
            continue  # a sliver between two lines drawn close together (an edge seen twice)
        f["ring"] = ring_of(f)
        cut = []  # depths where a section's cut crosses this face
        for z in f["on_x"].get(x_cut, []) if x_cut is not None else []:
            v = side_depth(bs[1] + (z - bm[1]) * (bs[3] - bs[1]) / (bm[3] - bm[1]))
            if v is not None:
                cut.append(v * sy)
        for x in f["on_y"].get(y_cut, []) if y_cut is not None else []:
            v = top_depth(bt[0] + (x - bm[0]) * (bt[2] - bt[0]) / (bm[2] - bm[0]))
            if v is not None:
                cut.append(v * W / (bt[3] - bt[1]))
        if cut:
            cut.sort()
            depth = cut[len(cut) // 2]
            # measured on the section to about a pixel: only a size written that close is the intended one
            # …or a step dimensioned from a deeper floor already read (a boss «2» high on a 4.6
            # floor): whichever is closer to what was measured
            cands = [(abs(v - depth), float(v)) for v in numbers.get("y", []) + numbers.get("x", [])
                     if abs(v - depth) <= 0.1]
            fb = f["box"]
            around = [d0 for g0, d0, h0 in found if h0 == "по разрезу" and d0 and g0["box"][0] <= fb[0]
                      and fb[2] <= g0["box"][2] and g0["box"][1] <= fb[1] and fb[3] <= g0["box"][3]]
            cands += [(abs(d0 - v - depth), round(d0 - v, 3)) for d0 in around
                      for v in numbers.get("y", []) if 0 < v < d0 and abs(d0 - v - depth) <= 0.06]
            if cands:
                depth = min(cands)[1]
            if end_floor and not back:
                # lying on the floor the end view's cut has left (a slot through a raised block):
                # already cut there, with its real (rounded) bottom — a flat cut would go too deep
                zc = ((bb[1] + bb[3]) / 2 - bm[1]) * sz
                fl = min(end_floor, key=lambda t: abs(t[0] - zc))
                # the end view shows the floor across the whole width; a section through the middle
                # may show a narrow channel deeper still — the floor is what the area is cut to
                if abs(fl[0] - zc) < 0.3 and fl[1] > 0.5 and fl[1] - 0.6 <= depth <= fl[1] + 3.0:
                    found.append((f, 0.0, "по виду с торца"))
                    continue
            found.append((f, round(depth, 2) if depth > 0.15 else 0.0, "по разрезу"))
            prof = band_profile(f)
            if prof:
                f["profile"] = prof
                try:
                    segs = band_segs(f, prof)
                except Exception:  # noqa: BLE001 - the measured profile stays
                    segs = None
                if segs:
                    f["profile_segs"] = segs
            continue
        if f is top_face:
            continue  # no section through it: the body's own front face
        if back and back.get("sil_lines") is not None:
            # a recess in the back face shows on the plain view from the side as a step at its
            # back edge: a line along the recess, its distance from the back edge the depth
            fb = outline_box(f)
            fw = fb[2] - fb[0]
            best = None
            for y_d, x0_, x1_ in back["sil_lines"]:
                cover = max(0.0, min(x1_, fb[2]) - max(x0_, fb[0]))
                if fw > 0 and cover >= 0.6 * fw and (x1_ - x0_) <= 1.6 * fw + 1 and 0.15 < y_d < 0.6 * W:
                    if best is None or abs((x1_ - x0_) - fw) < best[0]:
                        best = (abs((x1_ - x0_) - fw), y_d)
            if best:
                d0 = best[1]
                near = min(numbers.get("y", []) + numbers.get("x", []), key=lambda v: abs(v - d0), default=None)
                found.append((f, float(near) if near is not None and abs(near - d0) <= 0.1 else round(d0, 2),
                              "по виду сбоку"))
                continue
        ys = [p[1] for p in f["outline"]["points"]]
        depths, hub_top = [], False
        for z in (min(ys), max(ys)):
            if f["body"] and (abs(z - bm[1]) < 0.5 or abs(z - bm[3]) < 0.5):
                continue  # an edge of the part itself: its line in the side view is the outline
            # drawings are often drawn a few tenths off between views: half a millimetre of slack
            # a line that is the outline's own step — the part reaches the front face on the area's
            # side of it and only lies that deep beyond it — is the edge of a raised hub the area is
            # the top of, not the wall of a pocket (the gear's hub end)
            # (only inside a round hub found above: elsewhere such a line is a pocket's wall)
            step = False
            if hub_circles and not f["body"] and in_hub(f):
                zin = z + (0.4 if z == min(ys) else -0.4)
                zout = z - (0.4 if z == min(ys) else -0.4)
                fin, fout = front_at(zin), front_at(zout)
                step = fin is not None and fout is not None and fin < 0.1 and fout > 0.3
            if step and any(abs(zz - z) < 0.5 and near < 0.5 * W and abs(fout - far) < 0.15 for zz, far, near in hz):
                hub_top = True
            at = [far for zz, far, near in hz if abs(zz - z) < 0.5 and near < 0.5 * W
                  and not (f["body"] and far >= W - 0.05)
                  and not (step and abs(fout - far) < 0.15)]
            if at:
                depths.append(max(at))
        if hub_top and not f["body"]:
            found.append((f, 0.0, "по виду сбоку"))
            continue
        depth = None
        if depths:
            # each edge must agree: a line across the whole left view at one edge's height is some
            # other edge seen there (a boss, a step), the floor is where both edges reach
            depth = round(min(depths), 2)
            # a line across the area's whole height short of that is its floor: the edge lines can
            # run on past it (the gear's slot: its sides drawn to the rim, its floor at 2.2)
            z0, z1 = min(ys), max(ys)
            # (the floor may be drawn in pieces)
            floors = []
            for d_ in {round(d_, 2) for d_, a, b in vz if 0.1 < d_ < depth - 0.1}:
                cover = sum(max(0.0, min(b, z1) - max(a, z0)) for dd, a, b in vz if abs(dd - d_) <= 0.02)
                if cover >= 0.8 * (z1 - z0):
                    floors.append(d_)
            if floors and not f["body"] and hub_circles and in_hub(f):   # (a slot in a hub's end)
                depth = round(max(floors), 2)
            # …and a size written on the drawing that close is the intended one
            near = min(numbers.get("y", []) + numbers.get("x", []), key=lambda v: abs(v - depth), default=None)
            if near is not None and abs(near - depth) <= 0.3:
                depth = float(near)
        if depth is not None and not f["body"] and (side_sec or top_sec):
            # a side view cannot tell how far in from the edge a line lies: inside the outline,
            # with sections on the drawing, a neighbour read on a section says more
            depth = None
        if depth is not None and f["body"]:
            # a recess shows in the side view as a notch: its floor is a line at that depth along
            # the recess, not just two lines at its ends (grooves across the edge, or other edges
            # that happen to lie at the same height). Where sections are drawn they say more, and
            # a side view alone is believed only that far
            z0, z1 = min(ys), max(ys)
            cover = sum(max(0.0, min(b, z1) - max(a, z0)) for d_, a, b in vz if abs(d_ - depth) <= 0.15)
            if cover < 0.8 * (z1 - z0):
                depth = None
        if f["body"] and depth is None:
            continue  # part of the front face, cut off from it by some line
        found.append((f, depth, "по виду сбоку"))

    def touching(a, b, tol=0.3):
        return a[0] - tol <= b[2] and b[0] - tol <= a[2] and a[1] - tol <= b[3] and b[1] - tol <= a[3]

    def region(polys):
        """Where a question is about, for the review window: outlines on the main view's picture as
        fractions of its size (the picture shows the view with a 4 mm margin, y down)."""
        X0, X1, Y0, Y1 = bm[0] - 4, bm[2] + 4, bm[1] - 4, bm[3] + 4
        out = []
        for pts in polys:
            step = max(1, len(pts) // 120)
            out.append([[round((x - X0) / (X1 - X0), 4), round((Y1 - y) / (Y1 - Y0), 4)] for x, y in pts[::step]])
        return {"image": (back["view"] if back else front_i) + 1, "polygons": out}
    # a circle crossed by a section shows what it is — a boss (metal up to some depth inside it) or a
    # blind recess; circles of the same size not crossed by any section are taken to be the same
    by_ring = {}
    for f, d, h in found:
        if f.get("ring") and d is not None and h in ("по разрезу", "по виду сбоку"):
            by_ring.setdefault(round(f["ring"][2], 1), []).append((f, d))
    for k, (f, d, h) in enumerate(found):
        if f.get("ring") and d is None:
            same = [(g, dd) for g, dd in by_ring.get(round(f["ring"][2], 1), []) if abs(g["area"] - f["area"]) <= 0.2 * g["area"]]
            if same:
                found[k] = (f, same[0][1], "как у такой же окружности на разрезе")
    solid_rings = {f["ring"][:2] for f, d, h in found if f.get("ring") and d is not None and d < W - 0.05}
    # a boss drawn with two circles close together: the inner one is where its top fillet meets the
    # flat top — the band between them is that fillet, not a step of its own
    fillets = []  # (radius, model point on the rounded edge)
    by_centre: dict = {}
    for k, (f, d, h) in enumerate(found):
        if f.get("ring") and d is not None and 0 < d < W - 0.05:
            by_centre.setdefault((round(f["ring"][0], 1), round(f["ring"][1], 1)), []).append(k)
    for (cx, cz), ks in by_centre.items():
        if len(ks) != 2:
            continue
        inner, outer = sorted(ks, key=lambda k: found[k][0]["ring"][2])
        ri, ro = found[inner][0]["ring"][2], found[outer][0]["ring"][2]
        if not 0.1 <= ro - ri <= 1.0:
            continue
        d_top = found[inner][1]
        found[outer] = (found[outer][0], d_top, found[outer][2])
        ccx, ccz = found[outer][0]["ring"][:2]
        fillets.append((round((ro - ri) * sx, 3), [(ccx + ro - bm[0]) * sx, d_top, (ccz - bm[1]) * sz]))
    # first pass: each area's depth (asked where nothing on the drawing says it)
    plan = []  # (face, depth, how, where, qid, asked)
    for f, depth, how in found:
        if f.get("ring") and depth is None:
            continue  # a hole: cut below with the other holes of its size
        ys = [p[1] for p in f["outline"]["points"]]
        xs = [p[0] for p in f["outline"]["points"]]
        x_lo, x_hi = (min(xs) - bm[0]) * sx, (max(xs) - bm[0]) * sx
        z_lo, z_hi = (min(ys) - bm[1]) * sz, (max(ys) - bm[1]) * sz
        where = f"X {x_lo:.1f}…{x_hi:.1f}, Z {z_lo:.1f}…{z_hi:.1f}"
        pre = "back_" if back else ""
        qid = f"{pre}pocket_{x_lo:.0f}_{z_lo:.0f}"
        k = 2
        while any(q["id"] == qid for q in questions):  # two areas starting at the same corner
            qid = f"{pre}pocket_{x_lo:.0f}_{z_lo:.0f}_{k}"
            k += 1
        asked = depth is None
        if asked:
            if f["area"] < 2.0:
                continue
            # not in the left view: a compact area opening onto a pocket (a slot cut on from it) is
            # most likely as deep as that pocket — proposed, the user confirms
            bb = f["box"]
            compact = f["area"] >= 0.8 * (bb[2] - bb[0]) * (bb[3] - bb[1])
            nb = [d for g, d, _ in found if d and g is not f and touching(bb, g["box"]) and g["area"] > f["area"]]
            guess = max(nb) if compact and nb else 0
            if not guess:
                # a piece of a band (a groove bending round a corner): as deep as the band's
                # straight piece next to it — a resolved neighbour of the same width
                t = thickness(f)
                same = [(g["area"], d, g) for g, d, h in found if h == "по разрезу" and d and
                        faces_all.index(g) in f.get("touches", {}) and 0.6 * t <= thickness(g) <= 1.6 * t]
                if same:
                    _, guess, g = max(same, key=lambda s: s[0])
                    if g.get("profile"):
                        f["profile"] = g["profile"]  # the same groove going on: the same cross-section
                        if g.get("profile_segs"):
                            f["profile_segs"] = g["profile_segs"]
            questions.append({"id": qid, "text": (f"Область на виде с тыльной стороны ({where}): на какую "
                                                   f"глубину она уходит от тыльной стороны?" if back else
                                                   f"Область на главном виде ({where}): на какую глубину она "
                                                   f"уходит от лицевой стороны?") + f" (0 — это не вырез, {W:g} — насквозь)",
                              "default": guess, "unit": "мм",
                              "region": region([f["outline"]["points"]])})
            depth = guess
        if qid in answers:
            depth = float(answers[qid])
        plan.append((f, depth, how, where, qid, asked))
    # straight grooves run a hair past their ends: where one meets the turn of the next, faces that
    # coincide exactly leave KOMPAS a body it cannot resolve
    OV = 0.02

    def profile_ops(f, where):
        """A groove cut with its real cross-section: straight bands extruded along their length,
        bands bent round a corner revolved about the corner's centre."""
        kind = band_kind(f)
        prof = f["profile"]
        if not kind:
            return None
        bb = outline_box(f)
        w = prof[-1][0]

        def closed(fn2):
            """The cross-section closed above the front face, (s, depth) → sketch by fn2: the
            section's own lines and arcs when they were found, else the measured points."""
            segs = f.get("profile_segs")
            if segs:
                segs = [dict(s) for s in segs]
                # the start a few thousandths off a floor the drawing dimensions (the pocket the
                # groove runs along, 4.6) is on that floor, else a step that high is left at the
                # pocket's wall: the whole section moves by those thousandths (the section's scale
                # across). And an end a few hundredths below the front face is on it (else a film
                # that thin is left standing): the line ending there is brought up to depth 0
                floors = [0.0] + [float(v) for v in numbers.get("y", []) + numbers.get("x", []) if 0.05 < v < W]
                s0_, d0 = segs[0]["a"]
                near = min(floors, key=lambda v: abs(v - d0), default=None)
                # likewise it starts on the band's edge (where the pocket's wall is) — two
                # thousandths over it, so the pocket's wall and the groove's never meet exactly
                ds = -s0_ - 0.002 if abs(s0_) < 0.03 else 0.0
                dd = near - d0 if near is not None and abs(near - d0) < 0.06 else 0.0
                if ds or dd:
                    mv = lambda p: (p[0] + ds, p[1] + dd)  # noqa: E731
                    segs = [dict(s, a=mv(s["a"]), b=mv(s["b"]), **({"c": mv(s["c"])} if s["type"] == "arc" else {}))
                            for s in segs]
                for s, k in ((segs[0], "a"), (segs[-1], "b")):
                    if s["type"] == "line" and 0 < s[k][1] < 0.03:
                        s[k] = (s[k][0], 0.0)
                # an arc ending that close below the face is carried up to it by a short line
                if segs[-1]["type"] == "arc" and 0 < segs[-1]["b"][1] < 0.03:
                    e = segs[-1]["b"]
                    segs.append({"type": "line", "a": e, "b": (e[0] + 0.05, 0.0)})
                if segs[0]["type"] == "arc" and 0 < segs[0]["a"][1] < 0.03:
                    s_ = segs[0]["a"]
                    segs.insert(0, {"type": "line", "a": (s_[0] - 0.05, 0.0), "b": s_})
                S, E = segs[0]["a"], segs[-1]["b"]
                # the end on a floor already cut (the pocket the groove runs along) goes on a little
                # into that pocket instead of rising exactly along its wall: two walls meeting
                # exactly leave a face a few thousandths wide
                # (rising as it goes, so it never runs along that floor either)
                ext = list(segs)
                if E[1] > 0.15:
                    ext.append({"type": "line", "a": E, "b": (E[0] + 0.05, E[1] - 0.1)})
                    E = (E[0] + 0.05, E[1] - 0.1)
                if S[1] > 0.15:
                    ext.insert(0, {"type": "line", "a": (S[0] - 0.05, S[1] - 0.1), "b": S})
                    S = (S[0] - 0.05, S[1] - 0.1)
                ext += [{"type": "line", "a": E, "b": (E[0], -1.0)},
                        {"type": "line", "a": (E[0], -1.0), "b": (S[0], -1.0)},
                        {"type": "line", "a": (S[0], -1.0), "b": S}]
                o, u = fn2((0.0, 0.0)), fn2((1.0, 0.0))
                return dxf._segs_shapes(ext, fn2, math.dist(o, u))
            # depths of a few hundredths are the front face itself (a film that thin left otherwise)
            pts = [(s, 0.0 if d < 0.03 else d) for s, d in prof] + [(w, -1.0), (prof[0][0], -1.0)]
            return [{"type": "polyline", "closed": True, "points": [list(fn2(p)) for p in pts]}]
        if kind[0] == "v":
            inward_hi = (bb[0] + bb[2]) / 2 < vcx
            edge = bb[2] if inward_hi else bb[0]
            X = lambda s: ((edge - s if inward_hi else edge + s) - bm[0]) * sx  # noqa: E731
            return [{"op": "cut", "plane": "XY", "offset": (bb[1] - bm[1]) * sz - OV, "depth": (bb[3] - bb[1]) * sz + 2 * OV,
                     "shapes": closed(lambda p: (X(p[0]), p[1])), "comment": f"канавка по сечению с разреза ({where})"}]
        if kind[0] == "h":
            inward_hi = (bb[1] + bb[3]) / 2 < vcz
            edge = bb[3] if inward_hi else bb[1]
            Z = lambda s: ((edge - s if inward_hi else edge + s) - bm[1]) * sz  # noqa: E731
            return [{"op": "cut", "plane": "YZ", "offset": (bb[0] - bm[0]) * sx - OV, "depth": (bb[2] - bb[0]) * sx + 2 * OV,
                     "shapes": closed(lambda p: (p[1], Z(p[0]))), "comment": f"канавка по сечению с разреза ({where})"}]
        _, cx, cy, r0, r1, a0, a1 = kind
        if abs(sx - sz) > 1e-6 or r1 - r0 < 0.5 * w:
            return None
        # the revolve starts in the plane through the centre parallel to XY: at 0° or 180°
        ends = [(a0 % 360, 1), (a1 % 360, -1)]
        start = next(((a, d) for a, d in ends if min(abs(a - 0), abs(a - 360), abs(a - 180)) < 1.0), None)
        if start is None:
            return None
        a, d = start
        side = 1 if min(abs(a), abs(a - 360)) < 1.0 else -1   # +X or −X of the centre
        X = lambda s: (cx + side * (r0 + s) - bm[0]) * sx  # noqa: E731
        axis = {"type": "line", "x1": (cx - bm[0]) * sx, "y1": -5, "x2": (cx - bm[0]) * sx, "y2": W + 5, "style": 3}
        # KOMPAS turns a profile lying on +X towards +Z, one on −X also towards +Z (measured): from
        # 0° that is the counter-clockwise way, from 180° the clockwise one
        ccw = d > 0
        return [{"op": "revolve", "cut": True, "plane": "XY", "offset": (cy - bm[1]) * sz, "angle": a1 - a0,
                 "reverse": ccw != (side > 0), "shapes": closed(lambda p: (X(p[0]), p[1])) + [axis],
                 "comment": f"канавка на повороте по сечению с разреза ({where})"}]

    # second pass: every area is cut to its own depth — the areas inside it that have a depth of
    # their own (a boss, a deeper pocket, an area the user said is not cut) are left out of its
    # sketch as islands and get their own cut
    own = {faces_all.index(f): (d, qid in answers) for f, d, _, _, qid, _ in plan}
    for f, depth, how, where, qid, asked in plan:
        if depth <= 0.01:
            continue
        fb = f["box"]
        islands = []
        for j in f.get("touches", {}):
            g = faces_all[j]
            gb = g["box"]
            if j not in own or g["area"] >= f["area"] or not (fb[0] - 0.05 <= gb[0] and gb[2] <= fb[2] + 0.05
                                                              and fb[1] - 0.05 <= gb[1] and gb[3] <= fb[3] + 0.05):
                continue
            if not arrange._inside(_centroid(g["outline"]["points"]), f["outline"]["points"]):
                continue   # beside the area (in the bay of a ring, say), not inside it
            d, said = own[j]
            if d > 0.01 or said:
                islands.append(g)
        prof_ops = profile_ops(f, where) if f.get("profile") and qid not in answers and not islands \
            and not back else None
        if prof_ops:
            ops += prof_ops
            notes.append(f"Канавка {where}: сечение с разреза (дно до {max(d for _, d in f['profile']):.2f} мм, "
                         f"скруглённое как на разрезе)")
            continue
        shapes = dxf.outline_shapes(f["outline"], fn_out)
        for g in islands:
            shapes += dxf.outline_shapes(g["outline"], fn)
        isl = f", островков {len(islands)}" if islands else ""
        if depth >= W - 0.05:
            ops.append({"op": "cut", "plane": "XZ", "offset": -1, "through_all": True, "shapes": shapes,
                        "comment": f"сквозной вырез ({where})"})
            notes.append(f"Сквозной вырез {where}")
        else:
            ops.append({"op": "cut", "plane": "XZ", "offset": W if back else 0, "depth": depth, "shapes": shapes,
                        **({"reverse": True} if back else {}),
                        "comment": ("с тыльной стороны: " if back else "") + f"полость глубиной {depth:g} ({where}{isl})"})
            src = ("ваш ответ" if qid in answers else how if not asked
                   else "как у соседней полости (предложено, проверьте в вопросах)")
            kind = ("Бобышка/дно внутри окружности" if f.get("ring") else "Полость") + (" с тыльной стороны" if back else "")
            notes.append(f"{kind} {where}: глубина {depth:g} мм — {src}")
    if back:
        # the through holes are cut from the front already; no fillets from this side yet
        return {"operations": ops, "notes": notes, "questions": questions}
    # holes: whole circles drawn on the main view (CIRCLE, or a closed polyline of two half-circle
    # bulges as KOMPAS exports them). The left view usually does not cut through them, so their
    # depth is asked, through by default
    circles = {}
    for e in doc["entities"]:
        if e["kind"] != "solid" or e.get("from_dimension"):
            continue
        c = None
        if e["type"] == "CIRCLE":
            c = (e.get(10, 0.0), e.get(20, 0.0), e.get(40, 0.0))
        elif e["type"] == "LWPOLYLINE" and e.get("closed") and len(e["pts"]) == 2 \
                and all(abs(abs(b) - 1) < 1e-6 for b in (e.get("bulge") or [0, 0])[:2]):
            (ax, ay), (bx, by) = e["pts"]
            c = ((ax + bx) / 2, (ay + by) / 2, math.dist((ax, ay), (bx, by)) / 2)
        if c and bm[0] < c[0] - c[2] and c[0] + c[2] < bm[2] and bm[1] < c[1] - c[2] and c[1] + c[2] < bm[3]:
            if any(math.hypot(c[0] - x, c[1] - y) < 0.3 for x, y in solid_rings):
                continue  # not a hole: the section shows metal inside (a boss) — cut above as an area
            key = (round(c[0], 2), round(c[1], 2))   # one hole per centre (a counterbore's circle is no hole)
            if any(math.hypot(c[0] - hc[0], c[1] - hc[1]) < 0.05 and abs(c[2] - hc[2]) < 0.05 for hc in hub_circles):
                key += ("hub",)   # a hub's circle stands apart: the bore inside it has the same centre
            circles.setdefault(key, c)
    # an area whose exact outline is one circle (drawn as arcs in the PDF) inside a hub is its bore
    # (elsewhere such circles are counterbores and the like, read as areas above)
    for f in (faces_all if hub_circles else []):
        if not in_hub(f):
            continue
        segs = f["outline"].get("segs") or []
        if segs and all(s["type"] == "arc" for s in segs):
            c0, r0 = segs[0]["c"], segs[0]["r"]
            if all(math.dist(s["c"], c0) < 0.02 and abs(s["r"] - r0) < 0.02 for s in segs) and \
                    abs(sum(arrange._sweep(s) for s in segs) - 2 * math.pi) < 0.05 and \
                    not any(math.hypot(c0[0] - x, c0[1] - y) < 0.3 for x, y in solid_rings):
                hubbed = any(math.hypot(c0[0] - hc[0], c0[1] - hc[1]) < 0.05 and abs(r0 - hc[2]) < 0.05 for hc in hub_circles)
                circles.setdefault((round(c0[0], 2), round(c0[1], 2)) + (("hub",) if hubbed else ()), (c0[0], c0[1], r0))
    # a circle that is the outline of a raised hub (the view beside steps there: the part reaches
    # the front inside it, lies deeper outside) is no hole — the gear's Ø7 hub around its Ø4 bore
    hubs = []
    for key, (cx, cy, r) in list(circles.items()):
        hub = next((hc for hc in hub_circles if math.hypot(hc[0] - cx, hc[1] - cy) < 0.05 and abs(hc[2] - r) < 0.05), None)
        if hub:
            fout = hub[3]
            del circles[key]
            d_h = min(numbers.get("diameters", []), key=lambda v: abs(v - 2 * r * sx), default=None)
            d_h = d_h if d_h is not None and abs(d_h - 2 * r * sx) <= 0.1 else round(2 * r * sx, 3)
            h = min(numbers.get("y", []) + numbers.get("x", []), key=lambda v: abs(v - fout), default=None)
            h = h if h is not None and abs(h - fout) <= 0.1 else round(fout, 2)
            hubs.append((d_h, h))
            # everything round the hub lies that much deeper: the outline seen from the side cannot
            # take it away there (beside the hub's own height it keeps the whole width)
            ops.append({"op": "cut", "plane": "XZ", "offset": -1, "depth": h + 1,
                        "shapes": [{"type": "rect", "x": -1, "y": -1, "w": L + 2, "h": H + 2},
                                   {"type": "circle", "x": (cx - bm[0]) * sx, "y": (cy - bm[1]) * sz, "d": d_h}],
                        "comment": f"вокруг ступицы Ø{d_h:g} на глубину {h:g}"})
    if hubs:
        notes.append("Ступица " + ", ".join(f"Ø{d:g} высотой {h:g}" for d, h in hubs) +
                     " — выступ (по виду сбоку), не отверстие; вокруг неё снято на эту глубину")
    groups: dict[float, list] = {}
    for cx, cy, r in circles.values():
        groups.setdefault(round(2 * r * sx, 2), []).append(((cx - bm[0]) * sx, (cy - bm[1]) * sz))
    for dia, centres in sorted(groups.items()):
        d = min(numbers.get("diameters", []) + [dia], key=lambda v: abs(v - dia))
        d = d if abs(d - dia) <= 0.3 else dia
        qid = f"holes_{d:g}"
        depth = float(answers.get(qid, W))
        questions.append({"id": qid, "text": f"Отверстия Ø{d:g} ({len(centres)} шт.) на главном виде: глубина от лицевой "
                                              f"стороны? ({W:g} — насквозь)", "default": W, "unit": "мм",
                          "region": region([[(bm[0] + x / sx + d / 2 / sx * math.cos(k * math.pi / 12),
                                              bm[1] + z / sz + d / 2 / sz * math.sin(k * math.pi / 12)) for k in range(24)]
                                            for x, z in centres])})
        shapes = [{"type": "circle", "x": x, "y": z, "d": d} for x, z in centres]
        if depth >= W - 0.05:
            ops.append({"op": "cut", "plane": "XZ", "offset": -1, "through_all": True, "shapes": shapes,
                        "comment": f"отверстия Ø{d:g} насквозь"})
            notes.append(f"{len(centres)} отв. Ø{d:g} насквозь (глубину можно поменять в вопросах)")
        elif depth > 0:
            ops.append({"op": "cut", "plane": "XZ", "offset": 0, "depth": depth, "shapes": shapes,
                        "comment": f"отверстия Ø{d:g} глубиной {depth:g}"})
            notes.append(f"{len(centres)} отв. Ø{d:g} глубиной {depth:g}")
    # fillets last, once every cut is there: one operation per radius
    for rad in sorted({r for r, _ in fillets}):
        pts = [p for r, p in fillets if r == rad]
        ops.append({"op": "fillet", "radius": rad, "points": pts,
                    "comment": f"скругление R{rad:g} верха бобышек ({len(pts)} шт.)"})
        notes.append(f"Скругление R{rad:g} по верху {len(pts)} бобышек: на виде это две окружности, "
                     f"между ними {rad:g} мм")
    return {"operations": ops, "notes": notes, "questions": questions}


def _back_view(dxf_path: str, views: list, front_i: int, used: set, skip: set, sil=None,
               W: float | None = None) -> dict | None:
    """A plain view of the back face: the size of the main view, standing on its own. Which way it is
    turned is found from the through holes, which both views show: the mirroring (left-right or
    top-bottom) that puts the most of its circles onto the main view's. Returns {"doc", "view",
    "mirror"} with the back view's lines moved onto the main view's place (and the main view's own
    lines taken out), so the main view's machinery reads the back face."""
    doc = dxf.read_dxf(dxf_path)
    bm = views[front_i]["box_mm"]
    w0, h0 = bm[2] - bm[0], bm[3] - bm[1]
    cand = None
    for i, v in enumerate(views):
        b = v.get("box_mm")
        if i in used or i in skip or not b or v.get("section"):
            continue
        if abs((b[2] - b[0]) - w0) <= 0.02 * w0 and abs((b[3] - b[1]) - h0) <= 0.02 * h0:
            cand = i
            break
    if cand is None:
        return None
    bb = views[cand]["box_mm"]

    def inside(e, b):
        pts = dxf._points(e)
        return pts and all(b[0] - 0.1 <= p[0] <= b[2] + 0.1 and b[1] - 0.1 <= p[1] <= b[3] + 0.1 for p in pts)

    def circles(b):
        return [((e.get(10, 0.0) - b[0]) / (b[2] - b[0]), (e.get(20, 0.0) - b[1]) / (b[3] - b[1]), e.get(40, 0.0))
                for e in doc["entities"] if e["type"] == "CIRCLE" and e["kind"] == "solid" and inside(e, b)]
    cm, cb = circles(bm), circles(bb)
    best = None
    for mx in (False, True):
        for mz in (False, True):
            hits = sum(1 for x, y, r in cb if any(abs((1 - x if mx else x) - u) < 0.01 and abs((1 - y if mz else y) - v) < 0.01
                                                  and abs(r - q) < 0.3 for u, v, q in cm))
            if best is None or hits > best[0]:
                best = (hits, mx, mz)
    _, mx, mz = best

    def move(p):
        fx, fy = (p[0] - bb[0]) / (bb[2] - bb[0]), (p[1] - bb[1]) / (bb[3] - bb[1])
        fx, fy = (1 - fx if mx else fx), (1 - fy if mz else fy)
        return [bm[0] + fx * w0, bm[1] + fy * h0]
    ents = [e for e in doc["entities"] if not inside(e, bm)]
    for e in doc["entities"]:
        if e["type"] in ("TEXT", "MTEXT") or not inside(e, bb):
            continue
        e2 = dict(e)
        if e["type"] == "CIRCLE":
            e2[10], e2[20] = move((e[10], e[20]))
        elif e.get("pts"):
            e2["pts"] = [move(p) for p in e["pts"]]
            if mx != mz and e.get("bulge"):
                e2["bulge"] = [-b for b in e["bulge"]]  # a mirror turns arcs the other way
        else:
            continue
        ents.append(e2)
    # steps at the back edge of the plain view from the side (lined up over or under the main one):
    # (depth from the back face, x0, x1) in the main view's x
    sil_lines = None
    if sil and W:
        si, flip = sil
        sb = views[si]["box_mm"]
        k = W / (sb[3] - sb[1])
        sil_lines = []
        segs = []
        for e in doc["entities"]:
            if e["kind"] != "solid" or e.get("from_dimension") or e["type"] not in ("LINE", "LWPOLYLINE"):
                continue
            for (ax, ay), (bx, by) in dxf._segments(e, 15.0):
                if abs(ay - by) < 0.01 and sb[0] - 0.1 <= min(ax, bx) and max(ax, bx) <= sb[2] + 0.1 \
                        and sb[1] - 0.1 <= ay <= sb[3] + 0.1 and abs(bx - ax) > 0.3:
                    segs.append((ay, min(ax, bx), max(ax, bx)))
        # the back face: the longest horizontal line in the back half (the box's own edge may be a
        # chamfer or rounding below it)
        by_y: dict = {}
        for y, x0_, x1_ in segs:
            if (y - sb[1] < (sb[3] - sb[1]) / 2) == bool(flip):
                by_y[round(y, 2)] = by_y.get(round(y, 2), 0.0) + (x1_ - x0_)
        back_y = max(by_y, key=by_y.get) if by_y else (sb[1] if flip else sb[3])
        # one line drawn in pieces (a polyline read from a PDF) is one line
        merged: list = []
        for y, x0_, x1_ in sorted(segs, key=lambda s: (round(s[0], 2), s[1])):
            if merged and abs(merged[-1][0] - y) < 0.01 and x0_ <= merged[-1][2] + 0.3:
                merged[-1][2] = max(merged[-1][2], x1_)
            else:
                merged.append([y, x0_, x1_])
        for y, x0_, x1_ in merged:
            d = (y - back_y) * k if flip else (back_y - y) * k   # over the main view the back is the bottom edge
            sil_lines.append((d, bm[0] + (x0_ - sb[0]), bm[0] + (x1_ - sb[0])))
    return {"doc": {"entities": ents}, "view": cand, "mirror": mx or mz, "sil_lines": sil_lines}


def _silhouette_pockets(dxf_path: str, views: list, main_i: int, sil, numbers: dict) -> dict:
    """Areas on the plain view lined up over or under the main one (seen along Z): a channel along
    the part's bottom, open at one end («Крепление ножа»: 22 wide, 8 high, 40 long with R2.5 tool
    exits). How high it reaches is read on the main view: a line across the area's whole width, with
    the main view's lines along the area's sides running from the bottom (or the top) up to it."""
    if not sil:
        return {"operations": [], "notes": []}
    si, flip = sil
    b, m = views[si].get("box_mm"), views[main_i].get("box_mm")
    if not b or not m:
        return {"operations": [], "notes": []}
    L, W, H = numbers["length"], numbers["width"], numbers["height"]
    doc = dxf.read_dxf(dxf_path)
    fs = dxf.faces(doc, b)
    if len(fs) < 2:
        return {"operations": [], "notes": []}
    big = max(fs, key=lambda f: f["area"])
    w, h = b[2] - b[0], b[3] - b[1]
    mw, mh = m[2] - m[0], m[3] - m[1]
    X = lambda px: (px - b[0]) * L / w  # noqa: E731
    Y = (lambda py: (b[3] - py) * W / h) if flip else (lambda py: (py - b[1]) * W / h)  # noqa: E731
    hs, vs = [], []   # the main view's lines in part mm: (z, x0, x1) / (x, z0, z1)
    for e in doc["entities"]:
        if e["kind"] != "solid" or e["type"] not in ("LINE", "LWPOLYLINE") or e.get("from_dimension"):
            continue
        for (ax, ay), (bx, by) in dxf._segments(e):
            if not all(m[0] - 0.1 <= x <= m[2] + 0.1 and m[1] - 0.1 <= y <= m[3] + 0.1 for x, y in ((ax, ay), (bx, by))):
                continue
            x_a, x_b = (ax - m[0]) * L / mw, (bx - m[0]) * L / mw
            z_a, z_b = (ay - m[1]) * H / mh, (by - m[1]) * H / mh
            if abs(z_a - z_b) < 0.05:
                hs.append((z_a, min(x_a, x_b), max(x_a, x_b)))
            elif abs(x_a - x_b) < 0.05:
                vs.append((x_a, min(z_a, z_b), max(z_a, z_b)))

    def covered(lo, hi, pieces):   # length of [lo, hi] the pieces cover
        tot, cur = 0.0, lo
        for a, c in sorted(pieces):
            a, c = max(a, cur), min(c, hi)
            if c > a:
                tot += c - a
                cur = c
        return tot

    def wall(x, z0, z1):   # a line of the main view along x from z0 to z1
        return covered(z0, z1, [(a, c) for xx, a, c in vs if abs(xx - x) < 0.15]) >= 0.9 * (z1 - z0)
    ops, notes = [], []
    for f in fs:
        if f is big or f["area"] < 4:
            continue
        x0, x1 = X(f["box"][0]), X(f["box"][2])
        if x1 - x0 < 1 or x1 - x0 > L - 0.5:
            continue
        zs = sorted({round(z, 2) for z, a, c in hs if 0.3 < z < H - 0.3
                     and covered(x0, x1, [(a2, c2) for z2, a2, c2 in hs if abs(z2 - z) < 0.05]) >= 0.7 * (x1 - x0)})
        z_lo = next((z for z in zs if wall(x0, 0, z) and wall(x1, 0, z)), None)
        z_hi = next((z for z in reversed(zs) if wall(x0, z, H) and wall(x1, z, H)), None)
        if z_lo is None and z_hi is None:
            continue
        z = z_lo if z_lo is not None else z_hi
        near = min(numbers.get("x", []) + numbers.get("y", []) + numbers.get("z", []),
                   key=lambda v: abs(v - (z if z_lo is not None else H - z)), default=None)
        d = z if z_lo is not None else H - z
        d = near if near is not None and abs(near - d) <= 0.1 else round(d, 2)

        def fn(p):   # the area's points in the XY sketch, pushed past the part's own faces
            xx, yy = X(p[0]), Y(p[1])
            xx = next((q + (0.5 if q else -0.5) for q in (0.0, L) if abs(xx - q) < 0.05), xx)
            yy = next((q + (0.5 if q else -0.5) for q in (0.0, W) if abs(yy - q) < 0.05), yy)
            return xx, yy
        shapes = dxf.outline_shapes(f["outline"], fn)
        if z_lo is not None:
            ops.append({"op": "cut", "plane": "XY", "offset": -1, "depth": d + 1, "shapes": shapes,
                        "comment": f"паз снизу высотой {d:g} (X {x0:.1f}…{x1:.1f})"})
        else:
            ops.append({"op": "cut", "plane": "XY", "offset": H - d, "depth": d + 1, "shapes": shapes,
                        "comment": f"паз сверху глубиной {d:g} (X {x0:.1f}…{x1:.1f})"})
        notes.append(f"Паз X {x0:.1f}…{x1:.1f} по виду {si + 1}: {'снизу высотой' if z_lo is not None else 'сверху глубиной'} "
                     f"{d:g} мм — по линиям главного вида")
    return {"operations": ops, "notes": notes}


def _end_views(dxf_path: str, views: list, used: set, skip: set, numbers: dict, sil, side_i=None) -> dict:
    """More plain views of the part beyond the main and top ones:
    - a view of an end (its box W×H: thickness by the main view's height) is a silhouette along X —
      what lies outside its outline is cut away along the whole length (a slot running through a
      raised block shows there), and circles drawn on it are holes along X;
    - circles on the plain view lined up over or under the main one are holes across it, along Z.
    Holes are taken through (the user can change it in the plan); a view is turned so that its
    straight full-width edge, the back face, is at the back."""
    L, W, H = numbers["length"], numbers["width"], numbers["height"]
    doc = dxf.read_dxf(dxf_path)
    sys.path.insert(0, os.path.dirname(HERE))
    import kompas_api
    ops, notes, end_floor = [], [], []

    def circles_in(b):
        out = []
        for e in doc["entities"]:
            if e["type"] == "CIRCLE" and e["kind"] == "solid" and not e.get("from_dimension"):
                cx, cy, r = e.get(10, 0.0), e.get(20, 0.0), e.get(40, 0.0)
                if b[0] < cx - r and cx + r < b[2] and b[1] < cy - r and cy + r < b[3] and r >= 0.4:
                    out.append((cx, cy, r))
        # a chamfer drawn as a second circle round a hole: the hole is the smaller one
        return [c for c in out if not any(o is not c and math.hypot(o[0] - c[0], o[1] - c[1]) < 0.05
                                          and o[2] < c[2] for o in out)]

    def back_at_bottom(b):
        """Is the long straight edge across the whole width (the back face) at the box's bottom?"""
        def full(y):
            tot = 0.0
            for e in doc["entities"]:
                if e["kind"] != "solid" or e["type"] not in ("LINE", "LWPOLYLINE"):
                    continue
                for (ax, ay), (bx, by) in dxf._segments(e, 15.0):
                    if abs(ay - y) < 0.1 and abs(by - y) < 0.1 and b[0] - 0.1 <= min(ax, bx) and max(ax, bx) <= b[2] + 0.1:
                        tot += abs(bx - ax)
            return tot
        return full(b[1]) >= full(b[3])
    for i, v in enumerate(views):
        b = v.get("box_mm")
        if i in used or i in skip or not b or v.get("section"):
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if not (abs(w - H) <= 0.02 * H and abs(h - W) <= 0.02 * W):
            continue
        # an end view: horizontal → Z, vertical → Y with the front face (Y = 0) away from the back
        bottom_back = back_at_bottom(b)
        Y = (lambda py: (b[3] - py) * W / h) if bottom_back else (lambda py: (py - b[1]) * W / h)  # noqa: E731
        Z = lambda px: (px - b[0]) * H / w  # noqa: E731
        out = v.get("outline")
        if isinstance(out, dict):
            def fn(p):
                y, z = Y(p[1]), Z(p[0])
                y = next((q + (0.5 if q else -0.5) for q in (0.0, W) if abs(y - q) < 0.05), y)
                z = next((q + (0.5 if q else -0.5) for q in (0.0, H) if abs(z - q) < 0.05), z)
                return y, z
            shapes = kompas_api.arcs_to_lines(dxf.outline_shapes(out, fn)) or dxf.outline_shapes(out, fn)
            # the front-most metal at each Z (the floor this cut leaves), for the pockets to come:
            # an area of the main view lying on that floor is already cut, with its real shape
            poly = [(Y(p[1]), Z(p[0])) for p in out["points"]]
            for k in range(int(H / 0.1) + 1):
                z = k * 0.1
                ys = []
                for (y1, z1), (y2, z2) in zip(poly, poly[1:] + poly[:1]):
                    if (z1 - z) * (z2 - z) <= 0 and z1 != z2:
                        ys.append(y1 + (y2 - y1) * (z - z1) / (z2 - z1))
                if ys:
                    end_floor.append((z, min(ys)))
            ops.append({"op": "cut", "plane": "YZ", "offset": -1, "through_all": True,
                        "shapes": [{"type": "rect", "x": -1, "y": -1, "w": W + 2, "h": H + 2}] + shapes,
                        "comment": f"контур вида {i + 1} с торца"})
            notes.append(f"Вид {i + 1}{' «' + v['label'] + '»' if v.get('label') else ''} — вид с торца: "
                         "всё, что вне его контура, срезано по всей длине")
        cs = circles_in(b)
        if cs:
            ops.append({"op": "cut", "plane": "YZ", "offset": -1, "through_all": True,
                        "shapes": [{"type": "circle", "x": Y(cy), "y": Z(cx), "d": 2 * r * H / w} for cx, cy, r in cs],
                        "comment": f"отверстия вдоль длины с вида {i + 1}"})
            notes.append(f"{len(cs)} отв. вдоль длины по виду {i + 1}: "
                         + ", ".join(f"Ø{2 * r * H / w:.3g}" for _, _, r in cs) + " — насквозь (проверьте)")
    if side_i is not None and side_i not in skip and views[side_i].get("box_mm") and not views[side_i].get("section"):
        # the view beside the main one (from the left, drawn to its right): its circles are holes
        # along X — its right edge is the front face (Y = 0), up is Z
        b = views[side_i]["box_mm"]
        w, h = b[2] - b[0], b[3] - b[1]
        cs = circles_in(b)
        if cs and w > 0 and h > 0:
            ky, kz = W / w, H / h
            ops.append({"op": "cut", "plane": "YZ", "offset": -1, "through_all": True,
                        "shapes": [{"type": "circle", "x": (b[2] - cx) * ky, "y": (cy - b[1]) * kz, "d": 2 * r * ky}
                                   for cx, cy, r in cs],
                        "comment": f"отверстия вдоль длины с вида {side_i + 1}"})
            notes.append(f"{len(cs)} отв. вдоль длины по виду сбоку {side_i + 1}: "
                         + ", ".join(f"Ø{2 * r * ky:.3g}" for _, _, r in cs) + " — насквозь (проверьте)")
    if sil:
        si, flip = sil
        b = views[si]["box_mm"]
        cs = circles_in(b)
        if cs:
            w, h = b[2] - b[0], b[3] - b[1]
            # over the main view (flip) the front face is the view's top edge, under it the bottom one
            Y = (lambda py: (b[3] - py) * W / h) if flip else (lambda py: (py - b[1]) * W / h)  # noqa: E731
            ops.append({"op": "cut", "plane": "XY", "offset": -1, "through_all": True,
                        "shapes": [{"type": "circle", "x": (cx - b[0]) * L / w, "y": Y(cy), "d": 2 * r * L / w}
                                   for cx, cy, r in cs],
                        "comment": f"отверстия поперёк с вида {si + 1}"})
            notes.append(f"{len(cs)} отв. поперёк детали по виду {si + 1}: "
                         + ", ".join(f"Ø{2 * r * L / w:.3g}" for _, _, r in cs) + " — насквозь (проверьте)")
    return {"operations": ops, "notes": notes, "end_floor": end_floor}


def _fine_view(page_png: str, box, index: int, width: int = 7000) -> str | None:
    """The view cut out of the same PDF page rendered ~3× finer (None for pictures uploaded as
    such: there is nothing finer to render)."""
    folder = os.path.dirname(page_png)
    m = re.fullmatch(r"page(\d+)\.png", os.path.basename(page_png))
    pdf = os.path.join(folder, "source.pdf")
    if not m or not os.path.exists(pdf):
        return None
    hi_dir = os.path.join(folder, "fine")
    hi_page = os.path.join(hi_dir, os.path.basename(page_png))
    if not os.path.exists(hi_page):
        _render_pdf(pdf, hi_dir, width)
    with PILImage.open(page_png) as lo, PILImage.open(hi_page) as hi:
        k = hi.width / lo.width
        crop = hi.crop(tuple(int(round(c * k)) for c in box))
        out = os.path.join(folder, f"fine_view{index + 1}_{m.group(1)}.png")
        crop.save(out)
    return out


def _geometry_shaft(a: dict, skip: set) -> None:
    """Vector drawing of a turned part: the body of revolution from the longitudinal section measured
    in the file, then what the plain view shows is not round (teeth, flats) cut away."""
    import numpy as np
    vec, sh = a["vector"], a["vector"]["shaft"]
    n = _vector_numbers(a) or {}
    dims = drawing.without_excluded(a["reading"])["dimensions"]
    vals = [float(a["values"].get(d["id"], d["value"])) for d in dims]
    diam = [float(a["values"].get(d["id"], d["value"])) for d in dims if d.get("axis") == "diameter"]
    lens = [v for v, d in zip(vals, dims) if d.get("axis") not in ("diameter", "radius")]
    L = n.get("length") or sh["length"]
    p = {"z": np.array(sh["z"]), "r_out": np.array(sh["r_out"]), "r_in": np.array(sh["r_in"]),
         "px_per_mm": shaft.PPM}
    try:
        res = revolve.plan(p, L, diam, lens)
    except Exception as e:  # noqa: BLE001
        a["geometry"] = {"error": f"Не удалось снять профиль тела вращения: {e}"}
        return
    D = res["max_diameter"]
    nearD = min(diam, key=lambda v: abs(v - D), default=D)
    D = nearD if abs(nearD - D) <= 0.3 else round(D, 2)
    ops = list(res["operations"])
    notes = [f"Тело вращения: профиль снят с продольного разреза (вид {sh['view'] + 1}) точно по файлу, "
             f"длина {L:g} мм, наибольший Ø{D:g}"]
    if res["bore"]:
        notes.append("Внутри есть полости/отверстия по оси — взяты с разреза")
    pv = shaft.plain_view(None, vec["views"], sh["view"], sh["length"])
    if pv is not None and pv not in skip:
        doc = dxf.read_dxf(os.path.join(os.path.dirname(_upload_path(a["views"][0])), "source.dxf"))
        cut = shaft.outside_cut(doc, vec["views"][pv], D / 2)
        if cut:
            ops.append(cut)
            notes.append(f"Всё, что выходит за контур вида {pv + 1} (зубья, лыски, срезы), срезано насквозь")
        s_ops, s_notes = shaft.slots(doc, vec["views"][pv], sh, D / 2)
        ops += s_ops
        notes += s_notes
    a["geometry"] = {"notes": notes, "questions": [], "issues": []}
    a["reading"]["plan"] = {"operations": ops, "source": "picture"}
    a["reading"]["overall"] = {"length": L, "width": D, "height": D}


def _in_poly(p, poly) -> bool:
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _bevel_teeth(ops: list, metal: list, fn, table: dict, si: int):
    """A bevel gear's teeth (circular or straight, from its table): the toothed rim — the area of
    the section reaching the tip — is turned first, the tooth space cut through sections along the
    tooth line and copied round, then the rest of the blank is added (it gives back whatever the
    space, cut past the teeth's ends, took from it)."""
    import bevel
    # the rim: the area with the section's highest point (the tip at the back end)
    pts = lambda f: [fn(q) for q in f["outline"]["points"]]  # noqa: E731
    k_rim = max(range(len(metal)), key=lambda k: max(y for _, y in pts(metal[k])))
    rim = pts(metal[k_rim])
    i_top = max(range(len(rim)), key=lambda i: (rim[i][1], rim[i][0]))
    x_tip, r_tip = rim[i_top]
    # the tip cone: from the top corner the outline runs down towards the apex (smaller x)
    nb = [rim[(i_top - 1) % len(rim)], rim[(i_top + 1) % len(rim)]]
    front = [q for q in nb if q[0] < x_tip - 0.3]
    tip_angle = math.degrees(math.atan2(r_tip - front[0][1], x_tip - front[0][0])) if front else None
    p = bevel.params(table, tip_angle if tip_angle and 20 < tip_angle < 85 else None)
    ha_e = p["ham"] + (p["Re"] - p["Rm"]) * math.tan(p["theta_a"])
    x_apex = x_tip - p["Re"] * math.cos(p["delta"]) + ha_e * math.sin(p["delta"])
    secs = bevel.sections(p, x_apex)
    rim_op = ops[k_rim]
    rest = [o for k, o in enumerate(ops) if k != k_rim]
    kind = "круговым" if p["circular"] else "прямым"
    new = [rim_op,
           # auto_path: KOMPAS pairs the 4 pieces itself (in order it took a flank for the other
           # one now and then, and the space came out wavy)
           {"op": "loft", "cut": True, "plane": "YZ", "sections": secs, "auto_path": True,
            "comment": f"впадина между зубьями по сечениям (зуб {kind}, β {table.get('beta', 0):g}°)"},
           {"op": "circular", "plane": "YZ", "x": 0.0, "y": 0.0, "count": p["z"],
            "comment": f"впадины: {p['z']} шт. по кругу"}] + rest
    notes = [f"Коническое колесо по таблице: z {p['z']}, mn {p['mn']:g}, δ {math.degrees(p['delta']):g}°, "
             f"зуб {kind}" + (f", β {table.get('beta', 0):g}° {'правое' if p['hand'] == 'right' else 'левое'}, "
                              f"резцовая головка Ø{2 * p['r0']:g}" if p["circular"] else "") +
             f"; Re {p['Re']:g}, ширина венца {p['b']:g}; профиль — эвольвента эквивалентного колеса "
             f"(по Тредгольду), впадина по {len(secs)} сечениям"]
    return new, notes


def _turned_part(dxf_path: str, views: list, skip: set, diameters: list, lengths: list = (),
                 bevel: dict | None = None) -> dict | None:
    """A turned part drawn as a section along its axis (hatched, as tall as the part's biggest
    diameter) beside a view of its face (round: concentric circles, the outer one that diameter) —
    «Колесо»: the hatched areas above the axis are turned round it, then what the face view shows
    inside the bore (a keyway) is cut along the axis. Teeth are not modelled: the toothed rim is
    the body of revolution the section shows."""
    if len(diameters) < 2 or not os.path.exists(dxf_path):
        return None
    doc = dxf.read_dxf(dxf_path)
    circles = [(e.get(10, 0.0), e.get(20, 0.0), e.get(40, 0.0)) for e in doc["entities"]
               if e["type"] == "CIRCLE" and e["kind"] == "solid"]
    rounds = []
    for i, v in enumerate(views):
        b = v.get("box_mm")
        if i in skip or not b or v.get("section"):
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if abs(w - h) > 0.03 * w:
            continue
        if any(math.hypot(x - cx, y - cy) < 0.3 and abs(2 * r - w) < 0.04 * w for x, y, r in circles):
            rounds.append((i, w, cx, cy))
    for ri, D, rcx, rcy in sorted(rounds, key=lambda t: -t[1]):
        for si, v in enumerate(views):
            b = v.get("box_mm")
            if si in skip or not b or not v.get("section") or abs((b[3] - b[1]) - D) > 0.04 * D:
                continue
            axis = (b[1] + b[3]) / 2
            fs = dxf.faces(doc, b)
            hs = dxf.hatch_segments(doc, b)
            mids = [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in hs]
            upper = [f for f in fs if f["box"][1] >= axis - 0.1]
            # a solid part (no bore): its metal is one area across the axis — the half above it,
            # clipped at the axis (as a polyline: the arcs of that area are lost to 2° chords there)
            crossing = []
            for f in fs:
                if f["box"][1] < axis - 0.1 < axis + 0.1 < f["box"][3] and \
                        sum(_in_poly(m, f["outline"]["points"]) for m in mids) >= 3:
                    pts = f["outline"]["points"]
                    clip = []
                    for p, q in zip(pts, pts[1:] + pts[:1]):
                        if p[1] >= axis:
                            clip.append(p)
                        if (p[1] - axis) * (q[1] - axis) < 0:
                            t = (axis - p[1]) / (q[1] - p[1])
                            clip.append((p[0] + t * (q[0] - p[0]), axis))
                    if len(clip) >= 3:
                        crossing.append(clip)
            if not crossing and not any(sum(_in_poly(m, f["outline"]["points"]) for m in mids) >= 3 for f in upper):
                continue
            # every area above the axis is metal: a gear's teeth are left unhatched in a section
            # (ESKD), the bore crosses the axis and is no area of the upper half
            metal = upper
            Lx = b[2] - b[0]
            # the drawn sizes as dimensioned: radii onto the Ø written (36.32 → Ø72.7 / 2), the ends
            # onto the overall length (45.212 → 45.2)
            radii = sorted({d / 2 for d in diameters})
            L_dim = min(lengths, key=lambda v: abs(v - Lx), default=None)
            L_true = L_dim if L_dim is not None and abs(L_dim - Lx) <= 0.06 else Lx
            ax_line = {"type": "line", "x1": -2, "y1": 0, "x2": L_true + 2, "y2": 0, "style": 3}
            ops = []

            def fn(p):
                x, y = p[0] - b[0], p[1] - axis
                y = 0.0 if abs(y) < 0.05 else next((r for r in radii if abs(r - y) <= 0.06), y)
                x = 0.0 if abs(x) < 0.06 else (L_true if abs(x - Lx) < 0.06 else x)
                return x, y
            for f in metal:
                ops.append({"op": "revolve", "plane": "XY", "angle": 360,
                            "shapes": dxf.outline_shapes(f["outline"], fn) + [ax_line],
                            "comment": f"тело вращения по разрезу (вид {si + 1}), область X "
                                       f"{f['box'][0] - b[0]:.1f}…{f['box'][2] - b[0]:.1f}"})
            teeth_notes = []
            if bevel and metal:
                try:
                    ops, teeth_notes = _bevel_teeth(ops, metal, fn, bevel, si)
                except Exception as e:  # noqa: BLE001 — the blank stays
                    teeth_notes = [f"Зубья конического колеса не построены: {e}"]
            for clip in crossing:
                # after the teeth (their space, cut past the ends, must not take from it), else first
                ops.insert(3 if teeth_notes and ops and ops[1].get("op") == "loft" else 0,
                           {"op": "revolve", "plane": "XY", "angle": 360,
                               "shapes": [{"type": "polyline", "closed": True,
                                           "points": [list(fn(p)) for p in clip]}, ax_line],
                               "comment": f"тело вращения по разрезу (вид {si + 1}): сплошная часть над осью"})
            notes = [f"Тело вращения: разрез вдоль оси — вид {si + 1}, торец — вид {ri + 1} "
                     f"(Ø{D:.4g}); по разрезу повернуто {len(metal) + len(crossing)} обл."] + teeth_notes
            # the bore as the face view shows it (a keyway beside the round hole): cut along the axis
            rb = views[ri]["box_mm"]
            inner = [f for f in dxf.faces(doc, rb) if not f["body"] and _in_poly((rcx, rcy), f["outline"]["points"])
                     and f["area"] < 0.5 * math.pi * D * D / 4]
            if inner:
                # the innermost: smallest by its box (a chamfer's thin ring round the bore has the
                # smaller area but encloses the bore)
                f = min(inner, key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]))
                pts = f["outline"]["points"]
                rmax = max(math.hypot(x - rcx, y - rcy) for x, y in pts)
                rmin = min(math.hypot(x - rcx, y - rcy) for x, y in pts)
                if rmax - rmin > 0.3:   # not a plain round bore: the section has that already
                    ops.append({"op": "cut", "plane": "YZ", "offset": -1, "through_all": True,
                                "shapes": dxf.outline_shapes(f["outline"], lambda p: (p[0] - rcx, p[1] - rcy)),
                                "comment": f"отверстие со шпоночным пазом по виду {ri + 1}"})
                    notes.append(f"Отверстие по виду {ri + 1} не круглое (шпоночный паз) — прорезано вдоль оси")
            return {"operations": ops, "notes": notes, "length": round(L_true, 3), "diameter": D,
                    "views": [si, ri]}
    return None


def _geometry_revolve(a: dict) -> None:
    """A body of revolution from its longitudinal section: the profile above the axis is traced on
    the picture, scaled by the overall length and snapped to the diameters and lengths read."""
    rd, vals = drawing.without_excluded(a["reading"]), a["values"]
    val = lambda d: vals.get(d["id"], d.get("value"))  # noqa: E731
    dims = [d for d in rd.get("dimensions", []) if isinstance(val(d), (int, float))]
    diam = [float(val(d)) for d in dims if d.get("axis") == "diameter"
            or "Ø" in str(d.get("meaning", "")) or "диам" in str(d.get("meaning", "")).lower()]
    lens = [float(val(d)) for d in dims if float(val(d)) not in diam and d.get("axis") != "radius"]
    over = [float(val(d)) for d in dims if d.get("overall") and d.get("axis") == "length" and float(val(d)) > 0]
    L = max(over) if over else (max(lens) if lens else None)
    if not L:
        a["geometry"] = {"error": "Не найдена общая длина детали вдоль оси: добавьте её в таблицу и отметьте габаритом"}
        return
    path = a["revolve"].get("path") or _upload_path(a["views"][a["revolve"]["view"]])
    try:
        p = revolve.profile(path, L)
        res = revolve.plan(p, L, diam, lens)
    except Exception as e:  # noqa: BLE001
        a["geometry"] = {"error": f"Не удалось снять профиль тела вращения: {e}"}
        return
    D = res["max_diameter"]
    nearD = min(diam, key=lambda v: abs(v - D), default=D)
    D = nearD if abs(nearD - D) <= 0.3 else round(D, 2)
    ops = res["operations"]
    notes = [f"Тело вращения: профиль снят с продольного разреза (вид {a['revolve']['view'] + 1}), "
             f"длина {L:g} мм, наибольший Ø{D:g}"]
    if res["bore"]:
        ds = sorted({round(2 * r, 2) for _, r in res["bore"] if r > 0})
        notes.append("Внутри — сквозная расточка: Ø" + ", Ø".join(f"{v:g}" for v in ds))
    # flats «под ключ» named in the notes the model read (or the user's hint)
    text = " ".join([a.get("note", "")] + [str(f) for f in rd.get("features", [])] +
                    [str(d.get("meaning", "")) for d in dims])
    for fl in revolve.flats(text):
        cuts = revolve.flat_cuts(res["profile"], fl)
        if cuts:
            ops += cuts
            notes.append(f"{fl['n']} лыски S{fl['s']:g} на Ø{fl['d']:g}")
    notes.append("Резьбы построены по наружному диаметру (условно, без витков)")
    a["geometry"] = {"notes": notes, "questions": [], "issues": []}
    R = a["reading"]
    R["plan"] = {"operations": ops, "source": "picture"}
    R["overall"] = {"length": L, "width": D, "height": D}
    main = a["revolve"]["view"] + 1
    R["views"] = [v for v in R.get("views", []) if v.get("image") != main and v.get("name") != "главный"] + \
        [{"image": main, "name": "главный"}]


def _geometry(a: dict) -> None:
    """Shape from the pictures (vision.reconstruct), numbers from the confirmed dimensions."""
    dxf.SNAP_RADII, dxf.SNAPPED = [], {}
    skip = drawing.excluded_views(a["reading"])
    if a.get("steel"):
        st = a["steel"]
        if skip:  # views the user excluded add no cuts or holes
            st = {**st, "views": [v for i, v in enumerate(st["views"]) if i not in skip]}
        try:
            res = steel.plan(st, a["values"], a.get("answers", {}))
        except Exception as e:  # noqa: BLE001
            a["geometry"] = {"error": f"Не удалось составить деталь по чертежу металлоконструкции: {e}"}
            return
        a["geometry"] = {"notes": res["notes"], "questions": res["questions"], "issues": res["issues"]}
        a["reading"]["plan"] = {"operations": res["operations"], "source": "picture"}
        return
    if a.get("profile"):
        pf = a["profile"]
        try:
            res = profile_raster.plan(pf["path"], lines=pf["lines"], length=a["values"].get("L"))
        except Exception as e:  # noqa: BLE001
            res = None
            print("profile part:", e, flush=True)
        if not res:
            a["geometry"] = {"error": "Не удалось составить деталь из проката по чертежу"}
            return
        a["geometry"] = {"notes": res["notes"], "questions": res["questions"], "issues": []}
        a["reading"]["plan"] = {"operations": res["operations"], "source": "picture"}
        a["marks"], a["sheet_size"] = res["marks"], res["size"]
        return
    if a.get("revolve") and a["revolve"]["view"] not in skip:
        _geometry_revolve(a)
        return
    vector = a.get("vector")
    if vector and vector.get("shaft") and vector["shaft"]["view"] not in skip:
        _geometry_shaft(a, skip)
        return
    if vector and vector.get("views") and a.get("views"):
        src = os.path.join(os.path.dirname(_upload_path(a["views"][0])), "source.dxf")
        try:
            vn0 = vector.get("numbers") or {}
            tp = _turned_part(src, vector["views"], skip, vn0.get("diameters", []), vn0.get("values", []),
                              vector.get("bevel"))
        except Exception as e:  # noqa: BLE001
            tp = None
            print("turned part:", e, flush=True)
        if tp:
            D = min((vector.get("numbers") or {}).get("diameters", []), key=lambda v: abs(v - tp["diameter"]),
                    default=tp["diameter"])
            D = D if abs(D - tp["diameter"]) <= 0.4 else round(tp["diameter"], 2)
            toothed = any(o.get("op") == "circular" for o in tp["operations"])
            a["geometry"] = {"notes": tp["notes"] + ([] if toothed else
                                                     ["Зубья (если есть) не построены: венец — тело вращения по разрезу"]),
                             "questions": [], "issues": []}
            a["reading"]["plan"] = {"operations": tp["operations"], "source": "picture"}
            a["reading"]["overall"] = {"length": tp["length"], "width": D, "height": D}
            # the overall sizes in the table: the length along the axis and the biggest Ø both ways
            for d in a["reading"]["dimensions"]:
                if d.get("overall") and d.get("axis") in ("length", "width", "height"):
                    v = tp["length"] if d["axis"] == "length" else D
                    d["value"] = v
                    a["values"][d["id"]] = v
            return
    numbers = _vector_numbers(a) if vector else \
        drawing.numbers_from_dims(drawing.without_excluded(a["reading"]), a["values"])
    # arcs get the radii written on the drawing (R and Ø/2), fillets become exactly tangent
    vn = (vector or {}).get("numbers") or {}
    dxf.SNAP_RADII = sorted({float(r) for r in vn.get("radii", [])} | {float(d) / 2 for d in vn.get("diameters", [])})
    dxf.SNAPPED = {}
    names = {v.get("image"): v.get("name") for v in a["reading"].get("views", [])}
    lay = _vector_layout(vector, skip) if vector else None
    sil = lay.get("silhouette") if lay else None
    if lay:
        # vector: the views as they lie on the sheet, unless the user renamed them in the window
        by_name = {n: i - 1 for i, n in names.items() if i is not None}
        if "главный" in by_name:
            # the views are named (by the layout when the file was read, then by the user): a view
            # named «другое» or excluded is not a projection — nothing is filled in from the layout
            lay = {"front": by_name["главный"], "top": by_name.get("сверху"), "side": by_name.get("слева")}
        else:
            lay = {"front": lay["front"], "top": by_name.get("сверху", lay["top"]),
                   "side": by_name.get("слева", lay["side"])}
        pair = (lay["front"], lay["top"]) if lay["top"] is not None else (lay["front"], None)
        side_i = lay["side"]
    else:
        pair = drawing.pick_views(a["reading"], len(a["views"]), a.get("boxes"))
    if pair is None or (pair[1] is None and not lay):
        a["geometry"] = {"error": "Не найдены главный вид и вид сверху: укажите их под картинками видов"}
        return
    if numbers is None:
        a["geometry"] = {"error": "Нет габаритных размеров по всем трём осям: отметьте их в таблице"}
        return
    if not lay:
        if names.get(pair[0] + 1) != "главный" or names.get(pair[1] + 1) != "сверху":
            # the model named the views wrongly (or not at all): label them as they are used
            names.update({pair[0] + 1: "главный", pair[1] + 1: "сверху"})
            a["reading"]["views"] = [{"image": i, "name": n} for i, n in sorted(names.items()) if i is not None]
        side_i = drawing.pick_side(a["reading"], len(a["views"]), a.get("boxes"), pair[0])
        if side_i is not None and side_i == pair[1]:
            side_i = None
        if side_i is not None and names.get(side_i + 1) != "слева":
            names[side_i + 1] = "слева"
            a["reading"]["views"] = [{"image": i, "name": n} for i, n in sorted(names.items()) if i is not None]
    front = _upload_path(a["views"][pair[0]])
    side = _upload_path(a["views"][side_i]) if side_i is not None else None
    if pair[1] is not None:
        top = _upload_path(a["views"][pair[1]])
    else:
        # main + left view only: the top view of what they bound is a plain L × W rectangle at first;
        # the left view's own outline is then cut through (intersection of the views)
        top = os.path.join(os.path.dirname(front), f"synthetic_top_{a['id']}.png")
        ppm, m = vector["px_per_mm"], 32
        im = PILImage.new("L", (int(numbers["length"] * ppm) + 2 * m, int(numbers["width"] * ppm) + 2 * m), 255)
        from PIL import ImageDraw
        ImageDraw.Draw(im).rectangle([m, m, m + numbers["length"] * ppm, m + numbers["width"] * ppm],
                                     outline=0, width=max(2, int(ppm / 2)))
        im.save(top)
    construction = os.path.join(os.path.dirname(front), f"construction_{a['id']}.png")
    # the views are drawn to scale: their proportions check the overall sizes the model read
    extra_notes, size_issues = [], []
    try:
        est = {} if vector else vision.estimate_sizes(front, top, numbers["length"])  # vector: sizes are exact
    except Exception:  # noqa: BLE001
        est = {}
    # task drawings are only roughly to scale, so this is a coarse check (12 %)
    tol = 0.12
    for axis, label in (("height", "высота"), ("width", "ширина")):
        e = est.get(axis)
        if not e or abs(numbers[axis] - e) <= tol * e:
            continue
        # suggest, never substitute: the proportions are only approximate
        read = sorted({float(a["values"].get(d["id"], d.get("value"))) for d in a["reading"].get("dimensions", [])
                       if isinstance(a["values"].get(d["id"], d.get("value")), (int, float))
                       and abs(float(a["values"].get(d["id"], d.get("value"))) - e) <= tol * e})
        hint = (f"Подходят прочитанные: {', '.join(f'{v:g}' for v in read)} — отметьте нужный как габарит."
                if read else "Похоже, этот размер не прочитан — добавьте его в таблицу.")
        size_issues.append({"level": "error", "dims": [],
                            "text": f"По пропорциям видов габаритная {label} ≈{e:.0f} мм, а отмечена {numbers[axis]:g}. {hint}"})
    vv = vector.get("views") or [] if vector else []
    is_section = lambda i: bool(i is not None and i < len(vv) and vv[i].get("section"))  # noqa: E731
    views_for_outline = vv
    top_section = is_section(pair[1])
    if top_section and sil is not None and sil[0] not in skip and pair[0] == (lay or {}).get("front"):
        # the section shows the part only where it is cut: its outline comes from the plain view
        # lined up with the main one (the view from below is turned over to read as from above)
        si, flip = sil
        top = _upload_path(a["views"][si])
        if flip:
            flipped = os.path.join(os.path.dirname(top), f"flipped_{os.path.basename(top)}")
            with PILImage.open(top) as im:
                im.transpose(PILImage.FLIP_TOP_BOTTOM).save(flipped)
            top = flipped
        sv = dict(vv[si])
        if flip and isinstance(sv.get("outline"), dict):
            b = sv["box_mm"]
            o = sv["outline"]
            fy = lambda p: (p[0], b[1] + b[3] - p[1])  # noqa: E731
            sv["outline"] = {"points": [list(fy(p)) for p in o["points"]],
                             "arcs": [[[t[0], b[1] + b[3] - t[1], t[2]] for t in ts] for ts in o.get("arcs", [])]}
            if o.get("segs"):   # turned over: arcs run the other way round
                sv["outline"]["segs"] = [dict(s, a=fy(s["a"]), b=fy(s["b"]), **({"c": fy(s["c"]), "ccw": not s["ccw"]}
                                                                               if s["type"] == "arc" else {}))
                                         for s in o["segs"]]
        views_for_outline = list(vv)
        views_for_outline[pair[1]] = sv
        top_section = False
        extra_notes.append(f"Силуэт сбоку взят с вида {si + 1}, глубины — с разреза (вид {pair[1] + 1})")
    try:
        res = vision.reconstruct(front, top, numbers, answers=a.get("answers", {}), side=side,
                                 construction_png=construction, upscale=1 if vector else 4, exact=bool(vector),
                                 top_section=top_section, side_section=is_section(side_i))
    except Exception as e:  # noqa: BLE001
        a["geometry"] = {"error": f"Не удалось снять геометрию с видов: {e}"}
        return
    if vector and vector.get("views"):
        extra_notes += _exact_outlines(res["operations"], views_for_outline, pair, side_i, numbers)
        src_dxf = os.path.join(os.path.dirname(front), "source.dxf")
        try:
            # the gear's table: from the PDF's text, or as the language model read it from the sheet
            extra_notes += _helical_gear(res["operations"], src_dxf, vector["views"], side_i, numbers,
                                         a.get("gear_table") or vector.get("gear"))
        except Exception as e:  # noqa: BLE001
            extra_notes.append(f"Зубья не разобраны: {e}")
        end_floor = None
        if os.path.exists(src_dxf):
            try:
                used = {pair[0], pair[1], side_i} | ({sil[0]} if sil else set())
                ev = _end_views(src_dxf, vector["views"], used - {None}, skip, numbers, sil, side_i)
                end_floor = ev.get("end_floor")
                res["operations"] += ev["operations"]
                extra_notes += ev["notes"]
                if sil and sil[0] not in skip:
                    sp = _silhouette_pockets(src_dxf, vector["views"], pair[0], sil, numbers)
                    res["operations"] += sp["operations"]
                    extra_notes += sp["notes"]
            except Exception as e:  # noqa: BLE001
                extra_notes.append(f"Виды с торца не разобраны: {e}")
        if os.path.exists(src_dxf) and (pair[1] is None or side_i is not None or is_section(pair[1])) and \
                (side_i is None or vector["views"][side_i].get("box_mm")):
            try:
                pk = _pockets(src_dxf, vector["views"], pair[0], side_i, numbers, a.get("answers", {}), pair[1],
                              end_floor=end_floor)
                res["operations"] += pk["operations"]
                _gear_hubs(res["operations"])
                extra_notes += pk["notes"]
                res["questions"] += pk["questions"]
                # the back face: a plain view of the same size as the main one, not in projection
                used = {pair[0], pair[1], side_i} | ({sil[0]} if sil else set())
                bk = _back_view(src_dxf, vector["views"], pair[0], used - {None}, skip, sil, numbers["width"])
                if bk:
                    pb = _pockets(src_dxf, vector["views"], pair[0], side_i, numbers, a.get("answers", {}), pair[1],
                                  back=bk)
                    # back cuts go before the front's fillets
                    late = lambda o: o.get("op") == "fillet" and not o.get("edge_round")  # noqa: E731
                    fil = [o for o in res["operations"] if late(o)]
                    res["operations"] = [o for o in res["operations"] if not late(o)] + pb["operations"] + fil
                    extra_notes += [f"Вид {bk['view'] + 1} — тыльная сторона" + (" (зеркально)" if bk["mirror"] else "")]
                    extra_notes += pb["notes"]
                    res["questions"] += pb["questions"]
            except Exception as e:  # noqa: BLE001
                extra_notes.append(f"Полости по виду слева не разобраны: {e}")
    _gear_hubs(res["operations"])   # a gear with no areas read: its teeth all the same
    if dxf.SNAPPED:
        rs = sorted((k, v) for k, v in dxf.SNAPPED.items() if k != "касание")
        if rs:
            extra_notes.append("Радиусы дуг взяты с размеров чертежа: " +
                               ", ".join(f"R{k:g} ×{v}" for k, v in rs))
        if dxf.SNAPPED.get("касание"):
            extra_notes.append(f"Скругления между прямыми сделаны точно касательными: {dxf.SNAPPED['касание']}")
    uid = os.path.basename(os.path.dirname(construction))
    a["geometry"] = {"notes": extra_notes + res["notes"], "questions": res["questions"], "issues": size_issues,
                     "construction": f"/uploads/{uid}/{os.path.basename(construction)}?t={int(time.time())}"}
    a["reading"]["plan"] = {"operations": res["operations"], "source": "picture"}
    a["reading"]["overall"] = {"length": numbers["length"], "width": numbers["width"], "height": numbers["height"]}


def _mark_excluded(reading: dict, exclude: list) -> dict:
    """Puts back the user's «исключить из расчёта» marks (1-based view numbers) after a re-read."""
    ex = {int(i) for i in exclude or [] if str(i).isdigit()}
    if ex:
        reading["views"] = [v for v in reading.get("views", []) if v.get("image") not in ex] + \
            [{"image": i, "name": "исключить"} for i in sorted(ex)]
    return reading


async def _crosscheck(a: dict, src: str, note: str, settings: dict, skip0: set):
    """The model reads the drawing itself (with the user's comment and the program's summary) and
    names what it thinks the program got wrong; its reading is compared with the program's. The
    result goes into the review window as remarks (a["crosscheck"])."""
    g = a.get("geometry", {})
    numbers = _vector_numbers(a) if a.get("vector") else None
    summary = drawing.program_summary(numbers or {}, drawing.without_excluded(a["reading"]), a["values"],
                                      g.get("notes", []))
    crops = [(k + 1, _scaled_part(_upload_path(u))["url"]) for k, u in enumerate(a["views"]) if k not in skip0]
    sheet = _scaled_part(src, 1280)["url"]
    # the pictures of a vector drawing are drawn from its lines only, without the numbers: the model
    # gets the PDF page itself, rendered, and the views cut out of it
    pdf = os.path.join(os.path.dirname(src), "source.pdf")
    if os.path.exists(pdf):
        try:
            out = os.path.join(os.path.dirname(src), "crosscheck")
            page = os.path.join(out, "page1.png")
            if not os.path.exists(page):
                await anyio.to_thread.run_sync(_render_pdf, pdf, out, 3200)
            # the page in four overlapping quarters, so the small digits stay legible, and whole
            with PILImage.open(page) as hi:
                W, H = hi.size
                crops = []
                for n, (fx, fy) in enumerate(((0, 0), (0.45, 0), (0, 0.45), (0.45, 0.45)), 1):
                    c = hi.crop((int(fx * W), int(fy * H), int((fx + 0.55) * W), int((fy + 0.55) * H)))
                    path = os.path.join(out, f"quarter{n}.png")
                    c.save(path)
                    crops.append((n, _scaled_part(path, 1400)["url"]))
            sheet = _scaled_part(page, 1600)["url"]
            quarters = True
        except Exception as e:  # noqa: BLE001 — the line pictures are shown then
            print("crosscheck: PDF page not rendered:", e, flush=True)
            quarters = False
    else:
        quarters = False
    told = note + ("\n(Изображения 1–4 — четверти листа с перекрытием, чтобы цифры читались; "
                   "в конце — весь лист. Поле views можно оставить пустым.)" if quarters else "")
    msgs = drawing.reading_messages(crops, sheet, told, prompt=drawing.cross_prompt(summary))
    yield _ev("stage", text=f"Модель ИИ {settings['model']} читает чертёж для сверки с программой…")
    fmt = {"type": "json_schema", "json_schema": {"name": "crosscheck", "schema": drawing.CROSS_SCHEMA}}
    reading, err = None, ""
    for attempt, rf in enumerate((fmt, None), 1):
        msg = None
        try:
            async for kind, data in llm_stream(settings, msgs, None, rf, max_reasoning_chars=25000):
                if kind == "progress":
                    yield _ev("progress", step=attempt, **data)
                else:
                    msg = data
        except Exception as e:  # noqa: BLE001
            err = str(e)
            if re.search(r"context|остановлено|рассуждает", err, re.I):
                break
            continue
        for text in (msg["content"], msg.get("reasoning", "")):
            try:
                reading = drawing.parse_reading(text)
                break
            except ValueError:
                continue
        log_call("crosscheck_raw", json.dumps({"id": a["id"], "attempt": attempt}),
                 (msg["content"] or "")[-1500:], reading is None)
        if reading is not None:
            break
        err = "модель не вернула JSON"
    if reading is None:
        a["crosscheck"] = {"error": err, "issues": [{"level": "warning", "dims": [],
                                                     "text": f"Сверка с моделью ИИ не выполнена: {err}"}]}
        return
    yield _ev("stage", text="Сравниваю чтение модели с распознаванием программы…")
    # the gear's table as the model read it from the sheet (it is not text in every PDF): the teeth are
    # built from it — the plan is made again with it
    gt = reading.get("gear") if isinstance(reading.get("gear"), dict) else {}
    try:
        gt = {k: (float(v) if k != "hand" else str(v).lower()) for k, v in gt.items() if v not in (None, "")}
    except (TypeError, ValueError):
        gt = {}
    if gt.get("hand"):
        gt["hand"] = "right" if gt["hand"].startswith(("r", "п")) else "left" if gt["hand"].startswith(("l", "л")) else None
    if gt.get("m") and gt.get("z") and 0.01 < gt["m"] < 50 and 4 <= gt["z"] <= 500 and 0 <= gt.get("beta", 0) <= 45:
        a["gear_table"] = gt
        yield _ev("stage", text="Модель прочитала таблицу зубчатого колеса — пересобираю план с ней…")
        await anyio.to_thread.run_sync(_geometry, a)
    a["crosscheck"] = {"model": settings["model"], "reading": reading, "features": reading.get("features", []),
                       "issues": drawing.compare_readings(numbers or {}, drawing.without_excluded(a["reading"]),
                                                          a["values"], reading)}
    log_call("crosscheck", json.dumps({"id": a["id"], "note": note}), json.dumps(a["crosscheck"], ensure_ascii=False), False)


async def drawing_analyze(request: Request):
    body = await request.json()
    return StreamingResponse(_guarded(_analyze_events(body), body), media_type="application/x-ndjson")


async def _analyze_events(body: dict):
    """The analysis of one drawing as a stream of events (stage, progress, error, analysis).
    body: {image, note, exclude, ai, batch} — batch: no language model (a drawing that needs one
    is reported, not read)."""
    exclude = [int(i) for i in body.get("exclude") or [] if str(i).isdigit()]
    skip0 = {i - 1 for i in exclude}
    global stop_requested
    stop_requested = False
    settings = _drawing_settings()
    try:
        src = _upload_path(body["image"])
    except (KeyError, ValueError) as e:
        yield _ev("error", text=str(e))
        return
    folder = os.path.dirname(src)
    uid = os.path.basename(folder)
    stem = os.path.splitext(os.path.basename(src))[0]
    await anyio.to_thread.run_sync(_refresh_vector, folder)
    vec = _vector_meta(src)
    if vec and vec.get("steel"):
        # steel detail: profile from the specification, cuts and holes from the views — no model
        st = vec["steel"]
        aid = uuid.uuid4().hex[:12]
        reading = _mark_excluded(steel.reading(st), exclude)
        a = {"id": aid, "views": [f"/uploads/{uid}/{f}" for f in vec["steel_views"]], "boxes": None,
             "reading": reading, "values": {d["id"]: d["value"] for d in reading["dimensions"]},
             "vector": vec, "steel": st}
        analyses[aid] = a
        a["sheet"] = body["image"]
        prof = st["spec"]["profile"]
        yield _ev("stage", text=f"Чертёж металлоконструкции: {prof['name']}, размеры из спецификации и видов…")
        await anyio.to_thread.run_sync(_geometry, a)
        yield _ev("analysis", **_analysis_view(a))
        return
    yield _ev("stage", text="Вырезаю виды с листа…")
    if vec and vec.get("views"):
        # vector: the views were found in the geometry itself when the file was loaded
        views = [{"url": f"/uploads/{uid}/{v['file']}", "box": v["box"]} for v in vec["views"]]
    else:
        views = await anyio.to_thread.run_sync(drawing.segment_views, src, folder)
        for v in views:  # unique names per source page
            new = f"{stem}_{v['file']}"
            os.replace(os.path.join(folder, v["file"]), os.path.join(folder, new))
            v["url"] = f"/uploads/{uid}/{new}"
    if not vec:
        # a part from a rolled profile (the title block names it: «Труба 50х50х3 ГОСТ 8639-82»):
        # the section is the profile, the length, ends, holes and slots come from the views
        yield _ev("stage", text="Читаю штамп и размеры на листе…")
        words = await anyio.to_thread.run_sync(profile_raster.ocr, src)
        lines = profile_raster.lines_of(words)
        try:
            res = await anyio.to_thread.run_sync(lambda: profile_raster.plan(src, lines=lines, ocr_words=words))
        except Exception as e:  # noqa: BLE001
            res = None
            print("profile part:", e, flush=True)
        if res:
            aid = uuid.uuid4().hex[:12]
            reading = _mark_excluded(profile_raster.reading(res), exclude)
            # the long views are faces of the part: the first one is the main view, the next the top
            longs = [tuple(c) for c in res["longs"]]
            names = {}
            for i, v in enumerate(views):
                core = tuple(v.get("core") or ())
                if core in longs and longs.index(core) < 2:
                    names[i + 1] = ("главный", "сверху")[longs.index(core)]
            reading["views"] = [{"image": i, "name": n} for i, n in sorted(names.items())]
            if res.get("mass"):
                reading["mass_kg"] = res["mass"]
            a = {"id": aid, "views": [v["url"] for v in views], "boxes": [v["box"] for v in views],
                 "cores": [v.get("core") for v in views],
                 "reading": reading, "values": {d["id"]: d["value"] for d in reading["dimensions"]},
                 "profile": {"path": src, "lines": lines}}
            analyses[aid] = a
            a["sheet"] = body["image"]
            yield _ev("stage", text=f"Деталь из проката: {res['stock']['name']}, длина {res['length']:g} мм. "
                                    "Снимаю торцы, отверстия и пазы с видов…")
            await anyio.to_thread.run_sync(_geometry, a)
            yield _ev("analysis", **_analysis_view(a))
            return
    if vec:
        # vector drawing: the numbers are text in the file and the scale is exact — no model needed
        aid = uuid.uuid4().hex[:12]
        boxes = [v["box"] for v in views]
        reading = _mark_excluded(_vector_reading(vec, [_upload_path(v["url"]) for v in views], boxes, skip0),
                                 exclude)
        a = {"id": aid, "views": [v["url"] for v in views], "boxes": boxes, "reading": reading,
             "values": {d["id"]: d["value"] for d in reading["dimensions"]}, "vector": vec}
        analyses[aid] = a
        a["sheet"] = body["image"]
        yield _ev("stage", text="Векторный чертёж: размеры взяты из файла. Снимаю геометрию с видов…")
        await anyio.to_thread.run_sync(_geometry, a)
        note = (body.get("note") or "").strip()
        if body.get("ai") and not note:
            note = "Распознай чертёж полностью: какая это деталь, все размеры, отверстия, пазы, сечения."
        if note:
            # the user wrote a comment: the language model reads the drawing too, sees what the
            # program found, and the two readings are compared before anything is built
            async for ev in _crosscheck(a, src, note, settings, skip0):
                yield ev
        yield _ev("analysis", **_analysis_view(a))
        return
    if body.get("batch"):
        yield _ev("error", text="Размеры этого чертежа читает модель ИИ: откройте его в окне проверки по одному")
        return
    # a body of revolution drawn as one longitudinal section: axis + hatched metal on both sides
    rev = None
    by_area = sorted(range(len(views)), key=lambda i: -(views[i]["box"][2] - views[i]["box"][0]) *
                     (views[i]["box"][3] - views[i]["box"][1]))
    for i in [j for j in by_area if j not in skip0][:3]:
        vpath = _upload_path(views[i]["url"])
        if await anyio.to_thread.run_sync(revolve.has_axis, vpath):
            # telling the outline from the hatching needs more pixels than the page was rendered
            # with: a PDF page is rendered again, finely, and the view cut out of it
            fine = await anyio.to_thread.run_sync(_fine_view, src, views[i]["box"], i)
            det = await anyio.to_thread.run_sync(revolve.detect, fine or vpath)
            if det:
                rev = {"view": i, "path": fine or vpath, **det}
                break
    note = body.get("note", "")
    if rev:
        yield _ev("stage", text="Похоже на тело вращения: продольный разрез с осью. Профиль возьму с разреза…")
        note = (note + " " if note else "") + ("Деталь — тело вращения, изображение " + str(rev["view"] + 1) +
                                              " — продольный разрез: Ø — диаметры (axis=diameter), длины вдоль оси — "
                                              "axis=length, общая длина детали — overall.")
    # views the user excluded are not shown to the model at all; the others keep their numbers
    crops = [(k + 1, _scaled_part(_upload_path(v["url"]))["url"]) for k, v in enumerate(views) if k not in skip0]
    # the whole sheet only when no views were found: every picture costs context in small models
    sheet = _scaled_part(src, 1024)["url"] if not views else None
    msgs = drawing.reading_messages(crops, sheet, note)
    shown = f"Видов найдено: {len(views)}" + (f", исключено вами: {len(skip0 & set(range(len(views))))}" if skip0 else "")
    yield _ev("stage", text=f"{shown}. Модель {settings['model']} читает размеры…")
    msg = None
    fmt = {"type": "json_schema", "json_schema": {"name": "drawing", "schema": drawing.READ_DIMS_SCHEMA}}
    reading, last_error = None, ""
    # 1st try: strict JSON schema. Reasoning models (Nemotron) sometimes leave the answer empty or
    # put the JSON into their reasoning under a schema — so the reasoning is searched too, and
    # a 2nd try asks for plain JSON text without the schema.
    for attempt, rf in enumerate((fmt, None), 1):
        msg = None
        try:
            # reading numbers needs little thought: cap runaway reasoning (~7 000 tokens)
            async for kind, data in llm_stream(settings, msgs, None, rf, max_reasoning_chars=25000):
                if kind == "progress":
                    yield _ev("progress", step=attempt, **data)
                else:
                    msg = data
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            if re.search(r"context|остановлено|рассуждает", last_error, re.I):
                break
            continue
        for text in (msg["content"], msg.get("reasoning", "")):
            try:
                reading = drawing.parse_reading(text)
                break
            except ValueError:
                continue
        log_call("read_drawing_raw", json.dumps({"image": body["image"], "attempt": attempt}),
                 (msg["content"] or "")[-1500:] + " | REASONING: " + (msg.get("reasoning") or "")[-500:],
                 reading is None)
        if reading is not None:
            break
        last_error = "модель не вернула JSON"
        yield _ev("stage", text="Модель ответила не в нужном формате, прошу ещё раз…")
    if reading is None and rev:
        # the shape comes from the picture anyway: without the model only the overall length is
        # missing, and the user can type it
        yield _ev("stage", text=f"Модель не прочитала размеры ({last_error}). Форма тела вращения снята с "
                                "разреза — впишите общую длину детали в таблицу.")
        reading = {"views": [], "features": [], "overall": {}, "dimensions": [
            {"id": "L", "value": 0, "view": "", "meaning": "общая длина детали вдоль оси (впишите)",
             "axis": "length", "overall": True, "confidence": 0.0}]}
    if reading is None:
        hint = (" Модель загружена в LM Studio с маленьким контекстом: увеличьте Context Length "
                "(16384 и больше) и повторите." if re.search(r"context", last_error, re.I) else
                " Выберите в Настройках → «Модель для разбора чертежей» быструю модель с поддержкой "
                "изображений, например google/gemma-4-e4b: она читает чертёж за 1–2 минуты.")
        yield _ev("error", text=f"Не удалось прочитать чертёж: {last_error}.{hint}")
        return
    aid = uuid.uuid4().hex[:12]
    reading = _mark_excluded(reading, exclude)
    a = {"id": aid, "views": [v["url"] for v in views], "boxes": [v["box"] for v in views], "reading": reading,
         "values": {d["id"]: d["value"] for d in reading.get("dimensions", [])}, "revolve": rev,
         "note": note}
    analyses[aid] = a
    a["sheet"] = body["image"]
    log_call("read_drawing", json.dumps({"image": body["image"]}), json.dumps(reading, ensure_ascii=False), False)
    yield _ev("stage", text="Снимаю геометрию с видов…")
    await anyio.to_thread.run_sync(_geometry, a)
    yield _ev("analysis", **_analysis_view(a))



def _apply_edits(a: dict, body: dict) -> None:
    """Takes the user's corrections: values, edited meanings and dimensions they added by hand."""
    if isinstance(body.get("dimensions"), list):
        a["reading"]["dimensions"] = body["dimensions"]
    if isinstance(body.get("views"), list):
        a["reading"]["views"] = body["views"]
    if isinstance(body.get("answers"), dict):
        a["answers"] = {k: float(v) for k, v in body["answers"].items() if v not in ("", None)}
    for k, v in body.get("values", {}).items():
        if v in ("", None):
            continue
        try:
            a["values"][k] = float(str(v).replace(",", "."))
        except ValueError:
            continue  # text in a number field (a table cell copied by the model): left out
    if "overall" in body:
        a["reading"]["overall"] = body["overall"]
    # dimensions the user marked as overall decide the overall size along their axis
    overall = a["reading"].setdefault("overall", {})
    for d in a["reading"].get("dimensions", []):
        if d.get("overall") and d.get("axis") in ("length", "width", "height"):
            ref = overall.get(d["axis"])
            ref_dim = next((x for x in a["reading"]["dimensions"] if x.get("id") == ref), None)
            if ref_dim is None or ref_dim.get("axis") != d["axis"] or not ref_dim.get("overall"):
                overall[d["axis"]] = d["id"]


async def drawing_replan(request: Request):
    """Second pass: the model writes only the build plan from dimensions the user confirmed."""
    body = await request.json()
    a = analyses.get(body.get("id"))

    async def run():
        global stop_requested
        stop_requested = False
        if a is None:
            yield _ev("error", text="Разбор не найден, начните заново")
            return
        _apply_edits(a, body)
        off = drawing.excluded_views(a["reading"])
        crops = [(k + 1, _scaled_part(_upload_path(u))["url"]) for k, u in enumerate(a["views"]) if k not in off]
        msgs = drawing.plan_messages(a["reading"], a["values"], crops, body.get("note", ""))
        fmt = {"type": "json_schema", "json_schema": {"name": "plan", "schema": drawing.PLAN_SCHEMA}}
        yield _ev("stage", text="Модель составляет план по подтверждённым размерам…")
        msg = None
        try:
            async for kind, data in llm_stream(_drawing_settings(), msgs, None, fmt):
                if kind == "progress":
                    yield _ev("progress", step=1, **data)
                else:
                    msg = data
            res = drawing.parse_reading(msg["content"])
        except Exception as e:  # noqa: BLE001
            yield _ev("error", text=f"Не удалось составить план: {e}")
            return
        a["reading"]["plan"] = res.get("plan", a["reading"].get("plan", {}))
        if res.get("overall"):
            a["reading"]["overall"] = res["overall"]
        log_call("plan_drawing", json.dumps({"id": a["id"]}), json.dumps(res, ensure_ascii=False), False)
        yield _ev("analysis", **_analysis_view(a))

    return StreamingResponse(_guarded(run(), body), media_type="application/x-ndjson")


async def drawing_preview(request: Request):
    try:
        return await _drawing_preview(request)
    except Exception as e:  # noqa: BLE001
        import traceback
        log_call("preview_error", "", traceback.format_exc(), True)
        return JSONResponse({"error": f"Внутренняя ошибка проверки: {type(e).__name__}: {e}. "
                                      f"Подробности записаны в журнал calls.jsonl"})


async def _drawing_preview(request: Request):
    """Recomputes the checks with the user's corrected values, builds the part and returns its
    ESKD projections for side-by-side comparison with the drawing."""
    body = await request.json()
    a = analyses.get(body.get("id"))
    if a is None:
        return JSONResponse({"error": "Разбор не найден, начните заново"}, status_code=404)
    return JSONResponse(await _preview(a, body))


async def _preview(a: dict, body: dict, projections: bool = True) -> dict:
    """Builds the part from the analysis with the user's corrections (values, feature table) and
    checks it against the drawing: overall sizes, the title block's mass, KOMPAS errors."""
    _apply_edits(a, body)
    if isinstance(body.get("features"), dict):
        a["feature_edits"] = body["features"]
    await anyio.to_thread.run_sync(_geometry, a)
    out = _analysis_view(a)
    try:
        ops = drawing.resolve_plan(a["reading"]["plan"]["operations"], a["values"])
        ops = _apply_feature_edits(ops, a.get("feature_edits") or {})
    except (ValueError, KeyError) as e:
        out["build"] = {"error": str(e)}
        return out
    # a cut that KOMPAS refuses is skipped and named in the result, the rest of the part is built
    text, images, err = await bridge.call("build_part", {"operations": ops, "name": a["reading"].get("title") or "По чертежу",
                                                       "screenshot": False, "keep_going": True})
    out["build"] = {"error": text} if err else json.loads(text)
    if not err and not out["build"].get("volume_mm3"):
        out["issues"].append({"level": "error", "dims": [],
                              "text": "Тело не построено: объём модели 0. План нужно исправить или составить заново."})
    if not err:
        # the real body's overall size, measured by KOMPAS, against the drawing's overall dimensions
        size = out["build"].get("size_mm")
        overall = a["reading"].get("overall", {})
        for k, (axis, label) in enumerate((("length", "длина X"), ("width", "ширина Y"), ("height", "высота Z"))):
            if size and axis in overall:
                with contextlib.suppress(ValueError):
                    want = drawing.evaluate(overall[axis], a["values"])
                    if abs(size[k] - want) > 0.05:
                        out["issues"].append({"level": "error", "dims": [],
                                              "text": f"Габарит модели: {label} = {size[k]:g} мм, на чертеже {want:g} мм"})
        mass = a["reading"].get("mass_kg")
        if mass and out["build"].get("mass_g"):
            diff = out["build"]["mass_g"] / 1000 - float(mass)
            if abs(diff) > 0.05 * float(mass):
                out["issues"].append({"level": "warning", "dims": [],
                                      "text": f"Масса модели {out['build']['mass_g'] / 1000:.3f} кг, в штампе {mass} кг"})
        if projections:
            ptext, pimages, perr = await bridge.call("projections", {})
            if not perr:
                out["projections"] = dict(zip(json.loads(ptext), pimages))
    a["last_build"] = out.get("build")
    return out


async def drawing_view(request: Request):
    """An analysis made earlier (by the batch) for the review window, with its last build."""
    body = await request.json()
    a = analyses.get(body.get("id"))
    if a is None:
        return JSONResponse({"error": "Разбор не найден, начните заново"}, status_code=404)
    return JSONResponse({**_analysis_view(a), "build": a.get("last_build")})


async def drawing_batch(request: Request):
    """Many drawings one after another, without the language model: each is recognised, built,
    checked (mass against the title block, KOMPAS errors), optionally saved as .m3d and .step to
    the output folder, and closed. Streams one row per drawing."""
    body = await request.json()
    items = [x for x in body.get("items") or [] if isinstance(x, dict) and x.get("image")]
    save = bool(body.get("save"))

    async def run():
        global stop_requested
        for i, it in enumerate(items):
            if stop_requested:
                yield _ev("row", i=i, status="stopped", text="остановлено")
                break
            name = it.get("name") or f"чертёж {i + 1}"
            yield _ev("row", i=i, status="work", text="распознаю…")
            a, err = None, None
            async for ev in _analyze_events({"image": it["image"], "batch": True}):
                d = json.loads(ev)
                if d.get("type") == "analysis":
                    a = analyses.get(d["id"])
                elif d.get("type") == "error":
                    err = d.get("text")
                elif d.get("type") == "stage":
                    yield _ev("row", i=i, status="work", text=d.get("text", ""))
            if a is None or (a.get("geometry") or {}).get("error"):
                yield _ev("row", i=i, status="error", text=err or (a or {}).get("geometry", {}).get("error")
                          or "не распознан", id=a and a["id"])
                continue
            yield _ev("row", i=i, status="work", text="строю…", id=a["id"], recognised=_part_kind(a))
            out = await _preview(a, {}, projections=False)
            b = out.get("build") or {}
            saved = []
            if save and not b.get("error") and b.get("volume_mm3"):
                stem = re.sub(r'[\\/:*?"<>|]+', "_", os.path.splitext(name)[0]).strip() or f"part{i + 1}"
                for ext, tool in (("m3d", "save"), ("step", "export")):
                    text, _, e2 = await bridge.call(tool, {"path": f"{stem}.{ext}"})
                    if not e2:
                        saved.append(json.loads(text).get("saved") or json.loads(text).get("exported"))
            await bridge.call("close_document", {})
            yield _ev("row", i=i, status="done", id=a["id"], recognised=_part_kind(a), build=b,
                      mass_stamp=a["reading"].get("mass_kg"), saved=saved,
                      issues=[x["text"] for x in out.get("issues", []) if x.get("level") == "error"])
        yield _ev("done")
    return StreamingResponse(_guarded(run(), body), media_type="application/x-ndjson")


async def drawing_accept(request: Request):
    """Hands the confirmed result over to the chat, so the conversation can continue from it."""
    body = await request.json()
    a = analyses.get(body.get("id"))
    if a is None:
        return JSONResponse({"error": "Разбор не найден"}, status_code=404)
    dims = "; ".join(f"{d['id']} {d.get('meaning', '')} = {a['values'].get(d['id'], d.get('value', 0)):g}"
                     for d in a["reading"].get("dimensions", []))
    b = a.get("last_build") or {}
    conversation.append({"role": "user", "content": "Построй деталь по чертежу (размеры подтверждены пользователем)."})
    conversation.append({"role": "assistant", "content":
                         f"Деталь построена по проверенному плану. Размеры: {dims}. "
                         f"Объём {b.get('volume_mm3', '?')} мм³, масса {b.get('mass_g', '?')} г."})
    return JSONResponse({"ok": True, "summary": conversation[-1]["content"]})


async def stop(_: Request):
    global stop_requested
    stop_requested = True
    return JSONResponse({"ok": True})


async def reset(_: Request):
    conversation.clear()
    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with bridge.run():
        yield


app = Starlette(routes=[
    Route("/", index),
    Route("/api/settings", get_settings, methods=["GET"]),
    Route("/api/settings", put_settings, methods=["PUT"]),
    Route("/api/settings/defaults", get_defaults),
    Route("/api/models", models),
    Route("/api/tools", tools),
    Route("/api/status", status),
    Route("/api/tool", call_tool, methods=["POST"]),
    Route("/api/chat", chat, methods=["POST"]),
    Route("/api/reset", reset, methods=["POST"]),
    Route("/api/stop", stop, methods=["POST"]),
    Route("/api/upload", upload, methods=["POST"]),
    Route("/api/drawing/analyze", drawing_analyze, methods=["POST"]),
    Route("/api/drawing/preview", drawing_preview, methods=["POST"]),
    Route("/api/drawing/replan", drawing_replan, methods=["POST"]),
    Route("/api/drawing/accept", drawing_accept, methods=["POST"]),
    Route("/api/drawing/view", drawing_view, methods=["POST"]),
    Route("/api/drawing/batch", drawing_batch, methods=["POST"]),
    Route("/uploads/{uid}/{name}", uploaded_file),
], lifespan=lifespan)


if __name__ == "__main__":
    port = int(os.environ.get("KOMPAS_UI_PORT", "8765"))
    print(f"Интерфейс: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
