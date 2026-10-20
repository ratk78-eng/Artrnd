"""End-to-end test of the web UI with a fake OpenAI-compatible LLM.

The fake model asks for plate_with_holes, then screenshot, then answers with text.
Checks the whole chain: UI -> LLM API -> tool calls -> MCP server -> KOMPAS -> events.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK_PORT, UI_PORT = 8799, 8798
seen_requests = []


def tool_call(i, name, args):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


async def completions(request: Request):
    body = await request.json()
    seen_requests.append(body)
    n_tool_msgs = sum(1 for m in body["messages"] if m["role"] == "tool")
    if n_tool_msgs == 0:
        msg = {"role": "assistant", "content": "Строю пластину 100×60×5.",
               "tool_calls": [tool_call(1, "plate_with_holes", {"length": 100, "width": 60, "thickness": 5,
                                                                "hole_diameter": 4, "edge_offset": 8})]}
    elif n_tool_msgs == 1:
        msg = {"role": "assistant", "content": None, "tool_calls": [tool_call(2, "screenshot", {"path": "ui_test/plate.png"})]}
    else:
        msg = {"role": "assistant", "content": "Готово: пластина построена."}
    assert body.get("stream") is True
    return StreamingResponse(sse(msg), media_type="text/event-stream")


async def sse(msg):
    """Streams msg the way OpenAI-compatible servers do: reasoning, content and tool calls in pieces."""
    def chunk(delta):
        return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}, ensure_ascii=False) + "\n\n"
    yield chunk({"reasoning": "Думаю о размерах. "})
    yield chunk({"reasoning": "Всё ясно."})
    if msg.get("content"):
        half = len(msg["content"]) // 2
        yield chunk({"content": msg["content"][:half]})
        yield chunk({"content": msg["content"][half:]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        args = tc["function"]["arguments"]
        yield chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                     "function": {"name": tc["function"]["name"], "arguments": args[:5]}}]})
        yield chunk({"tool_calls": [{"index": i, "function": {"arguments": args[5:]}}]})
    yield "data: [DONE]\n\n"


async def models(_):
    return JSONResponse({"data": [{"id": "fake-model"}]})


def http(path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{UI_PORT}{path}", data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode("utf-8")


def main():
    mock = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"]), Route("/v1/models", models)])
    threading.Thread(target=uvicorn.run, args=(mock,), kwargs={"port": MOCK_PORT, "log_level": "warning"}, daemon=True).start()

    settings = os.path.join(tempfile.mkdtemp(), "settings.json")
    env = dict(os.environ, KOMPAS_UI_SETTINGS=settings, KOMPAS_UI_PORT=str(UI_PORT), PYTHONIOENCODING="utf-8")
    ui = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "ui", "app.py")], env=env)
    try:
        for _ in range(60):
            try:
                http("/api/tools"); break
            except Exception:
                time.sleep(1)
        print("tools:", [t["name"] for t in json.loads(http("/api/tools"))["tools"]])
        print("status:", http("/api/status")[:200])
        http("/api/settings", {"base_url": f"http://127.0.0.1:{MOCK_PORT}/v1", "model": "fake-model"}, "PUT")
        print("models:", http("/api/models"))
        assert "<title>КОМПАС MCP</title>" in http("/")
        events = [json.loads(l) for l in http("/api/chat", {"text": "Создай пластину"}).splitlines() if l.strip()]
        for e in events:
            if e["type"] == "tool_result":
                e = {**e, "images": [f"<{len(i['data'])} b64 chars>" for i in e["images"]], "text": e["text"][:150]}
            print(e)
        kinds = [e["type"] for e in events]
        assert kinds.count("tool_result") == 2 and kinds[-1] == "done", kinds
        assert "reasoning" in kinds, kinds
        assert not any(e.get("error") for e in events if e["type"] == "tool_result")
        assert any(e["images"] for e in events if e["type"] == "tool_result")
        assert len(seen_requests) == 3 and seen_requests[0]["tools"], "LLM did not receive tools"
        print("UI TEST PASSED")
    finally:
        ui.terminate()


if __name__ == "__main__":
    main()
