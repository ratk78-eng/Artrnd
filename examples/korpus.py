"""Корпус ИГ.КР.01.07.02 (example.pdf, лист 1), построенный через MCP-сервер.

Оси: X — длина 110, Y — ширина 70, Z — высота 60. Вид спереди на чертеже — плоскость XZ.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from smoke_test import Client  # noqa: E402


def data(res):
    return json.loads(res["content"][0]["text"])


def main():
    c = Client()
    c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "korpus", "version": "0"}})
    c.send("notifications/initialized", notify=True)

    c.call("new_part", name="Корпус ИГ.КР.01.07.02")

    # 1. Профиль вида спереди (плоскость XZ), выдавленный на ширину 70.
    profile = [[0, 0], [110, 0], [110, 60], [70, 60], [40, 40], [40, 30], [0, 30]]
    sid = data(c.call("sketch", plane="XZ", shapes=[{"type": "polyline", "points": profile}]))["sketch_id"]
    c.call("extrude", sketch_id=sid, depth=70)
    m = data(c.call("model_info"))["mass"]
    cx, cy, cz = m["center_mm"]
    if cz < 0 or cy < 0:
        # Ориентация осей эскиза на XZ отличается от ожидаемой: перестраиваем зеркально.
        c.call("new_part", name="Корпус ИГ.КР.01.07.02")
        fz = -1 if cz < 0 else 1
        pts = [[x, fz * z] for x, z in profile]
        sid = data(c.call("sketch", plane="XZ", shapes=[{"type": "polyline", "points": pts}]))["sketch_id"]
        c.call("extrude", sketch_id=sid, depth=70, reverse=cy < 0)
        m = data(c.call("model_info"))["mass"]
        print("rebuilt, center:", m["center_mm"])

    # 2. Паз 40 мм между стенками, дно на высоте 30, от x=40 до x=95.
    sid = data(c.call("sketch", plane="XY", offset=30,
                      shapes=[{"type": "rect", "x": 40, "y": 15, "w": 55, "h": 40}]))["sketch_id"]
    c.call("extrude", sketch_id=sid, depth=40, cut=True)

    # 3. Сквозное отверстие Ø30 с центром в 55 мм от левого торца.
    sid = data(c.call("sketch", plane="XY", shapes=[{"type": "circle", "x": 55, "y": 35, "d": 30}]))["sketch_id"]
    c.call("extrude", sketch_id=sid, depth=0, cut=True, through_all=True)

    # 4. Канавка 30 × 8 от левого торца до оси отверстия.
    sid = data(c.call("sketch", plane="XY", offset=30,
                      shapes=[{"type": "rect", "x": 0, "y": 20, "w": 55, "h": 30}]))["sketch_id"]
    c.call("extrude", sketch_id=sid, depth=8, cut=True, reverse=True)

    c.call("model_info")
    c.call("save", path="korpus/korpus.m3d")
    c.call("export", path="korpus/korpus.step")
    c.call("screenshot", path="korpus/korpus_iso.png")
    c.p.terminate()


if __name__ == "__main__":
    main()
