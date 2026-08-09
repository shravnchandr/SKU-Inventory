"""Start the local inventory web app.

Double-click run.command (Mac) or run.bat (Windows) instead of running this
directly — they set the working directory correctly first. If you do run it
yourself: `uv run app.py`.
"""
from __future__ import annotations

import socket
import threading
import webbrowser

from src.gowri_proj.webapp import create_app

HOST = "127.0.0.1"
PORT = 8765


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def main() -> None:
    port = PORT
    if not _port_is_free(port):
        # Something's already listening (very likely a previous run of this
        # app) — just open the browser to it rather than failing to bind.
        print(f"Something is already running on port {port} — opening it in your browser.")
        webbrowser.open(f"http://{HOST}:{port}/")
        return

    app = create_app()
    url = f"http://{HOST}:{port}/"
    print(f"Starting inventory app at {url}")
    print("Leave this window open while you use the app. Close it to stop.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
