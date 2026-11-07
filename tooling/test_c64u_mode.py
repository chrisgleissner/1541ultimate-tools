#!/usr/bin/env python3
"""Host tests for c64u_mode.py without a board.

A fake command runner stands in for openFPGALoader and powerctl: it keeps a small flash in memory (the sizes are
patched down), so a dump writes the flash into a file and a write puts a file into the flash. REST answers come
from a dict, or from a local HTTP server for the real Board.http.

    python3 -m unittest tooling.test_c64u_mode
"""

import configparser
import fcntl
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import hashlib
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c64u_mode as cm  # noqa: E402

LOG = cm.log  # the real log(); Base patches cm.log

SIZE = 64 * 1024
CODE = 16 * 1024
POWER = ("GET", "/v1/configs/U64%20Specific%20Settings/Power%20On%20After%20Power%20Loss")
OUTLETS = [{"alias": "C64U", "device_id": "ID-C64U"}, {"alias": "C64", "device_id": "ID-MICKEY"}]


def image(code: int, data: int = 0) -> bytes:
    return bytes([code]) * CODE + bytes([data]) * (SIZE - CODE)


class FakeRunner:
    def __init__(self, flash: bytes = b"", outlets=None):
        self.flash = bytearray(flash or image(1))
        self.calls = []
        self.corrupt_next_write = False
        self.unstable_reads = 0
        self.outlets = OUTLETS if outlets is None else outlets
        self.fail = set()

    def __call__(self, argv, text=True, capture_output=False):
        self.calls.append(argv)
        if argv[0] in self.fail:
            return subprocess.CompletedProcess(argv, 3, "", "")
        if argv[0] == "openFPGALoader":
            if "--dump-flash" in argv:
                data = bytearray(self.flash)
                if self.unstable_reads:
                    self.unstable_reads -= 1
                    data[5] ^= 0xFF
                with open(argv[-1], "wb") as f:
                    f.write(bytes(data))
            elif "-f" in argv:
                with open(argv[-1], "rb") as f:
                    data = bytearray(f.read())
                if self.corrupt_next_write:
                    data[100] ^= 0xFF
                    self.corrupt_next_write = False
                self.flash = data
        if argv[:2] == ["powerctl", "list"]:
            out = self.outlets if isinstance(self.outlets, str) else json.dumps(self.outlets)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self):
        return [" ".join(c[:2]) if c[0] == "powerctl" else c[0] for c in self.calls]


class FakeBoard(cm.Board):
    def __init__(self, cfg, dry_run=False, run=None, answers=None, allow_power_cycle=False, state=None):
        super().__init__(cfg, dry_run=dry_run, run=run or FakeRunner(), allow_power_cycle=allow_power_cycle,
                         state=state)
        self.answers = answers or {}
        self.requests = []
        self.notes = []

    def http(self, method, path, body=None, timeout=4.0):
        self.requests.append((method, path))
        a = self.answers.get((method, path), (None, "no answer"))
        return a() if callable(a) else a

    def notify(self, text):
        self.notes.append(text)


def config(**board):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read_dict({"board": {"host": "10.0.0.2", "outlet": "C64U", **board}, "modes": {}, "hotkeys": {}})
    return cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = cm.FlashStore(os.path.join(self.tmp.name, "flash"))
        for name, value in (("FLASH_SIZE", SIZE), ("CODE_SIZE", CODE), ("LOCK", os.path.join(self.tmp.name, "c64u.lock"))):
            p = mock.patch.object(cm, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cm.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cm, "log")
        p.start()
        self.addCleanup(p.stop)

    def save(self, name, data, stamp="20260101-000000"):
        os.makedirs(os.path.join(self.store.root, name), exist_ok=True)
        path = os.path.join(self.store.root, name, stamp + ".bin")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()


# ---- flash images ---------------------------------------------------------------------------------------------------

