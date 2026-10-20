"""КОМПАС MCP как отдельная программа Windows.

Запускает веб-сервер интерфейса в фоне и открывает его в собственном окне (Microsoft Edge в режиме
приложения, без вкладок и адресной строки). Когда окно закрыто, сервер останавливается.
Запускать через pythonw.exe, чтобы не было консоли: ярлык создаёт install_shortcut.ps1.
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "ui"))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "kompas-mcp")
LOG = os.path.join(DATA, "app.log")
PORT = int(os.environ.get("KOMPAS_UI_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def message(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "КОМПАС MCP", 0x10)


def port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_ready(timeout: float = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(URL + "/api/tools", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def open_window() -> subprocess.Popen:
    edge = next((p for p in EDGE_PATHS if os.path.exists(p)), None)
    if edge is None:
        os.startfile(URL)  # no Edge: fall back to the default browser
        return None
    profile = os.path.join(DATA, "window")
    # the version in the address: after an update the window loads the new page, not a cached one
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as fh:
            ver = fh.read().strip()
    except OSError:
        ver = str(int(time.time()))
    return subprocess.Popen([edge, f"--app={URL}/?v={ver}", f"--user-data-dir={profile}",
                             "--window-size=1350,900", "--no-first-run", "--disable-features=Translate"])


def main() -> None:
    os.makedirs(DATA, exist_ok=True)
    try:
        log = open(LOG, "a", encoding="utf-8", buffering=1)  # pythonw has no console
    except OSError:
        # written once by an elevated process, the file is closed to the normal one: use another
        log = open(os.path.join(DATA, f"app-{os.getpid()}.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    print(f"\n--- start {time.ctime()}")

    if port_busy():
        # Already running: just open another window onto it.
        open_window()
        return

    import uvicorn
    import app as ui_app

    server = uvicorn.Server(uvicorn.Config(ui_app.app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    if not wait_ready():
        message(f"Интерфейс не запустился. Подробности в журнале:\n{LOG}")
        return

    window = open_window()
    if window is None:
        while True:  # default browser: keep serving until the process is killed
            time.sleep(3600)
    # If a window of our profile is already open, Edge hands the request over to it and the launched
    # process exits at once, so watch every Edge process that uses our profile, not just this one.
    window.wait()
    time.sleep(2)
    while window_open():
        time.sleep(3)
    server.should_exit = True
    time.sleep(1)


def window_open() -> bool:
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    wmi = win32com.client.GetObject("winmgmts:")
    profile = os.path.join(DATA, "window").lower()
    for p in wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name = 'msedge.exe'"):
        if p.CommandLine and profile in p.CommandLine.lower():
            return True
    return False


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        message("Ошибка запуска КОМПАС MCP:\n" + traceback.format_exc()[-800:])
