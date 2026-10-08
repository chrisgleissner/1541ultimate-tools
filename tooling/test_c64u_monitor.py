#!/usr/bin/env python3
"""Host tests for c64u_monitor.py with every device interface faked.

The monitor talks to three things: the REST API over HTTP, the multicast VIC
stream over UDP and the CPU console over JTAG. Each is replaced by an
in-process stub, and time is a fake clock that only moves when a test moves
it, so the once-per-second judgements, the rate limits and the "for N
seconds" alerts are checked deterministically and without sleeping.

What is checked: the event log format and its rate limit, the REST helper,
the video packet accounting (frame rate, loss, sequence restarts, black
picture, missing packets, the hold file), the PNG output, the REST watcher's
version tracking and stream restarts, the console watcher's JTAG polling,
backlog handling and alert classification, and the command line, including
--stop and the shutdown sequence.

    python3 tooling/test_c64u_monitor.py
"""

import contextlib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from fractions import Fraction
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c64u_monitor as mon  # noqa: E402

T0 = 1_700_000_000.0


class FakeClock:
    """Stands in for the `time` module inside c64u_monitor."""

    def __init__(self, now=T0):
        # Exact arithmetic, so fifty steps of 0.02 s land exactly on 1 s.
        self.now = Fraction(now)
        self.localtime = time.localtime
        self.strftime = time.strftime

    def time(self):
        return float(self.now)

    def advance(self, seconds):
        self.now += Fraction(str(seconds))


class FakeStop:
    """threading.Event replacement whose wait() advances the clock.

    It stops the loop after `rounds` waits, so a watcher runs a fixed number
    of iterations.
    """

    def __init__(self, clock=None, rounds=1, on_wait=None):
        self.clock, self.rounds, self.on_wait = clock, rounds, on_wait
        self.flag = False
        self.waits = []

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.clock:
            self.clock.advance(seconds)
        if self.on_wait:
            self.on_wait(len(self.waits))
        if len(self.waits) >= self.rounds:
            self.flag = True
        return self.flag


class RecordingLog:
    def __init__(self):
        self.events = []

    def write(self, source, level, text, every=0.0, key=""):
        self.events.append((source, level, text, every, key))

    def levels(self, level):
        return [e[2] for e in self.events if e[1] == level]


def make_args(out, **overrides):
    values = dict(host="c64u", out=out, local_ip="192.168.1.2", group="239.0.1.65",
                  video_port=11064, min_fps=45.0, png_every=5.0, rest_every=5.0,
                  console_every=2.0, url="ftdi://ftdi:232h/1", frequency=3e6)
    values.update(overrides)
    return types.SimpleNamespace(**values)