class FlashStoreTest(Base):
    def test_state_round_trip_and_corrupt_file(self):
        self.assertIsNone(self.store.current())
        self.store.set_current("alt")
        self.assertEqual(self.store.current(), "alt")
        self.assertEqual(self.store.get("state"), "clean")
        self.store.update(power_on_seen=5)
        self.assertEqual(self.store.get("power_on_seen"), 5)
        with open(self.store._state_path(), "w") as f:
            f.write("[1, 2]")
        self.assertEqual(self.store.get("current", "none"), "none")
        with open(self.store._state_path(), "w") as f:
            f.write("{broken")
        self.assertIsNone(self.store.current())

    def test_names_latest_and_code(self):
        self.assertEqual(self.store.names(), [])
        self.assertIsNone(self.store.latest("stock"))
        a = self.save("stock", image(1, 9), "20260101-000000")
        b = self.save("stock", image(1, 8), "20260102-000000")
        self.save("alt", image(2))
        self.assertEqual(self.store.names(), ["alt", "stock"])
        self.assertEqual(self.store.latest("stock"), b)
        self.assertEqual(self.store.code_hash(a), self.store.code_hash(b))
        self.assertTrue(os.path.isfile(a + ".code"))
        self.assertEqual(self.store.code_hash(a), self.store.code_hash(a))      # from the cache
        self.assertEqual(self.store.name_of_code(self.store.code_hash(b)), "stock")
        self.assertIsNone(self.store.name_of_code("0" * 64))

    def test_sha256_limit(self):
        path = self.save("x", image(3, 4))
        self.assertEqual(cm.sha256(path, CODE), cm.sha256(self.save("y", bytes([3]) * CODE)))
        self.assertNotEqual(cm.sha256(path), cm.sha256(path, CODE))

    def test_sha256_limit_across_chunks(self):
        path = os.path.join(self.tmp.name, "big.bin")
        with open(path, "wb") as f:
            f.write(bytes(range(256)) * 12288)                                # 3 MiB, read in 1 MiB chunks
        with open(path, "rb") as f:
            head = f.read(2621440)
        self.assertEqual(cm.sha256(path, 2621440), hashlib.sha256(head).hexdigest())


class SnapshotTest(Base):
    def board(self, flash, **kw):
        self.runner = FakeRunner(flash)
        return FakeBoard(config(), run=self.runner, state=self.store, **kw)

    def test_snapshot_saves_and_records(self):
        path = cm.snapshot(self.board(image(2, 5)), self.store, "alt")
        self.assertEqual(self.read(path), image(2, 5))
        self.assertTrue(os.path.isfile(path + ".sha256"))
        self.assertEqual(self.store.current(), "alt")

    def test_snapshot_refuses_the_code_of_another_name(self):
        self.save("stock", image(1))
        with self.assertRaisesRegex(cm.ModeError, "code of 'stock'"):
            cm.snapshot(self.board(image(1, 7)), self.store, "alt")
        self.assertEqual(self.store.images("alt"), [])
        path = cm.snapshot(self.board(image(1, 7)), self.store, "alt", force=True)
        self.assertTrue(os.path.isfile(path))

    def test_differing_reads_save_nothing(self):
        b = self.board(image(2))
        self.runner.unstable_reads = 1
        with self.assertRaisesRegex(cm.ModeError, "differ"):
            cm.snapshot(b, self.store, "alt")
        self.assertEqual(self.store.images("alt"), [])

    def test_dry_run_reads_nothing(self):
        b = self.board(image(2), dry_run=True)
        cm.snapshot(b, self.store, "alt")
        self.assertIsNone(self.store.current())

    def test_short_read_is_an_error(self):
        b = self.board(image(2))
        self.runner.flash = bytearray(10)
        with self.assertRaisesRegex(cm.ModeError, "not"):
            cm.snapshot(b, self.store, "alt")


