"""Drive the real rework TUI from scripts: a pty + pyte terminal emulator.

    uv run --with pyte python scripts/tui_driver.py serve [-- command...]   # starts it
    uv run --with pyte python scripts/tui_driver.py screen                  # print screen
    uv run --with pyte python scripts/tui_driver.py type "fix the menus"
    uv run --with pyte python scripts/tui_driver.py key enter               # enter/esc/tab/
    uv run --with pyte python scripts/tui_driver.py key alt+2               # up/down/ctrl+n...
    uv run --with pyte python scripts/tui_driver.py click "Launch"          # first match
    uv run --with pyte python scripts/tui_driver.py click "Gates" --row 1
    uv run --with pyte python scripts/tui_driver.py wait "PROPOSAL" 60
    uv run --with pyte python scripts/tui_driver.py stop

Keys are sent as the bytes a macOS terminal sends (alt+N = the Option
character, e.g. alt+2 = "™"). The server listens on a unix socket.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time

SOCK = os.environ.get("TUI_DRIVER_SOCK", "/tmp/rework-tui-driver.sock")
COLS, ROWS = int(os.environ.get("TUI_COLS", "160")), int(os.environ.get("TUI_ROWS", "45"))

KEYS = {
    "enter": b"\r",
    "esc": b"\x1b",
    "escape": b"\x1b",
    "tab": b"\t",
    "shift+tab": b"\x1b[Z",
    "backspace": b"\x7f",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "space": b" ",
    "alt+1": "¡".encode(),
    "alt+2": "™".encode(),
    "alt+3": "£".encode(),
    "alt+4": "¢".encode(),
    "alt+5": "∞".encode(),
    "alt+a": "å".encode(),
    "alt+x": "≈".encode(),
    "alt+enter": b"\x1b\r",
    "f1": b"\x1bOP",
}


def _ctrl(name: str) -> bytes | None:
    if name.startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return bytes([ord(name[5].lower()) - 96])
    return None


def serve(command: list[str]) -> None:
    import pyte

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    lock = threading.Lock()
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ["TERM"] = "xterm-256color"
        os.execvp(command[0], command)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    alive = {"ok": True}

    def reader() -> None:
        while alive["ok"]:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    alive["ok"] = False
                    return
                with lock:
                    stream.feed(data)

    threading.Thread(target=reader, daemon=True).start()

    def lines() -> list[str]:
        with lock:
            return [line.rstrip() for line in screen.display]

    def find(text: str, row: int | None) -> tuple[int, int] | None:
        for r, line in enumerate(lines()):
            if row is not None and r != row:
                continue
            c = line.find(text)
            if c >= 0:
                return r, c
        return None

    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(4)
    print(f"driving pid {pid} on {SOCK}", flush=True)
    while alive["ok"]:
        srv.settimeout(0.5)
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            continue
        req = json.loads(conn.recv(65536).decode())
        op = req["op"]
        out: dict = {"ok": True}
        if op == "screen":
            out["lines"] = lines()
        elif op == "type":
            os.write(fd, req["text"].encode())
        elif op == "key":
            name = req["name"]
            data = KEYS.get(name) or _ctrl(name)
            if data is None:
                out = {"ok": False, "error": f"unknown key {name}"}
            else:
                os.write(fd, data)
        elif op == "click":
            hit = find(req["text"], req.get("row"))
            if hit is None:
                out = {"ok": False, "error": f"{req['text']!r} not on screen"}
            else:
                r, c = hit
                c += 1 + int(req.get("offset", 1))
                os.write(fd, f"\x1b[<0;{c};{r + 1}M".encode())
                time.sleep(0.05)
                os.write(fd, f"\x1b[<0;{c};{r + 1}m".encode())
                out["at"] = [r, c]
        elif op == "wait":
            end = time.time() + float(req.get("seconds", 30))
            out["found"] = False
            while time.time() < end:
                if find(req["text"], None):
                    out["found"] = True
                    break
                time.sleep(0.3)
        elif op == "stop":
            alive["ok"] = False
        conn.sendall(json.dumps(out).encode())
        conn.close()
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    srv.close()
    if os.path.exists(SOCK):
        os.unlink(SOCK)


def call(req: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall(json.dumps(req).encode())
    chunks = []
    while True:
        data = s.recv(65536)
        if not data:
            break
        chunks.append(data)
    return json.loads(b"".join(chunks).decode())


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    op = args[0]
    if op == "serve":
        command = args[args.index("--") + 1 :] if "--" in args else ["rework"]
        serve(command)
        return
    if op == "screen":
        res = call({"op": "screen"})
        for i, line in enumerate(res["lines"]):
            print(f"{i:2d} {line}")
        return
    if op == "type":
        print(call({"op": "type", "text": args[1]}))
    elif op == "key":
        for name in args[1:]:
            print(call({"op": "key", "name": name}))
            time.sleep(0.15)
    elif op == "click":
        row = int(args[args.index("--row") + 1]) if "--row" in args else None
        print(call({"op": "click", "text": args[1], "row": row}))
    elif op == "wait":
        print(
            call(
                {"op": "wait", "text": args[1], "seconds": float(args[2]) if len(args) > 2 else 30}
            )
        )
    elif op == "stop":
        print(call({"op": "stop"}))


if __name__ == "__main__":
    main()
