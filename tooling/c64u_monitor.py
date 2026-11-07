#!/usr/bin/env python3
"""Watch a C64 Ultimate / Ultimate 64 Elite II continuously and flag trouble early.

Three independent sources, one event log:

  video    the VIC stream on a multicast group of its own (239.0.1.65 by
           default), so the U64's stream on 239.0.1.64 cannot mix in.
           Unicast does not work on a machine with both Ethernet and WiFi up:
           the firmware finds the destination MAC by ARP, never sees the
           reply, and answers "Network Host Resolve Error". Frame rate,
           packet loss, all-black picture; the latest frame is saved as PNG.
  rest     /v1/info every few seconds: reachability, firmware and FPGA
           version changes, reported errors. Restarts the video stream when
           the device comes back without it.
  console  the CPU's UART output over JTAG (tooling/u64ii_jtag.py). Each poll
           takes the c64u device lock without waiting and skips when another
           JTAG user holds it, so deploys are never blocked for long.

Every line in events.log starts with a timestamp and a level; lines needing
attention carry ALERT. Watch it with:  tail -f <out>/events.log | grep ALERT

Create <out>/hold while a test run drives the machine: the monitor then keeps
logging but no longer restarts the video stream, which the tests use too.

Stop it with `c64u_monitor.py --stop --out <out>`, which signals the pid in
<out>/monitor.pid, rather than with pkill -f: a pattern given to pkill also
matches the shell that runs pkill. The video stream is stopped on the way out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIDTH = 384

# Console text that should never pass unnoticed. Matched case-insensitively.
CONSOLE_ALERTS = re.compile(
    r"assert|exception|panic|trap|abort|guru|stack overflow|out of memory|"
    r"malloc fail|heap|watchdog|hard ?fault|illegal|crash|corrupt|"
    r"\berror\b|failed|timeout|wedge|^Lock$|Hello world|Empty; waiting|Magic",
    re.IGNORECASE)
# Lines containing these are routine and only logged. A client closing its
# connection first makes the firmware print a socket read or write error, so a
# status poller that connects every few seconds produces them all the time.
CONSOLE_QUIET = re.compile(r"^(Accept client|HTTP (GET|PUT|POST))|"
                           r"^ERROR (reading from|writing to) socket")

PALETTE = [
    (0x00, 0x00, 0x00), (0xEF, 0xEF, 0xEF), (0x8D, 0x2F, 0x34), (0x6A, 0xD4, 0xCD),
    (0x98, 0x35, 0xA4), (0x4C, 0xB4, 0x42), (0x2C, 0x29, 0xB1), (0xEF, 0xEF, 0x5D),
    (0x98, 0x4E, 0x20), (0x5B, 0x38, 0x00), (0xD1, 0x67, 0x6D), (0x4A, 0x4A, 0x4A),
    (0x7B, 0x7B, 0x7B), (0x9F, 0xEF, 0x93), (0x6D, 0x6A, 0xEF), (0xB2, 0xB2, 0xB2),
]


class EventLog:
    def __init__(self, path: str):
        self.handle = open(path, "a", buffering=1)
        self.lock = threading.Lock()
        self.last: dict = {}

    def write(self, source: str, level: str, text: str, every: float = 0.0,
              key: str = "") -> None:
        """Log one event; with `every`, repeat the same text (or key) at most that often."""
        key = (source, level, key or text)
        now = time.time()
        if every and now - self.last.get(key, 0) < every:
            return
        self.last[key] = now
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
        with self.lock:
            self.handle.write(f"{stamp} {level:5} {source:7} {text}\n")


def rest(host: str, path: str, method: str = "GET", timeout: float = 4.0):
    request = urllib.request.Request(f"http://{host}{path}", method=method)
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        body = answer.read()
        return answer.status, body


def local_address_towards(host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((socket.gethostbyname(host), 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


# ---------------------------------------------------------------------------
class VideoWatch(threading.Thread):
    """Receives the multicast VIC stream and judges it once per second."""

    def __init__(self, args, log: EventLog, stop: threading.Event):
        super().__init__(daemon=True, name="video")
        self.args, self.log, self.stop = args, log, stop
        self.last_packet = 0.0
        self.last_status = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", args.video_port))
        membership = struct.pack("4s4s", socket.inet_aton(args.group),
                                 socket.inet_aton(args.local_ip))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self.sock.settimeout(0.5)

    def start_stream(self) -> bool:
        target = f"{self.args.group}:{self.args.video_port}"
        try:
            status, _ = rest(self.args.host, f"/v1/streams/video:start?ip={target}", "PUT")
            self.log.write("video", "INFO", f"stream start to {target}: HTTP {status}")
            return status == 200
        except Exception as exc:                            # noqa: BLE001
            # Right after a boot the firmware answers 500 until its network is
            # up; the REST watcher retries every few seconds.
            self.log.write("video", "INFO", f"stream start failed: {exc}", every=30,
                           key="stream start")
            return False

    def stop_stream(self) -> None:
        try:
            rest(self.args.host, "/v1/streams/video:stop", "PUT")
            self.log.write("video", "INFO", "stream stopped")
        except Exception:                                   # noqa: BLE001
            pass

    def save_png(self, raw: bytes) -> None:
        try:
            from PIL import Image
        except ImportError:
            return
        lines = len(raw) // (WIDTH // 2)
        img = Image.new("RGB", (WIDTH, lines))
        pixels = img.load()
        for i, byte in enumerate(raw[:lines * WIDTH // 2]):
            y, x = divmod(i, WIDTH // 2)
            pixels[2 * x, y] = PALETTE[byte & 15]
            pixels[2 * x + 1, y] = PALETTE[byte >> 4]
        tmp = os.path.join(self.args.out, "latest.tmp.png")
        img.save(tmp)
        os.replace(tmp, os.path.join(self.args.out, "latest.png"))

    def run(self) -> None:
        frame = bytearray()
        frames = lost = packets = 0
        last_seq = None
        window = time.time()
        last_hash, same_for, last_png = None, 0.0, 0.0
        black_since = None
        while not self.stop.is_set():
            try:
                data = self.sock.recv(2048)
            except socket.timeout:
                data = None
            now = time.time()
            if data and len(data) >= 12:
                self.last_packet = now
                packets += 1
                seq, _frame_no, line, _ = struct.unpack("<HHHH", data[:8])
                gap = None if last_seq is None else (seq - last_seq - 1) & 0xFFFF
                if gap and gap < 1000:
                    lost += gap
                elif gap:
                    # A jump this large is the firmware restarting its count.
                    self.log.write("video", "INFO", f"stream sequence restarted at {seq}")
                last_seq = seq
                frame += data[12:]
                if line & 0x8000:
                    frames += 1
                    raw = bytes(frame)
                    frame.clear()
                    digest = hashlib.blake2b(raw, digest_size=8).digest()
                    if digest == last_hash:
                        same_for += 1 / 50
                    else:
                        last_hash, same_for = digest, 0.0
                    if raw and raw.count(0) == len(raw):
                        black_since = black_since or now
                    else:
                        black_since = None
                    if now - last_png >= self.args.png_every:
                        last_png = now
                        self.save_png(raw)
            if now - window >= 1.0:
                if packets:
                    fps = frames / (now - window)
                    status = f"{fps:.1f} fps, {packets} packets, {lost} lost"
                    if now - self.last_status >= 60:
                        self.last_status = now
                        self.log.write("video", "INFO", status)
                    if fps < self.args.min_fps:
                        self.log.write("video", "ALERT", f"low frame rate: {status}",
                                       every=30, key="low fps")
                    if lost > packets // 20:
                        self.log.write("video", "WARN", f"packet loss: {status}",
                                       every=30, key="loss")
                if black_since and now - black_since > 3:
                    self.log.write("video", "ALERT", "picture all black for "
                                   f"{now - black_since:.0f}s", every=30, key="black")
                window, frames, packets, lost = now, 0, 0, 0
            held = os.path.exists(os.path.join(self.args.out, "hold"))
            if self.last_packet and now - self.last_packet > 3 and not held:
                self.log.write("video", "ALERT", "no video packets for "
                               f"{now - self.last_packet:.0f}s", every=30, key="no video")
        self.stop_stream()


# ---------------------------------------------------------------------------
class RestWatch(threading.Thread):
    def __init__(self, args, log: EventLog, stop: threading.Event, video: VideoWatch):
        super().__init__(daemon=True, name="rest")
        self.args, self.log, self.stop, self.video = args, log, stop, video

    def run(self) -> None:
        known: dict = {}
        down_since = None
        while not self.stop.is_set():
            try:
                status, body = rest(self.args.host, "/v1/info")
                info = json.loads(body)
                if down_since:
                    self.log.write("rest", "ALERT", f"back after {time.time() - down_since:.0f}s")
                    down_since = None
                for key in ("product", "firmware_version", "git_commit_hash",
                            "fpga_version", "core_version"):
                    if known.get(key) != info.get(key):
                        level = "INFO" if key not in known else "ALERT"
                        self.log.write("rest", level, f"{key} = {info.get(key)}"
                                       + ("" if key not in known else f" (was {known[key]})"))
                        known[key] = info.get(key)
                if info.get("errors"):
                    self.log.write("rest", "ALERT", f"errors: {info['errors']}", every=60)
                # <out>/hold: a test run owns the stream; watch, do not steer.
                held = os.path.exists(os.path.join(self.args.out, "hold"))
                if time.time() - self.video.last_packet > 5 and not held:
                    self.video.start_stream()
            except Exception as exc:                        # noqa: BLE001
                if down_since is None:
                    down_since = time.time()
                self.log.write("rest", "ALERT", f"/v1/info unreachable: {exc}",
                               every=30, key="unreachable")
            self.stop.wait(self.args.rest_every)


# ---------------------------------------------------------------------------
class ConsoleWatch(threading.Thread):
    def __init__(self, args, log: EventLog, stop: threading.Event):
        super().__init__(daemon=True, name="console")
        self.args, self.log, self.stop = args, log, stop
        self.text = open(os.path.join(args.out, "console.log"), "a", buffering=1)
        self.partial = ""
        self.known = 0
        # The FIFO keeps what the CPU printed before the monitor started; that
        # backlog is history, so it is logged but never raised as an alert.
        self.backlog = True
        self.wifi_rx, self.wifi_window = 0, time.time()

    def poll(self) -> None:
        import u64ii_jtag as jt
        try:
            lock = jt.DeviceLock(wait=0).__enter__()
        except jt.JtagError:
            self.log.write("console", "DEBUG", "JTAG busy (c64u lock held); skipped", every=120)
            return
        try:
            board = jt.Board(self.args.url, self.args.frequency)
            try:
                board.identify()
                if not board.design_loaded():
                    self.log.write("console", "ALERT", "user chain ID is not 0xDEAD1541 "
                                   "(FPGA not configured?)", every=30)
                    return
                data = b""
                for _ in range(8):                       # up to ~2 KB per poll
                    level, chunk = board.chain.drain(jt.USER_CONSOLE, min(self.known, 255))
                    self.known = max(0, level - len(chunk))
                    data += chunk
                    if not chunk and not level:
                        break
            finally:
                board.close()
        finally:
            lock.__exit__(None, None, None)
        if data:
            self.consume(data.decode("latin-1"))
        if self.backlog and self.known == 0:
            self.backlog = False
            self.log.write("console", "INFO", "backlog read; alerts from here on are live")

    def consume(self, text: str) -> None:
        self.text.write(text)
        # software/io/wifi/wifi_cmd.cc writes '~' to the UART for every
        # unsolicited message from the ESP32 (WiFi receive traffic). Count them
        # and keep them out of the lines.
        self.wifi_rx += text.count("~")
        now = time.time()
        if now - self.wifi_window >= 60:
            self.log.write("console", "INFO", f"ESP32 unsolicited messages: {self.wifi_rx}/min")
            self.wifi_rx, self.wifi_window = 0, now
        text = text.replace("~", "")
        lines = (self.partial + text).split("\n")
        self.partial = lines.pop()
        for line in lines:
            line = line.strip("\r ")
            if not line:
                continue
            if (CONSOLE_ALERTS.search(line) and not CONSOLE_QUIET.search(line)
                    and not self.backlog):
                # Identical alerts (digits aside) repeat at most every 5 minutes;
                # every occurrence is still in console.log.
                self.log.write("console", "ALERT", line, every=300,
                               key=re.sub(r"\d+", "#", line))
            else:
                self.log.write("console", "LOG", line)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception as exc:                        # noqa: BLE001
                self.known = 0
                self.log.write("console", "WARN", f"JTAG poll failed: {exc}", every=60)
            self.stop.wait(self.args.console_every)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", nargs="?", default=os.environ.get("U64II_VERIFY_HOST") or None,
                        help="address or name of the Ultimate (default: $U64II_VERIFY_HOST); required, "
                             "except with --stop")
    parser.add_argument("--out", default="scratch/c64u-monitor")
    parser.add_argument("--local-ip", default=None, help="interface that joins the group")
    parser.add_argument("--group", default="239.0.1.65", help="multicast group of the stream")
    parser.add_argument("--stop", action="store_true", help="stop the monitor writing to --out")
    parser.add_argument("--video-port", type=int, default=11064)
    parser.add_argument("--min-fps", type=float, default=45.0)
    parser.add_argument("--png-every", type=float, default=5.0)
    parser.add_argument("--rest-every", type=float, default=5.0)
    parser.add_argument("--console-every", type=float, default=2.0)
    parser.add_argument("--no-console", action="store_true", help="skip JTAG console polling")
    parser.add_argument("--url", default=os.environ.get("U64II_JTAG_URL", "ftdi://ftdi:232h/1"))
    parser.add_argument("--frequency", type=float, default=3e6)
    args = parser.parse_args(argv)
    if not args.host and not args.stop:
        parser.error("no host: give the Ultimate's address as HOST, or set U64II_VERIFY_HOST")
    pidfile = os.path.join(args.out, "monitor.pid")
    if args.stop:
        try:
            pid = int(open(pidfile).read())
            cmdline = open(f"/proc/{pid}/cmdline", "rb").read()
        except (OSError, ValueError):
            print(f"no running monitor for {pidfile}")
            return 1
        if b"c64u_monitor" not in cmdline:
            print(f"pid {pid} in {pidfile} is not a c64u monitor; not signalling it")
            return 1
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
        return 0
    os.makedirs(args.out, exist_ok=True)
    with open(pidfile, "w") as handle:
        handle.write(str(os.getpid()))
    args.local_ip = args.local_ip or local_address_towards(args.host)

    log = EventLog(os.path.join(args.out, "events.log"))
    log.write("monitor", "INFO", f"watching {args.host}; stream to {args.group}:"
              f"{args.video_port} via {args.local_ip}; pid {os.getpid()}")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    video = VideoWatch(args, log, stop)
    video.start_stream()
    threads = [video, RestWatch(args, log, stop, video)]
    if not args.no_console:
        threads.append(ConsoleWatch(args, log, stop))
    for thread in threads:
        thread.start()
    try:
        while not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        stop.set()
    for thread in threads:
        thread.join(timeout=5)
    log.write("monitor", "INFO", "stopped")
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