class FlashSwapTest(Base):
    def board(self, flash, **kw):
        self.runner = FakeRunner(flash)
        return FakeBoard(config(), run=self.runner, state=self.store, **kw)

    def test_files_the_flash_by_its_code_then_writes_and_verifies(self):
        stock = self.save("stock", image(1, 1))
        self.save("alt", image(2, 2))
        self.store.set_current("stock")                                       # a stale record: the flash holds alt
        b = self.board(image(2, 3))                                           # alt code, newer settings
        self.assertTrue(cm.flash_swap(b, self.store, "stock"))
        self.assertEqual(bytes(self.runner.flash), image(1, 1))
        self.assertEqual(self.read(self.store.latest("alt")), image(2, 3), "the flash was filed under alt, by code")
        self.assertEqual(self.read(self.store.latest("stock")), self.read(stock), "stock is untouched")
        self.assertEqual(self.store.current(), "stock")
        self.assertEqual(self.store.get("state"), "clean")

    def test_unknown_code_is_kept_as_unknown(self):
        self.save("stock", image(1))
        b = self.board(image(9))
        cm.flash_swap(b, self.store, "stock")
        unknown = [n for n in self.store.names() if n.startswith("unknown-")]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(self.read(self.store.latest(unknown[0])), image(9))

    def test_already_there_only_after_reading_it(self):
        self.save("stock", image(1, 1))
        b = self.board(image(1, 1))
        self.assertFalse(cm.flash_swap(b, self.store, "stock"))
        self.assertNotIn("-f", [a for c in self.runner.calls for a in c])
        self.assertEqual(self.store.current(), "stock")

    def test_failed_readback_leaves_state_writing_and_blocks_the_next_swap(self):
        self.save("stock", image(1))
        self.save("alt", image(2))
        b = self.board(image(2))
        self.runner.corrupt_next_write = True
        with self.assertRaisesRegex(cm.ModeError, "read back"):
            cm.flash_swap(b, self.store, "stock")
        self.assertEqual(self.store.get("state"), "writing stock")
        with self.assertRaisesRegex(cm.ModeError, "did not finish"):
            cm.flash_swap(b, self.store, "stock")
        self.assertTrue(cm.flash_swap(b, self.store, "stock", force=True))
        self.assertEqual(self.store.get("state"), "clean")

    def test_missing_image_and_dry_run(self):
        b = self.board(image(1))
        with self.assertRaisesRegex(cm.ModeError, "no image named 'alt'"):
            cm.flash_swap(b, self.store, "alt")
        self.save("alt", image(2))
        b = self.board(image(1), dry_run=True)
        self.assertTrue(cm.flash_swap(b, self.store, "alt"))
        self.assertEqual(self.runner.calls, [])


# ---- the board ------------------------------------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        code, body = self.routes.get(self.path, (404, "nope"))
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

    do_PUT = do_GET

    def log_message(self, *a):
        pass


class RealHttpTest(Base):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.board = cm.Board(config(host=f"127.0.0.1:{self.server.server_port}"))

    def test_json_text_error_and_down(self):
        Handler.routes = {"/v1/info": (200, '{"firmware_version": "1.2", "product": "C64 Ultimate"}'),
                          "/text": (200, "plain"), "/v1/machine:poweroff": (200, "{}")}
        self.assertEqual(self.board.rest_info()["product"], "C64 Ultimate")
        self.assertEqual(self.board.http("GET", "/text"), (200, "plain"))
        self.assertEqual(self.board.http("GET", "/missing")[0], 404)
        self.assertEqual(self.board.http("PUT", "/v1/machine:poweroff", body=b"")[0], 200)
        self.assertEqual(self.board.wait_rest(5)["firmware_version"], "1.2")
        down = cm.Board(config(host="127.0.0.1:1"))
        self.assertIsNone(down.http("GET", "/v1/info")[0])
        self.assertIsNone(down.rest_info())

    def test_no_host_is_an_error(self):
        with self.assertRaisesRegex(cm.ModeError, "no board address"):
            cm.Board(config(host="")).http("GET", "/")


