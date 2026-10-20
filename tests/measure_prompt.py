"""Measures how many prompt tokens the system prompt, the tool schemas and a drawing image cost,
by asking the loaded LM Studio model for 1 token and reading usage.prompt_tokens."""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
import app  # noqa: E402

BASE = "http://127.0.0.1:1234/v1"
model = sys.argv[1]
image = sys.argv[2] if len(sys.argv) > 2 else None


def prompt_tokens(messages, tools=None):
    body = {"model": model, "messages": messages, "max_tokens": 1, "temperature": 0}
    if tools:
        body["tools"] = tools
    t = time.time()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                                                 headers={"Content-Type": "application/json"}), timeout=900).read())
    return r["usage"]["prompt_tokens"], round(time.time() - t, 1)


async def tools_schema():
    async with app.bridge.run():
        return app.bridge.openai_tools()

import anyio  # noqa: E402
tools = anyio.run(tools_schema)
sysp = app.DEFAULT_SETTINGS["system_prompt"]
user = {"role": "user", "content": "Создай пластину 100 × 60 × 5 мм"}
base, t1 = prompt_tokens([user])
with_sys, t2 = prompt_tokens([{"role": "system", "content": sysp}, user])
with_tools, t3 = prompt_tokens([{"role": "system", "content": sysp}, user], tools)
print(f"user only: {base} tok ({t1}s); +system prompt: {with_sys - base}; +14 tool schemas: {with_tools - with_sys}; total {with_tools} ({t3}s)")
if image:
    for side in (1600, 1000):
        app.MAX_IMAGE_SIDE = side
        part = app.image_part(image)
        n, t = prompt_tokens([{"role": "user", "content": [{"type": "text", "text": "Опиши чертёж"}, part]}])
        print(f"image {side}px: {n} tok ({t}s)")