# ---------------------------------------------------------------------------
class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events.log")
        self.clock = FakeClock(T0 + 0.25)
        patcher = mock.patch.object(mon, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = mon.EventLog(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.log.handle.close)

    def lines(self):
        with open(self.path) as handle:
            return handle.read().splitlines()

    def test_line_format(self):
        self.log.write("video", "ALERT", "something")
        stamp = time.strftime("%H:%M:%S", time.localtime(T0 + 0.25)) + ".250"
        self.assertEqual(self.lines(), [f"{stamp} ALERT video   something"])

    def test_without_every_every_line_is_written(self):
        for _ in range(3):
            self.log.write("rest", "INFO", "same")
        self.assertEqual(len(self.lines()), 3)

    def test_every_limits_repeats_of_the_same_text(self):
        self.log.write("rest", "ALERT", "down", every=30)
        self.clock.advance(29.9)
        self.log.write("rest", "ALERT", "down", every=30)
        self.assertEqual(len(self.lines()), 1)
        self.clock.advance(0.1)
        self.log.write("rest", "ALERT", "down", every=30)
        self.assertEqual(len(self.lines()), 2)

    def test_key_groups_different_texts(self):
        self.log.write("rest", "ALERT", "unreachable: a", every=30, key="u")
        self.log.write("rest", "ALERT", "unreachable: b", every=30, key="u")
        self.log.write("rest", "WARN", "unreachable: b", every=30, key="u")
        self.log.write("video", "ALERT", "unreachable: b", every=30, key="u")
        texts = [line.split(None, 3)[1:] for line in self.lines()]
        self.assertEqual(texts, [["ALERT", "rest", "unreachable: a"],
                                 ["WARN", "rest", "unreachable: b"],
                                 ["ALERT", "video", "unreachable: b"]])

    def test_appends_to_an_existing_log(self):
        self.log.write("a", "INFO", "one")
        second = mon.EventLog(self.path)
        self.addCleanup(second.handle.close)
        second.write("a", "INFO", "two")
        self.assertEqual([line.split()[-1] for line in self.lines()], ["one", "two"])


# ---------------------------------------------------------------------------
class FakeAnswer:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RestHelperTest(unittest.TestCase):
    def test_builds_request_and_returns_status_and_body(self):
        seen = {}

        def urlopen(request, timeout):
            seen.update(url=request.full_url, method=request.get_method(), timeout=timeout)
            return FakeAnswer(200, b'{"a": 1}')

        with mock.patch.object(mon.urllib.request, "urlopen", urlopen):
            self.assertEqual(mon.rest("dev", "/v1/info"), (200, b'{"a": 1}'))
            self.assertEqual(seen, dict(url="http://dev/v1/info", method="GET", timeout=4.0))
            mon.rest("dev", "/v1/streams/video:stop", "PUT", timeout=1.5)
            self.assertEqual(seen, dict(url="http://dev/v1/streams/video:stop",
                                        method="PUT", timeout=1.5))

    def test_errors_propagate(self):
        def urlopen(request, timeout):
            raise urllib.error.URLError("refused")

        with mock.patch.object(mon.urllib.request, "urlopen", urlopen):
            with self.assertRaises(urllib.error.URLError):
                mon.rest("dev", "/v1/info")


# ---------------------------------------------------------------------------
class FakeSock:
    """UDP socket stub. recv() plays a script of (seconds, data) steps."""

    def __init__(self, *args):
        self.args = args
        self.options, self.bound, self.timeout = [], None, None
        self.connected, self.closed = None, False
        self.script = []
        self.on_empty = None
        self.clock = None

    def setsockopt(self, *option):
        self.options.append(option)

    def bind(self, address):
        self.bound = address

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        self.connected = address

    def getsockname(self):
        return ("10.0.0.7", 54321)

    def close(self):
        self.closed = True

    def recv(self, size):
        assert size == 2048
        if not self.script:
            self.on_empty()
            raise socket.timeout()
        seconds, data = self.script.pop(0)
        self.clock.advance(seconds)
        if data is None:
            raise socket.timeout()
        return data


class FakeSocketModule:
    """The real socket module, except that socket() returns FakeSock."""

    def __init__(self):
        self.created = []

    def socket(self, *args):
        sock = FakeSock(*args)
        self.created.append(sock)
        return sock

    def gethostbyname(self, host):
        return {"c64u": "192.168.1.148"}[host]

    def __getattr__(self, name):
        return getattr(socket, name)


class LocalAddressTest(unittest.TestCase):
    def test_returns_the_source_address_of_a_udp_route(self):
        fake = FakeSocketModule()
        with mock.patch.object(mon, "socket", fake):
            self.assertEqual(mon.local_address_towards("c64u"), "10.0.0.7")
        sock, = fake.created
        self.assertEqual(sock.args, (socket.AF_INET, socket.SOCK_DGRAM))
        self.assertEqual(sock.connected, ("192.168.1.148", 80))
        self.assertTrue(sock.closed)

    def test_closes_the_socket_when_resolution_fails(self):
        fake = FakeSocketModule()
        with mock.patch.object(mon, "socket", fake):
            with self.assertRaises(KeyError):
                mon.local_address_towards("unknown")
        self.assertTrue(fake.created[0].closed)


def packet(seq, line, payload=b"\x11" * 768, frame_no=0):
    return struct.pack("<HHHH", seq, frame_no, line, 0) + b"\0" * 4 + payload


class VideoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.sockets = FakeSocketModule()
        for target, value in (("time", self.clock), ("socket", self.sockets)):
            patcher = mock.patch.object(mon, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        patcher = mock.patch.object(mon, "rest", self.fake_rest)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rest_result = (200, b"")
        self.log = RecordingLog()
        self.stop = FakeStop()

    def fake_rest(self, host, path, method="GET", timeout=4.0):
        self.calls.append((host, path, method))
        if isinstance(self.rest_result, Exception):
            raise self.rest_result
        return self.rest_result

    def watch(self, **overrides):
        video = mon.VideoWatch(make_args(self.tmp.name, **overrides), self.log, self.stop)
        video.sock.clock = self.clock
        video.sock.on_empty = self.stop.set
        video.pngs = []
        video.save_png = video.pngs.append
        return video

    def play(self, video, script):
        video.sock.script = list(script)
        video.run()


class VideoSetupTest(VideoBase):
    def test_socket_joins_the_group_on_the_given_interface(self):
        video = self.watch(group="239.0.1.70", local_ip="10.1.2.3", video_port=12000)
        self.assertEqual(video.name, "video")
        self.assertTrue(video.daemon)
        sock = video.sock
        self.assertEqual(sock.bound, ("", 12000))
        self.assertEqual(sock.timeout, 0.5)
        self.assertIn((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), sock.options)
        membership = socket.inet_aton("239.0.1.70") + socket.inet_aton("10.1.2.3")
        self.assertIn((socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership), sock.options)

    def test_start_stream_success(self):
        video = self.watch()
        self.assertTrue(video.start_stream())
        self.assertEqual(self.calls, [("c64u", "/v1/streams/video:start?ip=239.0.1.65:11064",
                                       "PUT")])
        self.assertEqual(self.log.levels("INFO"),
                         ["stream start to 239.0.1.65:11064: HTTP 200"])

    def test_start_stream_refused(self):
        self.rest_result = (500, b"")
        self.assertFalse(self.watch().start_stream())
        self.assertEqual(self.log.levels("INFO"),
                         ["stream start to 239.0.1.65:11064: HTTP 500"])

    def test_start_stream_exception_is_rate_limited_info(self):
        self.rest_result = OSError("no route")
        self.assertFalse(self.watch().start_stream())
        self.assertEqual(self.log.events,
                         [("video", "INFO", "stream start failed: no route", 30,
                           "stream start")])

    def test_stop_stream(self):
        self.watch().stop_stream()
        self.assertEqual(self.calls, [("c64u", "/v1/streams/video:stop", "PUT")])
        self.assertEqual(self.log.levels("INFO"), ["stream stopped"])

    def test_stop_stream_swallows_errors(self):
        self.rest_result = OSError("gone")
        self.watch().stop_stream()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.log.events, [])


class VideoRunTest(VideoBase):
    def frames(self, count, start_seq=0, per_frame=1, gap=0.02, payload=b"\x11" * 768):
        """`count` frames of `per_frame` packets, each frame `gap` seconds apart."""
        script, seq = [], start_seq
        for _ in range(count):
            for part in range(per_frame):
                last = part == per_frame - 1
                script.append((gap if last else 0.0,
                               packet(seq & 0xFFFF, (0x8000 if last else 0) | part, payload)))
                seq += 1
        return script

    def test_healthy_stream_logs_status_once_per_minute(self):
        video = self.watch()
        script = self.frames(50) + self.frames(50, start_seq=50)
        self.play(video, script)
        self.assertEqual(self.log.levels("INFO")[0], "50.0 fps, 50 packets, 0 lost")
        # The second window is inside the minute: no second status line.
        self.assertEqual(self.log.levels("INFO")[1:], ["stream stopped"])
        self.assertEqual(self.log.levels("ALERT"), [])
        self.assertEqual(self.log.levels("WARN"), [])
        self.assertEqual(video.last_packet, T0 + 2.0)
        # stop_stream on the way out
        self.assertEqual(self.calls[-1], ("c64u", "/v1/streams/video:stop", "PUT"))

    def test_low_frame_rate_alerts(self):
        self.play(self.watch(min_fps=45.0), self.frames(10, gap=0.1))
        alerts = [e for e in self.log.events if e[1] == "ALERT"]
        self.assertEqual(alerts, [("video", "ALERT",
                                   "low frame rate: 10.0 fps, 10 packets, 0 lost", 30,
                                   "low fps")])

    def test_packet_loss_warns_above_five_percent(self):
        script = self.frames(50)
        # Drop 3 packets out of 53: 3 > 50 // 20 == 2.
        del script[10], script[20], script[30]
        script += self.frames(3, start_seq=50)
        self.play(self.watch(min_fps=0), script)
        warns = [e for e in self.log.events if e[1] == "WARN"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0][2:], ("packet loss: 50.0 fps, 50 packets, 3 lost", 30,
                                        "loss"))

    def test_small_loss_is_tolerated(self):
        script = self.frames(50)
        del script[10], script[20]
        script += self.frames(2, start_seq=50)         # 2 lost of 50: not above 50 // 20
        self.play(self.watch(min_fps=0), script)
        self.assertEqual(self.log.levels("WARN"), [])
        self.assertIn("2 lost", self.log.levels("INFO")[0])

    def test_sequence_wraps_without_loss(self):
        self.play(self.watch(min_fps=0), self.frames(50, start_seq=0xFFF0))
        self.assertEqual(self.log.levels("INFO")[0], "50.0 fps, 50 packets, 0 lost")
        self.assertFalse(any("restarted" in t for t in self.log.levels("INFO")))

    def test_large_sequence_jump_is_a_restart_not_loss(self):
        script = self.frames(25) + self.frames(25, start_seq=5000)
        self.play(self.watch(min_fps=0), script)
        info = self.log.levels("INFO")
        self.assertIn("stream sequence restarted at 5000", info)
        self.assertIn("50.0 fps, 50 packets, 0 lost", info)
        self.assertEqual(self.log.levels("WARN"), [])

    def test_gap_of_999_counts_as_loss(self):
        script = [(0.02, packet(0, 0x8000)), (0.98, packet(1000, 0x8000))]
        self.play(self.watch(min_fps=0), script)
        self.assertIn("2 packets, 999 lost", self.log.levels("WARN")[0])

    def test_short_packets_are_ignored(self):
        video = self.watch(min_fps=0)
        self.play(video, [(1.0, b"\0" * 11)])
        self.assertEqual(video.last_packet, 0.0)
        self.assertEqual(video.pngs, [])
        self.assertEqual(self.log.levels("INFO"), ["stream stopped"])

    def test_frame_is_assembled_from_packets_and_saved(self):
        video = self.watch(min_fps=0)
        script = [(0.0, packet(0, 0, b"\x01" * 768)), (0.0, packet(1, 1, b"\x02" * 768)),
                  (0.02, packet(2, 0x8002, b"\x03" * 768)),
                  (0.0, packet(3, 0, b"\x04" * 768))]
        self.play(video, script)
        self.assertEqual(video.pngs, [b"\x01" * 768 + b"\x02" * 768 + b"\x03" * 768])

    def test_png_is_saved_at_most_every_png_every_seconds(self):
        video = self.watch(min_fps=0, png_every=0.5)
        self.play(video, self.frames(51, gap=0.02))
        # Frames end at 0.02, 0.04, ... 1.02; saves at 0.02, 0.52 and 1.02,
        # each exactly 0.5 s after the one before.
        self.assertEqual(len(video.pngs), 3)

    def status_lines(self, seconds):
        self.log, self.stop = RecordingLog(), FakeStop()
        self.play(self.watch(min_fps=0), self.frames(50 * seconds))
        return [t for t in self.log.levels("INFO") if t.endswith("lost")]

    def test_status_is_logged_again_exactly_sixty_seconds_later(self):
        # First status at 1 s; the next one is due at 61 s, not before.
        self.assertEqual(len(self.status_lines(60)), 1)
        self.assertEqual(len(self.status_lines(61)), 2)

    def test_all_black_picture_alerts_after_three_seconds(self):
        black = b"\0" * 768
        script = []
        for second in range(5):
            script += self.frames(50, start_seq=50 * second, payload=black)
        self.play(self.watch(min_fps=0), script)
        alerts = self.log.levels("ALERT")
        # Black since 0.02 s: the windows closing at 4 s and 5 s are beyond 3 s.
        # Repeats are passed on rate-limited; EventLog drops them.
        self.assertEqual(alerts, ["picture all black for 4s", "picture all black for 5s"])
        self.assertEqual({e[3:] for e in self.log.events if e[1] == "ALERT"}, {(30, "black")})

    def test_a_non_black_frame_clears_the_black_timer(self):
        # One non-zero byte is enough to make a frame not black.
        black, grey = b"\0" * 768, b"\0" * 767 + b"\x01"
        script = []
        for second in range(5):
            payload = grey if second == 2 else black
            script += self.frames(50, start_seq=50 * second, payload=payload)
        self.play(self.watch(min_fps=0), script)
        self.assertEqual(self.log.levels("ALERT"), [])

    def test_no_packets_alert_after_three_seconds(self):
        script = [(0.02, packet(0, 0x8000))] + [(0.5, None)] * 8
        self.play(self.watch(min_fps=0), script)
        # Last packet at 0.02 s; checks at 3.52 s and 4.02 s (twice) are beyond 3 s.
        self.assertEqual(self.log.levels("ALERT"), ["no video packets for 4s"] * 3)
        self.assertEqual({e[3:] for e in self.log.events if e[1] == "ALERT"},
                         {(30, "no video")})

    def test_no_packets_alert_suppressed_by_hold(self):
        open(os.path.join(self.tmp.name, "hold"), "w").close()
        script = [(0.02, packet(0, 0x8000))] + [(0.5, None)] * 8
        self.play(self.watch(min_fps=0), script)
        self.assertEqual(self.log.levels("ALERT"), [])

    def test_no_alert_before_the_first_packet(self):
        self.play(self.watch(), [(0.5, None)] * 20)
        self.assertEqual(self.log.levels("ALERT"), [])
        self.assertEqual(self.log.levels("INFO"), ["stream stopped"])

    def test_stop_already_set_only_stops_the_stream(self):
        video = self.watch()
        self.stop.set()
        video.run()
        self.assertEqual(self.calls, [("c64u", "/v1/streams/video:stop", "PUT")])


