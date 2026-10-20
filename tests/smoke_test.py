"""End-to-end smoke test: starts server.py over stdio and calls tools like an MCP client would."""
import json
import os
import subprocess
import sys
import threading
import queue
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server.py")


class Client:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.q = queue.Queue()
        self.err = []
        threading.Thread(target=lambda: [self.q.put(l) for l in self.p.stdout], daemon=True).start()
        threading.Thread(target=lambda: [self.err.append(l) for l in self.p.stderr], daemon=True).start()
        self.i = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.i += 1
            msg["id"] = self.i
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        return None if notify else self.wait(self.i)

    def wait(self, i, t=180):
        end = time.time() + t
        while time.time() < end:
            try:
                m = json.loads(self.q.get(timeout=0.5))
            except queue.Empty:
                if self.p.poll() is not None:
                    raise RuntimeError("server exited:\n" + b"".join(self.err).decode(errors="replace"))
                continue
            if m.get("id") == i:
                return m
        raise TimeoutError(i)

    def call(self, tool, **args):
        r = self.send("tools/call", {"name": tool, "arguments": args})
        res = r.get("result", r)
        texts = [c.get("text", f"<{c['type']}>") for c in res.get("content", [])]
        flag = "ERROR" if res.get("isError") else "ok"
        print(f"[{flag}] {tool}: {' '.join(texts)[:600]}")
        return res


def main():
    c = Client()
    c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "smoke", "version": "0"}})
    c.send("notifications/initialized", notify=True)
    tools = c.send("tools/list")["result"]["tools"]
    print("tools:", [t["name"] for t in tools])
    c.call("diagnostics", launch_if_needed=True)
    c.call("plate_with_holes", length=100, width=60, thickness=5, hole_diameter=4, edge_offset=8,
           name="Пластина 100x60x5")
    c.call("model_info")
    boss = c.call("sketch", plane="XY", offset=5, shapes=[{"type": "circle", "x": 50, "y": 30, "d": 20}])
    c.call("extrude", sketch_id=json.loads(boss["content"][0]["text"])["sketch_id"], depth=10)
    c.call("model_info")
    c.call("save", path="smoke/plate.m3d")
    c.call("export", path="smoke/plate.step")
    c.call("export", path="smoke/plate.stl")
    c.call("screenshot", path="smoke/plate.png")
    # revolve: shaft profile with axis on X
    c.call("new_part", name="Вал")
    sk = c.call("sketch", plane="XY", shapes=[
        {"type": "line", "x1": -10, "y1": 0, "x2": 60, "y2": 0, "style": 3},
        {"type": "polyline", "points": [[0, 0], [0, 10], [30, 10], [30, 6], [50, 6], [50, 0]]},
    ])
    sid = json.loads(sk["content"][0]["text"])["sketch_id"]
    c.call("revolve", sketch_id=sid)
    c.call("model_info")
    c.call("screenshot", path="smoke/shaft.png")
    # 2D fragment + DXF
    c.call("new_fragment")
    c.call("draw_2d", shapes=[{"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60},
                              {"type": "circle", "x": 8, "y": 8, "d": 4},
                              {"type": "text", "x": 10, "y": 30, "text": "KOMPAS MCP", "height": 5}])
    c.call("save", path="smoke/plate.frw")
    c.call("export", path="smoke/plate.dxf")
    c.p.terminate()


if __name__ == "__main__":
    main()