class BoardTest(Base):
    def test_run_dry_check_and_always(self):
        runner = FakeRunner()
        b = cm.Board(config(), dry_run=True, run=runner)
        self.assertEqual(b.run(["x"]).returncode, 0)
        self.assertEqual(runner.calls, [])
        b.run(["powerctl", "list", "--json"], always=True)
        self.assertEqual(len(runner.calls), 1)
        runner.fail.add("bad")
        live = cm.Board(config(), run=runner)
        with self.assertRaisesRegex(cm.ModeError, "bad failed"):
            live.run(["bad"])
        self.assertEqual(live.run(["bad"], check=False).returncode, 3)

    def test_jtag_reload_and_dry_wait(self):
        runner = FakeRunner()
        b = cm.Board(config(), run=runner)
        b.jtag_run(["--bin", "a.bin"])
        b.reload()
        self.assertTrue(runner.calls[0][0].endswith("u64ii_jtag.sh"))
        self.assertIn("-r", runner.calls[1])
        self.assertIsNone(cm.Board(config(), dry_run=True).wait_rest(1))

    def test_wait_rest_times_out(self):
        b = FakeBoard(config())
        with mock.patch.object(cm.time, "monotonic", side_effect=[0, 0, 10]):
            self.assertIsNone(b.wait_rest(5))

    def test_notify(self):
        b = cm.Board(config())
        with mock.patch.object(cm.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(cm.subprocess, "run") as run, mock.patch.dict(os.environ, {"C64U_MODE_NOTIFY": "1"}):
            b.notify("hi")
        run.assert_called_once()
        with mock.patch.object(cm.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(cm.subprocess, "run") as run, mock.patch.dict(os.environ, {"C64U_MODE_NOTIFY": "0"}):
            b.notify("quiet")
        run.assert_not_called()

    def test_log_prints_with_the_prefix(self):
        with mock.patch("builtins.print") as out:
            LOG("x")
        out.assert_called_once_with("[c64u-mode] x", flush=True)

    def test_find_current(self):
        self.assertEqual(cm._find_current({"a": {"b": {"current": "On"}}}), "On")
        self.assertIsNone(cm._find_current({"a": [1]}))


class OutletTest(Base):
    def board(self, outlets=None, **cfg):
        self.runner = FakeRunner(outlets=outlets)
        return FakeBoard(config(**cfg), run=self.runner)

    def test_deny_floor_and_configured_denials(self):
        for outlet in ("C64", "c64", "Mickey", "Mic", "Desk"):
            with self.subTest(outlet=outlet), self.assertRaisesRegex(cm.ModeError, "deny list"):
                self.board(outlet=outlet, deny_outlets="Desk").check_outlet()
        with self.assertRaisesRegex(cm.ModeError, "no outlet"):
            self.board(outlet="").check_outlet()

    def test_registry_must_name_exactly_one_outlet_with_the_right_id(self):
        self.assertEqual(self.board(outlet_id="ID-C64U").check_outlet(), "C64U")
        with self.assertRaisesRegex(cm.ModeError, "device id"):
            self.board(outlet_id="OTHER").check_outlet()
        with self.assertRaisesRegex(cm.ModeError, "0 outlets"):
            self.board(outlets=[]).check_outlet()
        with self.assertRaisesRegex(cm.ModeError, "2 outlets"):
            self.board(outlets=[{"alias": "C64U"}, {"alias": "C64U"}]).check_outlet()
        with self.assertRaisesRegex(cm.ModeError, "JSON"):
            self.board(outlets="not json").check_outlet()


class PowerCycleTest(Base):
    def board(self, answers, allow=True):
        self.runner = FakeRunner()
        return FakeBoard(config(), run=self.runner, answers=answers, allow_power_cycle=allow, state=self.store)

    def test_needs_permission(self):
        with self.assertRaisesRegex(cm.ModeError, "allow-power-cycle"):
            self.board({}, allow=False).power_cycle()

    def test_rest_poweroff_then_wait_silent_then_cut(self):
        info = iter([(200, {}), (None, "")])
        b = self.board({POWER: (200, {"x": {"current": "On"}}), ("GET", "/v1/info"): lambda: next(info)})
        b.power_cycle()
        self.assertIn(("PUT", "/v1/machine:poweroff"), b.requests)
        self.assertEqual(self.runner.commands()[-1], "powerctl cycle")
        self.assertIn("C64U", self.runner.calls[-1])
        self.assertIsNotNone(self.store.get("power_on_seen"))

    def test_rest_poweroff_without_a_state_store(self):
        info = iter([(200, {}), (None, "")])
        b = FakeBoard(config(), run=FakeRunner(), allow_power_cycle=True, state=None,
                      answers={POWER: (200, {"x": {"current": "On"}}), ("GET", "/v1/info"): lambda: next(info)})
        b.power_cycle()
        self.assertIn(("PUT", "/v1/machine:poweroff"), b.requests)

    def test_setting_off_refuses(self):
        b = self.board({POWER: (200, {"current": "Off"})})
        with self.assertRaisesRegex(cm.ModeError, "not 'On'"):
            b.power_cycle()
        self.assertNotIn("powerctl cycle", self.runner.commands())

    def test_board_that_keeps_answering_is_not_cut(self):
        b = self.board({POWER: (200, {"current": "On"}), ("GET", "/v1/info"): (200, {})})
        with mock.patch.object(cm.time, "monotonic", side_effect=[0, 0, 100]):
            with self.assertRaisesRegex(cm.ModeError, "still answers"):
                b.power_cycle()
        self.assertNotIn("powerctl cycle", self.runner.commands())

    def test_without_rest_needs_a_recent_record(self):
        b = self.board({})
        with self.assertRaisesRegex(cm.ModeError, "check-power"):
            b.power_cycle()
        self.store.update(power_on_seen=time.time() - 40 * 86400)
        with self.assertRaisesRegex(cm.ModeError, "check-power"):
            b.power_cycle()
        self.store.update(power_on_seen=time.time() - 86400)
        b.power_cycle()
        self.assertEqual(self.runner.commands()[-1], "powerctl cycle")


# ---- the lock -------------------------------------------------------------------------------------------------------

class LockTest(Base):
    def hold(self, text="pid=1 cmd=other\n"):
        h = open(cm.LOCK, "a+")
        fcntl.flock(h.fileno(), fcntl.LOCK_EX)
        h.seek(0)
        h.truncate()
        h.write(text)
        h.flush()
        self.addCleanup(h.close)
        return h

    def test_takes_writes_and_clears_the_holder(self):
        with cm.DeviceLock("alt"):
            with open(cm.LOCK) as f:
                self.assertIn(f"pid={os.getpid()} cmd=c64u-mode alt", f.read())
        with open(cm.LOCK) as f:
            self.assertEqual(f.read(), "")

    def test_refuses_when_held_and_waits_when_asked(self):
        self.hold()
        with self.assertRaisesRegex(cm.ModeError, "in use .pid=1 cmd=other"):
            with cm.DeviceLock("x"):
                pass
        with mock.patch.object(cm.time, "monotonic", side_effect=[0, 0, 0, 100]):
            with self.assertRaisesRegex(cm.ModeError, "in use"):
                with cm.DeviceLock("x", wait=10):
                    pass

    def test_ancestor_holder_and_off_switch(self):
        self.hold(f"pid={os.getppid()} cmd=with-device-locks\n")
        with cm.DeviceLock("x") as lock:
            self.assertIsNone(lock.handle)
        with mock.patch.dict(os.environ, {"C64U_MODE_LOCK": "off"}):
            with cm.DeviceLock("x") as lock:
                self.assertIsNone(lock.handle)

    def test_ancestors(self):
        self.assertIn(os.getppid(), cm._ancestors())

    def test_ancestors_stop_at_an_unreadable_process(self):
        with mock.patch("builtins.open", side_effect=OSError("gone")):
            self.assertEqual(cm._ancestors(), {os.getppid()})


# ---- modes, ensure, plugins, desktop, main ----------------------------------------------------------------------------

class ModesTest(Base):
    def ctx(self, flash=image(1), **kw):
        self.runner = FakeRunner(flash)
        b = FakeBoard(config(), run=self.runner, state=self.store, **kw)
        reg = cm.Registry()
        cm.builtin(reg, self.store)
        return cm.Context(b, self.store, config(), reg)

    def test_flash_mode_swaps_reloads_and_is_reached(self):
        self.save("stock", image(1))
        self.save("alt", image(2))
        self.save("unknown-1", image(9))
        c = self.ctx(image(1))
        self.assertNotIn("flash:unknown-1", c.registry.modes)
        self.assertTrue(c.ensure("flash:alt"))
        self.assertIn("-r", self.runner.calls[-1])
        self.assertFalse(c.ensure("flash:alt"))
        writes = sum("-f" in call for call in self.runner.calls)
        c.mode("flash:alt")                                                   # reads, finds alt, no write
        self.assertEqual(sum("-f" in call for call in self.runner.calls), writes)

    def test_mode_and_ensure_errors(self):
        c = self.ctx()
        with self.assertRaisesRegex(cm.ModeError, "snapshot stock"):
            c.mode("flash:stock")
        with self.assertRaisesRegex(cm.ModeError, "does not exist"):
            c.mode("nothing")
        with self.assertRaisesRegex(cm.ModeError, "unknown mode"):
            c.ensure("nothing")
        with self.assertRaisesRegex(cm.ModeError, "cannot tell"):
            c.ensure("reload")

    def test_ensure_waits_for_reached_and_times_out(self):
        c = self.ctx()
        state = {"n": 0}

        def reached(ctx):
            state["n"] += 1
            return state["n"] > 2
        c.registry.add("x", "x", lambda ctx, a: None, reached=reached, timeout=5)
        self.assertTrue(c.ensure("x"))
        c.registry.add("never", "n", lambda ctx, a: None, reached=lambda ctx: False, timeout=1)
        with mock.patch.object(cm.time, "monotonic", side_effect=[0, 0, 5]):
            with self.assertRaisesRegex(cm.ModeError, "did not reach"):
                c.ensure("never")
        c.board.dry_run = True
        self.assertTrue(c.ensure("never"))

    def test_jtag_and_reload_modes(self):
        c = self.ctx()
        with self.assertRaisesRegex(cm.ModeError, "after --"):
            c.mode("jtag")
        c.mode("jtag", ["--bin", "a"])
        c.mode("reload")
        self.assertTrue(self.runner.calls[0][0].endswith("u64ii_jtag.sh"))

    def test_detector_and_status(self):
        c = self.ctx(answers={("GET", "/v1/info"): (200, {"firmware_version": "1.2", "product": "C64 Ultimate"})})
        self.store.set_current("alt")
        text = cm.status(c.board, c.registry, self.store)
        self.assertIn("C64 Ultimate firmware 1.2", text)
        self.assertIn("alt (recorded; state clean)", text)
        c2 = self.ctx()
        self.assertIn("no answer", cm.status(c2.board, c2.registry, self.store))

    def test_resolve(self):
        cfg = config()
        cfg["modes"]["game"] = "plugin:x  extra"
        self.assertEqual(cm.resolve("game", cfg, None), "plugin:x")
        self.assertEqual(cm.resolve("other", cfg, None), "other")


class PluginTest(Base):
    def test_plugins_load_from_the_environment(self):
        d = os.path.join(self.tmp.name, "plugins")
        os.makedirs(d)
        with open(os.path.join(d, "demo.py"), "w") as f:
            f.write("def register(reg, cfg):\n    reg.add('demo', 'demo', lambda ctx, a: ctx.board.notify('demo ' + ' '.join(a)))\n")
        with open(os.path.join(d, "_skip.py"), "w") as f:
            f.write("raise SystemExit('loaded')\n")
        with open(os.path.join(d, "noreg.py"), "w") as f:
            f.write("X = 1\n")
        with mock.patch.dict(os.environ, {"C64U_MODE_PLUGINS": d}):
            self.assertIn(d, cm.plugin_dirs())
            reg = cm.Registry()
            loaded = cm.load_plugins(reg, config())
        self.assertEqual([os.path.basename(p) for p in loaded], ["demo.py"])
        self.assertIn("demo", reg.modes)


class DesktopTest(Base):
    def test_files_carry_the_power_consent_and_the_hotkey(self):
        cfg = config()
        cfg["modes"]["stock"] = "flash:stock"
        cfg["hotkeys"]["stock"] = "Meta+Alt+1"
        d = os.path.join(self.tmp.name, "apps")
        with mock.patch.object(cm.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(cm.subprocess, "run") as run:
            [path] = cm.install_desktop(cfg, "/usr/bin/c64u-mode", apps_dir=d)
        with open(path) as f:
            text = f.read()
        self.assertIn("Exec=/usr/bin/c64u-mode stock --allow-power-cycle", text)
        self.assertIn("X-KDE-Shortcuts=Meta+Alt+1", text)
        self.assertEqual(run.call_count, 2)                                   # kwriteconfig6 and update-desktop-database
        self.assertEqual(cm.install_desktop(cfg, "x", apps_dir=d, dry_run=True), [path])

    def test_mode_without_a_hotkey_and_no_kde_tools(self):
        cfg = config()
        cfg["modes"]["stock"] = "flash:stock"
        d = os.path.join(self.tmp.name, "apps")
        with mock.patch.object(cm.shutil, "which", return_value=None), mock.patch.object(cm.subprocess, "run") as run:
            [path] = cm.install_desktop(cfg, "/usr/bin/c64u-mode", apps_dir=d)
        with open(path) as f:
            self.assertNotIn("X-KDE-Shortcuts", f.read())
        run.assert_not_called()


class MainTest(Base):
    def setUp(self):
        super().setUp()
        self.cfg = config()
        self.cfg["modes"]["s"] = "flash:stock"
        for name, value in (("load_config", lambda: self.cfg),
                            ("FlashStore", lambda root=None: self.store)):
            p = mock.patch.object(cm, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.runner = FakeRunner(image(1))
        self.boards = []
        self.answers = {}

    def factory(self, cfg, dry_run=False, allow_power_cycle=False, state=None):
        b = FakeBoard(cfg, dry_run=dry_run, run=self.runner, answers=self.answers,
                      allow_power_cycle=allow_power_cycle, state=state)
        self.boards.append(b)
        return b

    def main(self, *argv, factory=None):
        return cm.main(list(argv), board_factory=factory or self.factory)

    def test_read_only_commands(self):
        with mock.patch("builtins.print"):
            self.assertEqual(self.main("status"), 0)
            self.assertEqual(self.main("list"), 0)
        with mock.patch.object(cm, "install_desktop") as inst:
            self.assertEqual(self.main("install-desktop", "--dry-run"), 0)
        inst.assert_called_once()

    def test_check_power(self):
        self.assertEqual(self.main("check-power"), 1)
        self.answers[POWER] = (200, {"current": "On"})
        self.assertEqual(self.main("check-power", "--dry-run"), 0)
        self.assertIsNone(self.store.get("power_on_seen"))
        self.assertEqual(self.main("check-power"), 0)
        self.assertIsNotNone(self.store.get("power_on_seen"))

    def test_snapshot_ensure_and_modes(self):
        self.assertEqual(self.main("snapshot"), 1)
        self.assertEqual(self.main("snapshot", "stock"), 0)
        self.assertEqual(self.store.current(), "stock")
        self.assertEqual(self.main("ensure"), 1)
        self.assertEqual(self.main("ensure", "s"), 0)                          # recorded and clean: nothing to do
        self.assertEqual(self.main("nothing"), 1)
        self.assertEqual(self.main("s", "--allow-power-cycle"), 0)            # reads, finds stock, no write
        self.assertTrue(self.boards[-1].allow_power_cycle)
        self.assertEqual(self.main("jtag", "--", "--bin", "a"), 0)

    def test_held_lock_refuses_and_notifies(self):
        h = open(cm.LOCK, "a+")
        fcntl.flock(h.fileno(), fcntl.LOCK_EX)
        h.write("pid=1 cmd=agent\n")
        h.flush()
        self.addCleanup(h.close)
        self.assertEqual(self.main("reload"), 1)
        self.assertIn("in use", self.boards[-1].notes[-1])

    def test_old_factory_signature(self):
        made = []

        def old(cfg, dry_run=False):
            made.append(dry_run)
            return FakeBoard(cfg, dry_run=dry_run, run=self.runner)
        with mock.patch("builtins.print"):
            self.assertEqual(self.main("status", factory=old), 0)
        self.assertEqual(made, [False])


class LoadConfigTest(unittest.TestCase):
    def test_file_and_environment(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.ini")
            with open(path, "w") as f:
                f.write("[board]\nhost = 1.2.3.4 ; comment\n")
            with mock.patch.dict(os.environ, {"C64U_HOST": ""}):
                self.assertEqual(cm.load_config(path)["board"]["host"], "1.2.3.4")
            with mock.patch.dict(os.environ, {"C64U_HOST": "5.6.7.8"}):
                cfg = cm.load_config(path)
            self.assertEqual(cfg["board"]["host"], "5.6.7.8")
            self.assertTrue(cfg.has_section("hotkeys"))


if __name__ == "__main__":
    unittest.main()