class FakeImage:
    def __init__(self, size):
        self.size = size
        self.pixels = {}

    def load(self):
        return self.pixels

    def save(self, path):
        with open(path, "wb") as handle:
            handle.write(repr(sorted(self.pixels.items())).encode())


class SavePngTest(VideoBase):
    def fake_pil(self):
        created = []

        def new(mode, size):
            self.assertEqual(mode, "RGB")
            image = FakeImage(size)
            created.append(image)
            return image

        image_module = types.SimpleNamespace(new=new)
        pil = types.ModuleType("PIL")
        pil.Image = image_module
        return pil, created

    def test_pixels_are_decoded_low_nibble_first(self):
        video = mon.VideoWatch(make_args(self.tmp.name), self.log, self.stop)
        pil, created = self.fake_pil()
        raw = bytes([0x10, 0xF2]) + b"\0" * 190 + bytes([0x5A]) + b"\0" * 191 + b"\x77" * 7
        with mock.patch.dict(sys.modules, {"PIL": pil}):
            video.save_png(raw)
        image, = created
        self.assertEqual(image.size, (384, 2))             # the 7 trailing bytes are dropped
        px = image.pixels
        self.assertEqual(len(px), 384 * 2)
        self.assertEqual(px[0, 0], mon.PALETTE[0])
        self.assertEqual(px[1, 0], mon.PALETTE[1])
        self.assertEqual(px[2, 0], mon.PALETTE[2])
        self.assertEqual(px[3, 0], mon.PALETTE[15])
        self.assertEqual(px[0, 1], mon.PALETTE[10])
        self.assertEqual(px[1, 1], mon.PALETTE[5])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "latest.png")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "latest.tmp.png")))

    def test_without_pillow_nothing_is_written(self):
        video = mon.VideoWatch(make_args(self.tmp.name), self.log, self.stop)
        with mock.patch.dict(sys.modules, {"PIL": None}):
            video.save_png(b"\x11" * 384)
        self.assertEqual(os.listdir(self.tmp.name), [])

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow not installed")
    def test_real_pillow_writes_a_readable_png(self):
        from PIL import Image
        video = mon.VideoWatch(make_args(self.tmp.name), self.log, self.stop)
        video.save_png(bytes([0x21]) * (192 * 3))
        with Image.open(os.path.join(self.tmp.name, "latest.png")) as image:
            self.assertEqual(image.size, (384, 3))
            self.assertEqual(image.getpixel((0, 2)), mon.PALETTE[1])
            self.assertEqual(image.getpixel((1, 2)), mon.PALETTE[2])


