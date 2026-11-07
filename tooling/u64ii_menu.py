#!/usr/bin/env python3
"""Read and drive the Ultimate menu of a C64 Ultimate or Ultimate 64 Elite II
through its Telnet remote screen, with a screen that can be read before each key.

Firmware that has no `machine:input` or `machine:menu_screen` route, such as
the recovery application (3.14c), still serves the menu on Telnet port 23. This
tool holds one Telnet session in a background process, keeps a small VT100
screen model of it, and exposes three client commands:

    u64ii_menu.py HOST start           open the session
    u64ii_menu.py HOST show            print the screen as text
    u64ii_menu.py HOST key NAME...     send keys, then print the screen
    u64ii_menu.py HOST wait TEXT [S]   wait up to S seconds (default 20) for TEXT
    u64ii_menu.py HOST stop            close the session

Key names: up down left right enter esc f1..f8 back home end pageup pagedown,
or `text:abc` for literal characters. Every command prints the screen it ends
on, so each key that answers a prompt is chosen after reading the prompt.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

ROWS, COLS = 30, 80
KEYS = {
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "enter": b"\r", "esc": b"\x1b", "back": b"\x7f",
    "home": b"\x1b[1~", "end": b"\x1b[4~", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
}
LINE_DRAWING = {"q": "-", "x": "|", "l": "+", "k": "+", "m": "+", "j": "+",
                "t": "+", "u": "+", "v": "+", "w": "+", "n": "+"}


class Screen:
    """Just enough VT100 for the Ultimate's remote screen."""

    def __init__(self) -> None:
        self.reset()
        self.pending = b""

    def reset(self) -> None:
        self.grid = [[" "] * COLS for _ in range(ROWS)]
        self.row = self.col = 0
        self.graphics = False

    def feed(self, data: bytes) -> None:
        data = self.pending + data
        self.pending = b""
        i = 0
        while i < len(data):
            b = data[i]
            if b == 0xFF:                                  # Telnet IAC
                if i + 2 >= len(data):
                    self.pending = data[i:]
                    return
                i += 3
            elif b == 0x1B:
                j = self._escape(data, i)
                if j < 0:
                    self.pending = data[i:]
                    return
                i = j
            else:
                self._char(b)
                i += 1

    def _char(self, b: int) -> None:
        if b == 0x0D:
            self.col = 0
        elif b == 0x0A:
            self.row = min(self.row + 1, ROWS - 1)
        elif b == 0x08:
            self.col = max(self.col - 1, 0)
        elif b >= 0x20:
            ch = chr(b)
            if self.graphics:
                ch = LINE_DRAWING.get(ch, ch)
            if self.col < COLS:
                self.grid[self.row][self.col] = ch
            self.col = min(self.col + 1, COLS)

    def _escape(self, data: bytes, i: int) -> int:
        if i + 1 >= len(data):
            return -1
        kind = data[i + 1]
        if kind == ord("c"):
            self.reset()
            return i + 2
        if kind in (ord("("), ord(")")):
            if i + 2 >= len(data):
                return -1
            if kind == ord("("):
                self.graphics = data[i + 2] == ord("0")
            return i + 3
        if kind != ord("["):
            return i + 2
        j = i + 2
        while j < len(data) and not 0x40 <= data[j] <= 0x7E:
            j += 1
        if j >= len(data):
            return -1
        params = data[i + 2:j].decode("ascii", "replace").lstrip("?")
        final = chr(data[j])
        nums = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else []
        if final in "Hf":
            self.row = min(max((nums[0] if nums else 1) or 1, 1), ROWS) - 1
            self.col = min(max((nums[1] if len(nums) > 1 else 1) or 1, 1), COLS) - 1
        elif final == "J":
            if (nums[0] if nums else 0) == 2:
                self.reset()
        elif final == "K":
            mode = nums[0] if nums else 0
            lo, hi = {0: (self.col, COLS), 1: (0, self.col + 1), 2: (0, COLS)}.get(mode, (0, 0))
            for c in range(lo, min(hi, COLS)):
                self.grid[self.row][c] = " "
        return j + 1

    def text(self) -> str:
        lines = ["".join(r).rstrip() for r in self.grid]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def socket_path(host: str) -> str:
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"u64ii-menu-{host}.sock")


def serve(host: str) -> None:
    screen = Screen()
    lock = threading.Lock()
    link = socket.create_connection((host, 23), timeout=10)
    link.settimeout(None)

    def pump() -> None:
        while True:
            data = link.recv(4096)
            if not data:
                os._exit(0)
            with lock:
                screen.feed(data)

    threading.Thread(target=pump, daemon=True).start()
    path = socket_path(host)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(1)
    while True:
        conn, _ = server.accept()
        request = conn.makefile("rb").readline().decode().rstrip("\n").split("\t")
        verb = request[0]
        reply = ""
        if verb == "stop":
            conn.sendall(b"stopped\n")
            os.unlink(path)
            os._exit(0)
        if verb == "key":
            for name in request[1:]:
                link.sendall(text_bytes(name))
                time.sleep(0.25)
            time.sleep(0.5)
        elif verb == "wait":
            deadline = time.time() + float(request[2])
            while time.time() < deadline and request[1] not in snapshot(screen, lock):
                time.sleep(0.2)
            reply = "" if request[1] in snapshot(screen, lock) else f"TIMEOUT waiting for {request[1]!r}\n"
        reply += snapshot(screen, lock) + "\n"
        conn.sendall(reply.encode())
        conn.close()


def snapshot(screen: Screen, lock: threading.Lock) -> str:
    with lock:
        return screen.text()


def text_bytes(name: str) -> bytes:
    if name.startswith("text:"):
        return name[5:].encode()
    return KEYS[name]


def client(host: str, *request: str) -> str:
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(60)
    conn.connect(socket_path(host))
    conn.sendall(("\t".join(request) + "\n").encode())
    out = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return out.decode()
        out += chunk


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    host, verb, args = argv[1], argv[2], argv[3:]
    if verb == "serve":
        serve(host)
        return 0
    if verb == "start":
        os.spawnl(os.P_NOWAIT, sys.executable, sys.executable, os.path.abspath(__file__), host, "serve")
        for _ in range(50):
            if os.path.exists(socket_path(host)):
                break
            time.sleep(0.1)
        time.sleep(1.5)
        verb = "show"
    if verb == "key":
        for name in args:
            if name not in KEYS and not name.startswith("text:"):
                print(f"unknown key {name!r}", file=sys.stderr)
                return 2
        out = client(host, "key", *args)
    elif verb == "wait":
        timeout = args[1] if len(args) > 1 else "20"
        try:
            valid = len(args) in (1, 2) and float(timeout) >= 0
        except ValueError:
            valid = False
        if not valid:                  # the server would die on a missing text or a bad timeout
            print(__doc__)
            return 2
        out = client(host, "wait", args[0], timeout)
    elif verb in ("show", "stop"):
        out = client(host, verb)
    else:
        print(__doc__)
        return 2
    print(out, end="")
    return 1 if out.startswith("TIMEOUT") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