# ---------------------------------------------------------------------------
class FakeVideo:
    def __init__(self, last_packet=0.0):
        self.last_packet = last_packet
        self.starts = 0

    def start_stream(self):
        self.starts += 1
        return True


INFO = {"product": "C64 Ultimate", "firmware_version": "1.2.0", "git_commit_hash": "abc",
        "fpga_version": "121", "core_version": "1.47"}


class RestWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        patcher = mock.patch.object(mon, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = RecordingLog()
        self.answers = []
        self.paths = []
        patcher = mock.patch.object(mon, "rest", self.fake_rest)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_rest(self, host, path, method="GET", timeout=4.0):
        self.paths.append((host, path, method))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return 200, json.dumps(answer).encode() if isinstance(answer, dict) else answer

    def run_watch(self, answers, video=None, rest_every=5.0):
        self.answers = list(answers)
        video = video or FakeVideo(last_packet=T0 + 10_000)
        stop = FakeStop(self.clock, rounds=len(answers))
        watch = mon.RestWatch(make_args(self.tmp.name, rest_every=rest_every), self.log,
                              stop, video)
        self.assertEqual(watch.name, "rest")
        self.assertTrue(watch.daemon)
        watch.run()
        self.assertEqual(stop.waits, [rest_every] * len(answers))
        return video

    def test_first_answer_logs_versions_as_info(self):
        self.run_watch([INFO])
        self.assertEqual(self.paths, [("c64u", "/v1/info", "GET")])
        self.assertEqual(self.log.levels("INFO"), [
            "product = C64 Ultimate", "firmware_version = 1.2.0", "git_commit_hash = abc",
            "fpga_version = 121", "core_version = 1.47"])
        self.assertEqual(self.log.levels("ALERT"), [])

    def test_version_change_alerts_with_old_value(self):
        changed = dict(INFO, firmware_version="1.2.1", fpga_version="122")
        self.run_watch([INFO, INFO, changed])
        self.assertEqual(self.log.levels("ALERT"), ["firmware_version = 1.2.1 (was 1.2.0)",
                                                    "fpga_version = 122 (was 121)"])
        self.assertEqual(len(self.log.levels("INFO")), 5)

    def test_missing_keys_are_not_reported_until_they_appear(self):
        partial = {"product": "C64 Ultimate"}
        self.run_watch([partial, dict(partial, core_version="1.47")])
        self.assertEqual(self.log.levels("INFO"), ["product = C64 Ultimate",
                                                   "core_version = 1.47"])
        self.assertEqual(self.log.levels("ALERT"), [])

    def test_reported_errors_alert_rate_limited(self):
        self.run_watch([dict(INFO, errors=["sd card"]), dict(INFO, errors=[])])
        alerts = [e for e in self.log.events if e[1] == "ALERT"]
        self.assertEqual(alerts, [("rest", "ALERT", "errors: ['sd card']", 60, "")])

    def test_unreachable_then_back(self):
        self.run_watch([INFO, OSError("timed out"), OSError("timed out"), INFO, INFO])
        alerts = [e for e in self.log.events if e[1] == "ALERT"]
        # One "back after" line: the next good answer is not another return.
        self.assertEqual(alerts, [
            ("rest", "ALERT", "/v1/info unreachable: timed out", 30, "unreachable"),
            ("rest", "ALERT", "/v1/info unreachable: timed out", 30, "unreachable"),
            ("rest", "ALERT", "back after 10s", 0.0, "")])

    def test_bad_json_counts_as_unreachable(self):
        self.run_watch([b"<html>", INFO])
        alerts = self.log.levels("ALERT")
        self.assertTrue(alerts[0].startswith("/v1/info unreachable: Expecting value"))
        self.assertEqual(alerts[1], "back after 5s")

    def test_restarts_stream_when_video_is_silent(self):
        video = self.run_watch([INFO], video=FakeVideo(last_packet=T0 - 5.1))
        self.assertEqual(video.starts, 1)

    def test_leaves_a_live_stream_alone(self):
        video = self.run_watch([INFO], video=FakeVideo(last_packet=T0 - 5.0))
        self.assertEqual(video.starts, 0)

    def test_hold_file_prevents_stream_restart(self):
        open(os.path.join(self.tmp.name, "hold"), "w").close()
        video = self.run_watch([INFO, INFO], video=FakeVideo(last_packet=0.0))
        self.assertEqual(video.starts, 0)

    def test_no_restart_while_unreachable(self):
        video = self.run_watch([OSError("x")], video=FakeVideo(last_packet=0.0))
        self.assertEqual(video.starts, 0)


# ---------------------------------------------------------------------------
class FakeJtagError(RuntimeError):
    pass


class FakeChain:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def drain(self, register, pops):
        self.requests.append((register, pops))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeJtag:
    """Module double for u64ii_jtag, recording the order of operations."""

    USER_CONSOLE = 0xA
    JtagError = FakeJtagError

    def __init__(self, replies=(), busy=False, loaded=True, identify_error=None):
        self.trace = []
        self.busy, self.loaded, self.identify_error = busy, loaded, identify_error
        self.chain = FakeChain(replies)
        self.boards = []
        jtag = self

        class DeviceLock:
            def __init__(self, wait):
                jtag.trace.append(("lock", wait))

            def __enter__(self):
                if jtag.busy:
                    raise FakeJtagError("held")
                jtag.trace.append("locked")
                return self

            def __exit__(self, *exc):
                jtag.trace.append("unlocked")

        class Board:
            def __init__(self, url, frequency):
                jtag.trace.append(("board", url, frequency))
                self.chain = jtag.chain
                jtag.boards.append(self)

            def identify(self):
                jtag.trace.append("identify")
                if jtag.identify_error:
                    raise jtag.identify_error
                return 0x362C093

            def design_loaded(self):
                return jtag.loaded

            def close(self):
                jtag.trace.append("close")

        self.DeviceLock, self.Board = DeviceLock, Board


class ConsoleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        patcher = mock.patch.object(mon, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = RecordingLog()
        self.stop = FakeStop(self.clock)
        self.watches = []

    def watch(self, **overrides):
        watch = mon.ConsoleWatch(make_args(self.tmp.name, **overrides), self.log, self.stop)
        self.addCleanup(watch.text.close)
        self.watches.append(watch)
        return watch

    def console_text(self):
        for watch in self.watches:
            watch.text.flush()
        with open(os.path.join(self.tmp.name, "console.log"), "rb") as handle:
            return handle.read()

    def with_jtag(self, jtag):
        patcher = mock.patch.dict(sys.modules, {"u64ii_jtag": jtag})
        patcher.start()
        self.addCleanup(patcher.stop)
        return jtag


class ConsoleConsumeTest(ConsoleBase):
    def live(self):
        watch = self.watch()
        watch.backlog = False
        return watch

    def test_lines_are_logged_and_partial_lines_kept(self):
        watch = self.live()
        watch.consume("boot ok\r\nsecond")
        self.assertEqual(self.log.levels("LOG"), ["boot ok"])
        self.assertEqual(watch.partial, "second")
        watch.consume(" half\n\n  \r\n")
        self.assertEqual(self.log.levels("LOG"), ["boot ok", "second half"])
        self.assertEqual(watch.partial, "")
        self.assertEqual(self.console_text(), b"boot ok\r\nsecond half\n\n  \r\n")

    def test_alert_patterns(self):
        watch = self.live()
        alerting = ["Assertion failed", "Guru meditation", "Stack Overflow in task x",
                    "HardFault", "hard fault", "ERROR 5", "Lock", "Hello world!",
                    "Empty; waiting", "Magic 1234", "wedge detected", "Timeout on bus"]
        quiet = ["Lockdown", "terror", "errors", "boot ok"]
        watch.consume("\n".join(alerting + quiet) + "\n")
        self.assertEqual(self.log.levels("ALERT"), alerting)
        self.assertEqual(self.log.levels("LOG"), quiet)

    def test_routine_socket_noise_is_only_logged(self):
        watch = self.live()
        noise = ["Accept client 3 failed", "HTTP GET /v1/info timeout",
                 "ERROR reading from socket", "ERROR writing to socket"]
        watch.consume("\n".join(noise) + "\n")
        self.assertEqual(self.log.levels("LOG"), noise)
        self.assertEqual(self.log.levels("ALERT"), [])

    def test_alert_key_ignores_digits(self):
        watch = self.live()
        watch.consume("panic at 0x1234\n")
        self.assertEqual(self.log.events, [("console", "ALERT", "panic at 0x1234", 300,
                                            "panic at #x#")])

    def test_backlog_lines_never_alert(self):
        watch = self.watch()
        watch.consume("panic\n")
        self.assertEqual(self.log.levels("ALERT"), [])
        self.assertEqual(self.log.levels("LOG"), ["panic"])

    def test_wifi_markers_are_counted_and_stripped(self):
        watch = self.live()
        watch.consume("a~b~\n~~")
        self.assertEqual(self.log.levels("LOG"), ["ab"])
        self.assertEqual(watch.wifi_rx, 4)
        self.assertEqual(self.console_text(), b"a~b~\n~~")
        self.clock.advance(60)
        watch.consume("~c\n")
        self.assertEqual(self.log.levels("INFO"), ["ESP32 unsolicited messages: 5/min"])
        self.assertEqual((watch.wifi_rx, watch.wifi_window), (0, T0 + 60))
        self.assertEqual(self.log.levels("LOG"), ["ab", "c"])

    def test_wifi_count_not_reported_within_the_minute(self):
        watch = self.live()
        self.clock.advance(59.9)
        watch.consume("~~\n")
        self.assertEqual(self.log.levels("INFO"), [])
        self.assertEqual(watch.wifi_rx, 2)


class ConsolePollTest(ConsoleBase):
    def test_setup(self):
        watch = self.watch()
        self.assertEqual(watch.name, "console")
        self.assertTrue(watch.daemon)
        self.assertTrue(watch.backlog)
        self.assertEqual((watch.known, watch.partial), (0, ""))

    def test_busy_lock_skips_the_poll(self):
        jtag = self.with_jtag(FakeJtag(busy=True))
        self.watch().poll()
        self.assertEqual(jtag.trace, [("lock", 0)])
        self.assertEqual(self.log.events, [("console", "DEBUG",
                                            "JTAG busy (c64u lock held); skipped", 120, "")])

    def test_unconfigured_fpga_alerts_and_releases_everything(self):
        jtag = self.with_jtag(FakeJtag(loaded=False))
        watch = self.watch(url="ftdi://x/1", frequency=1e6)
        watch.poll()
        self.assertEqual(jtag.trace, [("lock", 0), "locked", ("board", "ftdi://x/1", 1e6),
                                      "identify", "close", "unlocked"])
        self.assertEqual(self.log.levels("ALERT"),
                         ["user chain ID is not 0xDEAD1541 (FPGA not configured?)"])
        self.assertTrue(watch.backlog)

    def test_drain_follows_fifo_level_and_ends_backlog(self):
        jtag = self.with_jtag(FakeJtag(replies=[(300, b"boot\n"), (295, b"x" * 255),
                                                (40, b"y" * 40 + b"\n"), (0, b"")]))
        watch = self.watch()
        watch.poll()
        # Pops requested: none until the level is known, then min(known, 255).
        self.assertEqual(jtag.chain.requests, [(0xA, 0), (0xA, 255), (0xA, 40), (0xA, 0)])
        self.assertEqual(jtag.trace[-2:], ["close", "unlocked"])
        self.assertEqual(watch.known, 0)
        self.assertFalse(watch.backlog)
        self.assertEqual(self.log.levels("LOG"), ["boot", "x" * 255 + "y" * 40])
        self.assertEqual(self.log.levels("INFO"),
                         ["backlog read; alerts from here on are live"])

    def test_drain_stops_after_eight_rounds_and_keeps_backlog(self):
        jtag = self.with_jtag(FakeJtag(replies=[(1000, b"z" * 10)] * 9))
        watch = self.watch()
        watch.poll()
        self.assertEqual(len(jtag.chain.requests), 8)
        self.assertEqual(watch.known, 990)
        self.assertTrue(watch.backlog)
        self.assertEqual(self.console_text(), b"z" * 80)
        self.assertEqual(self.log.levels("INFO"), [])

    def test_backlog_ends_once_and_later_alerts_are_live(self):
        jtag = self.with_jtag(FakeJtag(replies=[(5, b"old panic\n"), (0, b""),
                                                (0, b"new panic\n"), (0, b"")]))
        watch = self.watch()
        watch.poll()
        watch.poll()
        self.assertEqual(self.log.levels("LOG"), ["old panic"])
        self.assertEqual(self.log.levels("ALERT"), ["new panic"])
        self.assertEqual(self.log.levels("INFO"),
                         ["backlog read; alerts from here on are live"])
        self.assertEqual(len(jtag.boards), 2)

    def test_latin1_bytes_are_decoded(self):
        jtag = self.with_jtag(FakeJtag(replies=[(0, b"caf\xe9\n"), (0, b"")]))
        watch = self.watch()
        watch.poll()
        self.assertEqual(self.log.levels("LOG"), ["caf\xe9"])
        # A level of 0 with data still drains again; only no data and level 0 ends it.
        self.assertEqual(jtag.chain.requests, [(0xA, 0), (0xA, 0)])

    def test_identify_failure_still_closes_and_unlocks(self):
        jtag = self.with_jtag(FakeJtag(identify_error=FakeJtagError("no FT232H")))
        with self.assertRaises(FakeJtagError):
            self.watch().poll()
        self.assertEqual(jtag.trace[-2:], ["close", "unlocked"])

    def test_run_reports_poll_failures_and_resets_level(self):
        jtag = self.with_jtag(FakeJtag(replies=[(500, b"a"), OSError("usb gone"), (0, b""),
                                                (0, b"")]))
        self.stop.rounds = 3
        watch = self.watch(console_every=2.0)
        watch.run()
        self.assertEqual(self.stop.waits, [2.0, 2.0, 2.0])
        warns = [e for e in self.log.events if e[1] == "WARN"]
        self.assertEqual(warns, [("console", "WARN", "JTAG poll failed: usb gone", 60, "")])
        # After the failure the next poll starts from an unknown level again:
        # it pops nothing, although the failed poll had seen a level of 500.
        self.assertEqual(jtag.chain.requests, [(0xA, 0), (0xA, 255), (0xA, 0), (0xA, 0)])
        self.assertFalse(watch.backlog)


# ---------------------------------------------------------------------------
class MainStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pidfile = os.path.join(self.tmp.name, "monitor.pid")
        self.proc = {}
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                if path not in self.proc:
                    raise FileNotFoundError(path)
                return io.BytesIO(self.proc[path])
            # main() leaves the pid file to the garbage collector; hand it a copy.
            with real_open(path, *args, **kwargs) as handle:
                return io.StringIO(handle.read())

        patcher = mock.patch.object(mon, "open", fake_open, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.kill = mock.patch.object(mon.os, "kill").start()
        self.addCleanup(mock.patch.stopall)

    def stop(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mon.main(["--stop", "--out", self.tmp.name])
        return code, out.getvalue().strip()

    def write_pid(self, text):
        with open(self.pidfile, "w") as handle:
            handle.write(text)

    def test_no_pidfile(self):
        self.assertEqual(self.stop(), (1, f"no running monitor for {self.pidfile}"))
        self.kill.assert_not_called()

    def test_garbage_pidfile(self):
        self.write_pid("not a pid")
        self.assertEqual(self.stop(), (1, f"no running monitor for {self.pidfile}"))
        self.kill.assert_not_called()

    def test_dead_process(self):
        self.write_pid("4242")
        self.assertEqual(self.stop(), (1, f"no running monitor for {self.pidfile}"))
        self.kill.assert_not_called()

    def test_refuses_a_pid_that_is_not_the_monitor(self):
        self.write_pid("4242")
        self.proc["/proc/4242/cmdline"] = b"bash\0-c\0sleep 100\0"
        self.assertEqual(self.stop(), (1, f"pid 4242 in {self.pidfile} is not a c64u "
                                          "monitor; not signalling it"))
        self.kill.assert_not_called()

    def test_signals_the_monitor(self):
        self.write_pid("4242\n")
        self.proc["/proc/4242/cmdline"] = b"python3\0tooling/c64u_monitor.py\0c64u\0"
        self.assertEqual(self.stop(), (0, "sent SIGTERM to 4242"))
        self.kill.assert_called_once_with(4242, mon.signal.SIGTERM)


class MainRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "nested", "out")
        self.clock = FakeClock()
        self.created = []
        self.order = []
        self.handlers = {}
        self.stop_event = None
        test = self

        class FakeThread:
            def __init__(self, kind, args, log, stop, *rest):
                self.kind, self.args, self.log, self.stop, self.rest = \
                    kind, args, log, stop, rest
                test.created.append(self)
                test.stop_event = stop
                self.joined = None

            def start(self):
                test.order.append(("start", self.kind))
                if test.on_start:
                    test.on_start(self)

            def join(self, timeout):
                self.joined = timeout

        class Video(FakeThread):
            def __init__(self, *a):
                super().__init__("video", *a)

            def start_stream(self):
                test.order.append(("start_stream",))

        self.on_start = None
        self.real_event_log = mon.EventLog
        patches = [
            mock.patch.object(mon, "time", self.clock),
            mock.patch.object(mon, "VideoWatch", Video),
            mock.patch.object(mon, "RestWatch", lambda *a: FakeThread("rest", *a)),
            mock.patch.object(mon, "ConsoleWatch", lambda *a: FakeThread("console", *a)),
            mock.patch.object(mon, "local_address_towards", self.local_address),
            mock.patch.object(mon, "EventLog", self.event_log),
            mock.patch.object(mon.signal, "signal", self.install_handler),
            mock.patch.dict(os.environ, {"U64II_VERIFY_HOST": "envhost",
                                         "U64II_JTAG_URL": "ftdi://env/1"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def event_log(self, path):
        # main() never closes its event log; the test does.
        log = self.real_event_log(path)
        self.addCleanup(log.handle.close)
        return log

    def local_address(self, host):
        self.order.append(("local_address", host))
        return "10.9.8.7"

    def install_handler(self, signum, handler):
        self.handlers[signum] = handler

    def events(self):
        with open(os.path.join(self.out, "events.log")) as handle:
            return [line.split(None, 3)[1:] for line in handle.read().splitlines()]

    def test_sigterm_stops_and_cleans_up(self):
        pidfile = os.path.join(self.out, "monitor.pid")
        waits = []

        class BoundedEvent(FakeStop):
            # The handler sets the event before the main loop starts, so the
            # loop never waits; a broken handler fails here instead of hanging.
            def wait(inner, seconds):
                waits.append(seconds)
                if len(waits) >= 3:
                    raise AssertionError("SIGTERM did not stop the main loop")
                return inner.flag

        def on_start(thread):
            if thread.kind == "console":
                with open(pidfile) as handle:
                    self.assertEqual(handle.read(), str(os.getpid()))
                self.handlers[mon.signal.SIGTERM](mon.signal.SIGTERM, None)

        self.on_start = on_start
        with mock.patch.object(mon.threading, "Event", BoundedEvent):
            self.assertEqual(mon.main(["--out", self.out]), 0)
        self.assertTrue(self.stop_event.is_set())
        self.assertEqual(waits, [])
        self.assertEqual(self.order, [("local_address", "envhost"), ("start_stream",),
                                      ("start", "video"), ("start", "rest"),
                                      ("start", "console")])
        video, rest_watch, console = self.created
        self.assertIs(rest_watch.rest[0], video)
        args = video.args
        self.assertEqual((args.host, args.local_ip, args.group, args.video_port, args.url,
                          args.frequency, args.min_fps, args.png_every, args.rest_every,
                          args.console_every),
                         ("envhost", "10.9.8.7", "239.0.1.65", 11064, "ftdi://env/1", 3e6,
                          45.0, 5.0, 5.0, 2.0))
        self.assertTrue(all(t.joined == 5 for t in self.created))
        self.assertFalse(os.path.exists(pidfile))
        self.assertEqual(self.events(), [
            ["INFO", "monitor", f"watching envhost; stream to 239.0.1.65:11064 via "
                                f"10.9.8.7; pid {os.getpid()}"],
            ["INFO", "monitor", "stopped"]])

    def test_options_no_console_and_given_local_ip(self):
        self.on_start = lambda thread: self.stop_event.set()
        code = mon.main(["dev", "--out", self.out, "--no-console", "--local-ip", "1.2.3.4",
                         "--group", "239.0.1.99", "--video-port", "12345",
                         "--min-fps", "30", "--png-every", "2", "--rest-every", "7",
                         "--console-every", "3", "--url", "ftdi://u/2",
                         "--frequency", "1e6"])
        self.assertEqual(code, 0)
        self.assertEqual([t.kind for t in self.created], ["video", "rest"])
        self.assertNotIn(("local_address", "dev"), self.order)
        args = self.created[0].args
        self.assertEqual((args.host, args.local_ip, args.group, args.video_port, args.url,
                          args.frequency, args.min_fps, args.png_every, args.rest_every,
                          args.console_every),
                         ("dev", "1.2.3.4", "239.0.1.99", 12345, "ftdi://u/2", 1e6,
                          30.0, 2.0, 7.0, 3.0))
        self.assertIn("via 1.2.3.4", self.events()[0][2])

    def test_waits_in_one_second_steps_until_stopped(self):
        waits = []

        class CountingEvent(FakeStop):
            def wait(inner, seconds):
                waits.append(seconds)
                if len(waits) == 3:
                    inner.set()

        with mock.patch.object(mon.threading, "Event", CountingEvent):
            self.assertEqual(mon.main(["--out", self.out, "--no-console"]), 0)
        self.assertEqual(waits, [1, 1, 1])

    def test_keyboard_interrupt_stops_cleanly(self):
        class InterruptedEvent(FakeStop):
            def wait(inner, seconds):
                raise KeyboardInterrupt

        with mock.patch.object(mon.threading, "Event", InterruptedEvent):
            self.assertEqual(mon.main(["--out", self.out, "--no-console"]), 0)
        self.assertTrue(self.stop_event.is_set())
        self.assertEqual(self.events()[-1], ["INFO", "monitor", "stopped"])

    def test_missing_pidfile_at_exit_is_ignored(self):
        def on_start(thread):
            if thread.kind == "rest":
                os.unlink(os.path.join(self.out, "monitor.pid"))
                self.stop_event.set()

        self.on_start = on_start
        self.assertEqual(mon.main(["--out", self.out, "--no-console"]), 0)
        self.assertEqual(self.events()[-1], ["INFO", "monitor", "stopped"])

    def test_default_host_without_environment(self):
        self.on_start = lambda thread: self.stop_event.set()
        with mock.patch.dict(os.environ, clear=True):
            mon.main(["--out", self.out, "--no-console"])
        self.assertEqual(self.created[0].args.host, "c64u")
        self.assertEqual(self.created[0].args.url, "ftdi://ftdi:232h/1")


if __name__ == "__main__":
    unittest.main()
