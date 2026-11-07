#!/usr/bin/env python3
"""Host tests for u2_flash.py without hardware.

A fake bench stands in for the two devices. It serves the REST API of the cartridge and of the C64 Ultimate
from two local HTTP servers and replaces ftplib.FTP, and it models what the real devices do with the keys:

  * the cartridge shows a file browser overlay whose tree is the one the FTP upload writes into;
  * keys reach that overlay only through the C64 Ultimate's machine:input, and only while the C64 Ultimate's own
    menu is closed;
  * Run Update takes the cartridge's REST API down and replaces the C64 screen RAM with a scripted updater;
  * the C64 Ultimate's own menu has an F1 overlay with Power & Reset, and Power Cycle in a submenu.

The clock is replaced too, so waits and timeouts cost no time.

    python3 -m unittest tooling.test_u2_flash
"""

import contextlib
import fcntl
import ftplib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import device_lock  # noqa: E402
import u2_flash as uf  # noqa: E402

REFORMAT = "Reformat Flash Disk?"
CONFIRM = "Flash 3.15?"
RESET_CONFIG = "Reset Configuration?"
WIFI = "Want to update the WiFi Module?"
TURN_OFF = "PLEASE TURN OFF YOUR MACHINE"


class Prompt(str):
    """A screen that stays up until a key arrives."""


def happy_script():
    return [Prompt(REFORMAT), REFORMAT, "Checking the image", Prompt(CONFIRM), "Flashing FPGA 12%", "Flashing FPGA 90%",
            "Flashing Application 30%", "Flashing Application 90%", "Done!", Prompt(RESET_CONFIG),
            Prompt(WIFI), TURN_OFF]


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def put(chars, colors, row, col, text, fg=12):
    for i, ch in enumerate(text[:uf.SCREEN_WIDTH - col]):
        chars[row * uf.SCREEN_WIDTH + col + i] = ord(ch)
        colors[row * uf.SCREEN_WIDTH + col + i] = fg


def blank():
    return bytearray(b" " * uf.SCREEN_CELLS), bytearray([12]) * uf.SCREEN_CELLS


def screen_bytes(rows):
    """A menu screen from (row, col, text, fg) tuples."""
    chars, colors = blank()
    for row, col, text, fg in rows:
        put(chars, colors, row, col, text, fg)
    return bytes(chars) + bytes(colors)


class Device:
    """One REST server. `handle` returns (status, content type, body), or None to drop the connection."""

    def __init__(self, bench, name):
        self.bench, self.name = bench, name
        self.password = None
        self.down = False
        self.delay = 0.0
        self.server = None
        self.address = None

    def start(self, case):
        device = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urllib.parse.urlsplit(self.path)
                device.bench.requests.append((device.name, self.command, url.path))
                if device.delay:
                    threading.Event().wait(device.delay)
                if device.password is not None and self.headers.get("X-Password") != device.password:
                    answer = (403, "application/json", b'{"errors": ["Forbidden."]}')
                elif device.down:
                    answer = None
                else:
                    answer = device.handle(self.command, url.path, dict(urllib.parse.parse_qsl(url.query)), body)
                if answer is None:
                    self.close_connection = True
                    return
                status, ctype, payload = answer
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                with contextlib.suppress(OSError):          # the client may have timed out
                    self.wfile.write(payload)

            do_GET = do_PUT = do_POST = serve

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.address = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        case.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def json_reply(status, data):
    return status, "application/json", json.dumps(data).encode()


class Cartridge(Device):
    def handle(self, method, path, query, body):
        bench = self.bench
        if path == uf.MENU_SCREEN_PATH:
            screen = bench.cartridge_screen()
            if screen is None:
                return json_reply(404, {"errors": ["Menu screen unavailable."]})
            return 200, bench.screen_content_type, screen
        if path == uf.MENU_BUTTON_PATH and method == "PUT":
            if not bench.button_inert:
                bench.overlay_open = not bench.overlay_open
                bench.context = False
            return json_reply(200, {"errors": []})
        if path == uf.INPUT_PATH:
            return json_reply(501, {"errors": ["Not implemented."]})
        if path.startswith("/v1/files/") and path.endswith(":info"):
            return bench.files_info(urllib.parse.unquote(path[len("/v1/files/"):-len(":info")]))
        return json_reply(404, {"errors": ["Unknown."]})


class Host(Device):
    def handle(self, method, path, query, body):
        bench = self.bench
        if path == uf.MENU_SCREEN_PATH:
            screen = bench.host_screen()
            if screen is None:
                return json_reply(404, {"errors": ["Menu screen unavailable."]})
            return 200, "application/octet-stream", screen
        if path == uf.MENU_BUTTON_PATH and method == "PUT":
            bench.host_menu = None if bench.host_menu else "top"
            return json_reply(200, {"errors": []})
        if path == uf.INPUT_PATH and method == "GET":
            return json_reply(bench.input_status, {"errors": []})
        if path == uf.INPUT_PATH:
            keys = json.loads(body)["events"][0]["inputs"]
            if bench.input_fails(keys):
                return json_reply(500, {"errors": ["Internal."]})
            bench.keys.append(keys)
            bench.key(keys)
            return json_reply(200, {"errors": []})
        if path == uf.READMEM_PATH:
            return bench.readmem(query)
        return json_reply(404, {"errors": ["Unknown."]})


class FakeFTP:
    """ftplib.FTP against the bench's tree."""

    def __init__(self, bench):
        self.bench = bench
        self.cwd_path = "/"
        self.connected = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.bench.ftp_quit_error:
            raise ftplib.error_temp("421 bye")
        self.bench.ftp_events.append("closed")

    def connect(self, host, timeout=None):
        if self.bench.ftp_refuse:
            raise ConnectionRefusedError("refused")
        self.bench.ftp_events.append(("connect", host, timeout))

    def login(self, user, password):
        self.bench.ftp_events.append(("login", user, password))

    def node(self, path):
        node = self.bench.tree
        for part in [p for p in path.split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                raise ftplib.error_perm("550 no such file or directory")
            node = node[part]
        return node

    def pwd(self):
        return self.cwd_path

    def cwd(self, path):
        if not isinstance(self.node(path), dict):
            raise ftplib.error_perm("550 not a directory")
        self.cwd_path = path

    def mkd(self, path):
        parent, _, name = path.rpartition("/")
        if path in self.bench.ftp_mkd_refused:
            raise ftplib.error_perm("553 Requested action not taken")
        self.node(parent or "/")[name] = {}

    def size(self, path):
        node = self.node(path)
        if isinstance(node, dict):
            raise ftplib.error_perm("550 is a directory")
        return node + self.bench.ftp_size_error

    def delete(self, path):
        parent, _, name = path.rpartition("/")
        del self.node(parent or "/")[name]

    def storbinary(self, command, handle):
        path = command[len("STOR "):]
        parent, _, name = path.rpartition("/")
        data = handle.read()
        self.node(parent)[name] = len(data) - self.bench.ftp_truncate
        self.bench.ftp_events.append(("stor", path, len(data)))

    def rename(self, source, target):
        parent, _, name = source.rpartition("/")
        directory = self.node(parent)
        self.node(target.rpartition("/")[0])[target.rpartition("/")[2]] = directory.pop(name)

    def nlst(self, path):
        if self.bench.ftp_nlst_fails:
            raise ftplib.error_perm("550 nope")
        return list(self.node(path))


class Bench:
    """Both devices, the file tree and the key model."""

    def __init__(self, case, start_path=(), host_matches=True):
        self.requests = []
        self.keys = []
        self.tree = {"Flash": {}, "Temp": {}, "USB1": {}}
        self.path = list(start_path)
        self.cursor = 0
        self.overlay_open = False
        self.context = False
        self.save_popup = False
        self.foreign_title = False
        self.button_inert = False
        self.screen_content_type = "application/octet-stream"
        self.flashing = False
        self.script = happy_script()
        self.script_index = 0
        self.script_unreadable = 0
        self.readmem_status = 200
        self.input_status = 200
        self.input_fails = lambda keys: False
        self.host_menu = None          # None, "top", "overlay" or "sub"
        self.host_cursor = 0
        self.host_has_power_cycle = host_matches
        self.power_cycled = False
        self.files_info_override = None
        self.ftp_events = []
        self.ftp_refuse = False
        self.ftp_mkd_refused = set()
        self.ftp_size_error = 0
        self.ftp_truncate = 0
        self.ftp_quit_error = False
        self.ftp_nlst_fails = False
        self.cartridge = Cartridge(self, "cartridge")
        self.host = Host(self, "host")
        self.cartridge.start(case)
        self.host.start(case)
        patcher = mock.patch.object(uf.ftplib, "FTP", lambda: FakeFTP(self))
        patcher.start()
        case.addCleanup(patcher.stop)

    # ---- the cartridge -------------------------------------------------------------------
    def listing(self):
        node = self.tree
        for part in self.path:
            node = node[part]
        return [name for name in node if not name.endswith(".uploading")], node

    def cartridge_screen(self):
        if not self.overlay_open:
            return None
        names, node = self.listing()
        rows = [(0, 2, "*** C64 Ultimate ***" if self.foreign_title else "*** Ultimate II+L 3.15 (122) ***", 1)]
        for i, name in enumerate(names):
            text = f"{name:<14}{node[name]:>8}" if isinstance(node[name], int) else f"{name} <DIR>"
            rows.append((2 + i, 0, text, 1 if i == self.cursor and not self.context else 12))
        if self.context:
            rows.append((12, 10, "Run Update", 1))
        if self.save_popup:
            rows.append((14, 5, "Save changes to Flash?", 1))
        return screen_bytes(rows)

    def files_info(self, remote):
        if self.files_info_override is not None:
            return self.files_info_override
        node = self.tree
        for part in remote.split("/"):
            if not isinstance(node, dict) or part not in node:
                return json_reply(404, {"errors": ["not found"]})
            node = node[part]
        return json_reply(200, {"files": {"path": remote, "size": node}, "errors": []})

    def cartridge_key(self, keys):
        names, node = self.listing()
        shifted = "left_shift" in keys
        if self.save_popup:
            if keys == ["n"]:
                self.save_popup = False
                self.overlay_open = False
        elif keys[-1] == "cursor_up_down":
            self.cursor = max(0, min(len(names) - 1, self.cursor + (-1 if shifted else 1)))
        elif keys[-1] == "cursor_left_right":
            if shifted and self.path:
                left = self.path.pop()
                self.cursor = self.listing()[0].index(left)
            elif not shifted and isinstance(node[names[self.cursor]], dict):
                self.path.append(names[self.cursor])
                self.cursor = 0
        elif keys == ["return"]:
            if self.context:
                self.flashing = True
                self.cartridge.down = True
            elif isinstance(node[names[self.cursor]], int):
                self.context = True
        elif keys == ["run_stop"]:
            if self.context:
                self.context = False
            else:
                self.overlay_open = False

    # ---- the C64 Ultimate ------------------------------------------------------------------
    HOST_ENTRIES = ["Power & Reset", "Settings", "Developer"]
    HOST_SUB = ["Reset", "Reboot", "Power Cycle"]

    def host_screen(self):
        if self.host_menu is None:
            return None
        # The menu under an overlay keeps its own stale highlight at row 3.
        rows = [(0, 2, "*** C64 Ultimate ***", 1), (3, 6, "Tasks", 1)]
        if self.host_menu in ("overlay", "sub"):
            for i, name in enumerate(self.HOST_ENTRIES):
                rows.append((10 + i, 2, name, 1 if self.host_menu == "overlay" and i == self.host_cursor else 12))
        if self.host_menu == "sub":
            for i, name in enumerate(self.HOST_SUB):
                if name == "Power Cycle" and not self.host_has_power_cycle:
                    name = "Hibernate"
                rows.append((10 + i, 20, name, 1 if i == self.host_cursor else 12))
        return screen_bytes(rows)

    def host_key(self, keys):
        shifted = "left_shift" in keys
        size = len(self.HOST_SUB if self.host_menu == "sub" else self.HOST_ENTRIES)
        if keys == ["f1"] and self.host_menu == "top":
            self.host_menu, self.host_cursor = "overlay", 2
        elif keys[-1] == "cursor_up_down" and self.host_menu in ("overlay", "sub"):
            self.host_cursor = max(0, min(size - 1, self.host_cursor + (-1 if shifted else 1)))
        elif keys == ["return"] and self.host_menu == "overlay" and self.host_cursor == 0:
            self.host_menu, self.host_cursor = "sub", 0
        elif keys == ["return"] and self.host_menu == "sub" and self.HOST_SUB[self.host_cursor] == "Power Cycle":
            self.power_cycled = True
            self.host_menu = None
        elif keys == ["run_stop"]:
            self.host_menu = None

    def key(self, keys):
        if self.host_menu is not None:
            self.host_key(keys)
        elif self.flashing:
            current = self.script[self.script_index]
            if isinstance(current, Prompt) and keys in (["y"], ["n"]):
                self.script_index += 1
        elif self.overlay_open:
            self.cartridge_key(keys)

    def readmem(self, query):
        if self.readmem_status != 200:
            return json_reply(self.readmem_status, {"errors": ["Internal."]})
        if self.script_unreadable:
            self.script_unreadable -= 1
            return None
        assert query == {"address": "0400", "length": "1000"}
        item = self.script[self.script_index]
        if not isinstance(item, Prompt) and self.script_index < len(self.script) - 1:
            self.script_index += 1
        lines = [f"{line:<40}"[:40] for line in item.split("\n")]
        text = "".join(lines + [" " * 40] * (25 - len(lines)))
        return 200, "application/octet-stream", text.encode()


def write_artifact(directory, name="update_abc1234.u2l", size=uf.MIN_ARTIFACT_SIZE + 10):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(b"\x5a" * size)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="u2_flash_test_")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        patcher = mock.patch.dict(os.environ, {device_lock.LOCK_DIR_ENV: os.path.join(self.tmp, "locks")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("U2_FLASH_HOST", "U2_FLASH_C64_HOST", "U2_FLASH_PASSWORD", "U2_FLASH_C64_PASSWORD"):
            os.environ.pop(name, None)
        patcher = mock.patch.object(sys, "stdout", io.StringIO())    # the tool's progress lines
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeTime()
        patcher = mock.patch.object(uf, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.artifact = write_artifact(self.tmp)

    def bench(self, **kwargs):
        self.b = Bench(self, **kwargs)
        return self.b

    def args(self, *extra, host=True, c64=True, artifact=True):
        args = ["--branch", "main", "--date", "251107", "--git-id", "abc1234", "--dest-root", "/USB1/Firmware/latest",
                "--settle-seconds", "5"]
        if host:
            args += ["--host", self.b.cartridge.address]
        if c64:
            args += ["--c64-host", self.b.host.address]
        if artifact:
            args += ["--artifact", self.artifact]
        return args + list(extra)

    def run_main(self, argv):
        sys.stdout.seek(0)
        sys.stdout.truncate()
        code = uf.main(argv)
        self.out = sys.stdout.getvalue()
        return code

    def flash(self, *extra, **kwargs):
        return self.run_main(self.args("--confirm-flash", *extra, **kwargs))

    def assertKeysStart(self, expected):
        self.assertEqual(self.b.keys[:len(expected)], expected)


DOWN, UP, RIGHT, LEFT = uf.KEY_DOWN, uf.KEY_UP, uf.KEY_RIGHT, uf.KEY_LEFT
RETURN, RUN_STOP = uf.KEY_RETURN, uf.KEY_RUN_STOP
PATH_KEYS = [DOWN, DOWN, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT]     # USB1, Firmware, latest, main, 251107, abc1234


class UpdateFlow(Base):
    def test_a_full_update_drives_exactly_this_key_sequence(self):
        self.bench()
        code = self.flash("--confirm-power-cycle")
        self.assertEqual(code, 0, self.out)
        self.assertEqual(self.b.keys, PATH_KEYS + [
            RETURN,                  # context menu of update.u2l
            RETURN,                  # Run Update
            ["n"],                   # Reformat Flash Disk? (default No)
            ["y"],                   # Flash <version>?
            ["n"],                   # Reset Configuration? (default No)
            ["n"],                   # Want to update the WiFi Module? (default No)
            ["f1"], UP, UP, RETURN,  # F1 menu: Power & Reset
            DOWN, DOWN, RETURN,      # Power Cycle
        ])
        self.assertTrue(self.b.power_cycled)
        self.assertIn("Result:              DONE (flash confirmed complete; power-cycled", self.out)
        self.assertIn("sha256=", self.out)

    def test_keys_go_to_the_host_and_the_screen_is_read_from_the_cartridge(self):
        self.bench()
        self.flash()
        self.assertIn(("host", "POST", uf.INPUT_PATH), self.b.requests)
        self.assertNotIn(("cartridge", "POST", uf.INPUT_PATH), self.b.requests)
        self.assertIn(("cartridge", "GET", uf.MENU_SCREEN_PATH), self.b.requests)
        self.assertEqual(self.b.requests.count(("cartridge", "PUT", uf.MENU_BUTTON_PATH)), 1)

    def test_the_artifact_is_uploaded_under_the_branch_date_and_id(self):
        self.bench()
        self.assertEqual(self.run_main(self.args("--upload-only")), 0, self.out)
        directory = self.b.tree["USB1"]["Firmware"]["latest"]["main"]["251107"]["abc1234"]
        self.assertEqual(directory, {"update.u2l": uf.MIN_ARTIFACT_SIZE + 10})
        self.assertIn(("login", "anonymous", ""), self.b.ftp_events)
        self.assertIn("UPLOAD_ONLY_OK", self.out)
        self.assertEqual([r for r in self.b.requests if r[0] == "host"], [])

    def test_the_flash_is_not_confirmed_until_power_off_is_seen_three_times_and_settled(self):
        self.bench()
        self.flash("--confirm-power-cycle")
        self.assertIn("Observed the power-off instruction (3 consecutive read(s))", self.out)
        self.assertIn(5.0, self.clock.sleeps)                       # --settle-seconds
        self.assertIn("after FPGA and application flashing and 'Done!'", self.out)

    def test_without_confirm_power_cycle_the_host_is_left_alone(self):
        self.bench()
        self.assertEqual(self.flash(), 0, self.out)
        self.assertFalse(self.b.power_cycled)
        self.assertNotIn(["f1"], self.b.keys)
        self.assertIn("--confirm-power-cycle withheld", self.out)

    def test_yes_answers_are_sent_when_asked_for(self):
        self.bench()
        self.flash("--reformat-flash-disk", "--reset-config", "--update-wifi-module")
        self.assertEqual(self.b.keys[len(PATH_KEYS) + 2:], [["y"], ["y"], ["y"], ["y"]])

    def test_the_reformat_prompt_still_on_screen_is_not_taken_for_the_flash_confirmation(self):
        # The script shows the reformat text again after its key; answering that as "Flash <version>?" would
        # send a second key.
        self.bench()
        self.flash()
        self.assertEqual(self.b.keys[len(PATH_KEYS) + 2:], [["n"], ["y"], ["n"], ["n"]])

    def test_a_menu_that_is_already_open_on_the_cartridge_is_closed_first(self):
        self.bench(start_path=["USB1"])
        self.b.overlay_open = True
        self.b.save_popup = True
        self.flash()
        self.assertKeysStart([["n"]])                        # No to the save popup, never Yes
        self.assertEqual(self.b.requests.count(("cartridge", "PUT", uf.MENU_BUTTON_PATH)), 1)

    def test_a_cartridge_that_opens_in_a_subdirectory_is_walked_back_to_the_root(self):
        self.bench(start_path=["USB1"])
        self.b.tree["USB1"]["Firmware"] = {}
        self.b.overlay_open = False
        code = self.flash()
        self.assertEqual(code, 0, self.out)
        self.assertKeysStart([LEFT, RIGHT])        # LEFT leaves the cursor on USB1

    def test_a_listing_row_with_size_columns_matches_by_its_first_word(self):
        self.bench()
        self.flash()
        self.assertIn("Confirmed selection on 'update.u2l", self.out)

    def test_progress_is_followed_across_unreadable_screen_ram(self):
        self.bench()
        self.b.script_unreadable = 4
        self.assertEqual(self.flash("--confirm-power-cycle"), 0, self.out)
        self.assertTrue(self.b.power_cycled)

    def test_the_overlay_is_validated_and_backed_out_without_confirm_flash(self):
        self.bench()
        code = self.run_main(self.args())
        self.assertEqual(code, 1)
        self.assertIn("STOPPED_BEFORE_CONFIRM", self.out)
        self.assertEqual(self.b.keys, PATH_KEYS + [RETURN, RUN_STOP, RUN_STOP])   # context menu, then the overlay
        self.assertFalse(self.b.flashing)
        self.assertFalse(self.b.overlay_open)

    def test_a_cartridge_without_a_host_is_left_for_manual_completion(self):
        self.bench()
        code = self.run_main(self.args(c64=False))
        self.assertEqual(code, 0)
        self.assertIn("UPLOAD_VERIFIED_MANUAL_COMPLETION_REQUIRED", self.out)
        self.assertEqual(self.b.keys, [])

    def test_the_hosts_and_passwords_can_come_from_the_environment(self):
        self.bench()
        self.b.cartridge.password = "cart"
        self.b.host.password = "host"
        env = {"U2_FLASH_HOST": self.b.cartridge.address, "U2_FLASH_C64_HOST": self.b.host.address,
               "U2_FLASH_PASSWORD": "cart", "U2_FLASH_C64_PASSWORD": "host"}
        with mock.patch.dict(os.environ, env):
            code = self.run_main(self.args("--confirm-flash", host=False, c64=False))
        self.assertEqual(code, 0, self.out)

    def test_the_host_password_defaults_to_the_cartridge_password(self):
        self.bench()
        self.b.cartridge.password = self.b.host.password = "same"
        self.assertEqual(self.flash("--password", "same"), 0, self.out)

    def test_the_password_is_not_printed_by_help(self):
        with mock.patch.dict(os.environ, {"U2_FLASH_PASSWORD": "s3cret"}):
            result = subprocess.run([sys.executable, os.path.join(HERE, "u2_flash.py"), "--help"],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("s3cret", result.stdout)


class DryRun(Base):
    def test_a_dry_run_needs_no_host_no_network_and_no_lock(self):
        self.bench()
        code = self.run_main(["--dry-run", "--artifact", self.artifact, "--branch", "main", "--date", "251107",
                              "--git-id", "abc1234"])
        self.assertEqual(code, 0)
        self.assertIn("/USB1/Firmware/latest/main/251107/abc1234/update.u2l", self.out)
        self.assertIn("DRY_RUN_OK", self.out)
        self.assertEqual(self.b.requests, [])
        self.assertEqual(self.b.ftp_events, [])
        self.assertFalse(os.path.exists(device_lock.lock_path("c64u")))

    def test_run_as_a_script_the_dry_run_exits_zero(self):
        result = subprocess.run([sys.executable, os.path.join(HERE, "u2_flash.py"), "--dry-run", "--artifact",
                                 self.artifact, "--branch", "feat/x", "--git-id", "id1"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("/feat-x/", result.stdout)

    def test_a_bad_artifact_is_refused_before_anything_else(self):
        for name, size in (("update_a.u2l", 10), ("update_b.u2l", uf.MAX_ARTIFACT_SIZE + 1)):
            with self.subTest(name=name):
                path = write_artifact(self.tmp, name, size)
                self.assertEqual(self.run_main(["--dry-run", "--artifact", path]), 1)
                self.assertIn("is outside", self.out)

    def test_an_artifact_with_the_wrong_extension_or_none_is_refused(self):
        wrong = write_artifact(self.tmp, "update.u64")
        self.assertEqual(self.run_main(["--dry-run", "--artifact", wrong]), 1)
        self.assertIn("is not '.u2l'", self.out)
        self.assertEqual(self.run_main(["--dry-run", "--artifact", os.path.join(self.tmp, "gone.u2l")]), 1)
        self.assertIn("artifact not found", self.out)

    def test_the_artifact_is_found_in_the_repository_root(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        self.assertIsNone(uf.discover_local_artifact(root))
        self.assertEqual(self.run_main(["--dry-run", "--repo-root", root]), 1)
        self.assertIn("no U2+L (.u2l) artifact found", self.out)
        older = write_artifact(root, "update_old.u2l")
        newer = write_artifact(root, "update_new.u2l")
        os.utime(older, (1, 1))
        self.assertEqual(uf.discover_local_artifact(root), newer)
        plain = write_artifact(root, "update.u2l")
        self.assertEqual(uf.discover_local_artifact(root), plain)

    def test_branch_and_id_default_to_the_git_state_of_the_repository(self):
        root = os.path.join(self.tmp, "repo")
        os.makedirs(root)
        for cmd in (["init", "-q", "-b", "topic/one"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit",
                                                         "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-C", root, *cmd], check=True)
        head = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        write_artifact(root, "update.u2l")
        self.assertEqual(self.run_main(["--dry-run", "--repo-root", root, "--date", "251107"]), 0)
        self.assertIn(f"/topic-one/251107/{head}/update.u2l", self.out)

    def test_outside_git_the_names_fall_back_to_unknown(self):
        self.assertIsNone(uf.git_output(self.tmp, "rev-parse", "HEAD"))
        self.assertIsNone(uf.git_output("/nonexistent-directory", "status"))
        self.assertEqual(self.run_main(["--dry-run", "--repo-root", self.tmp, "--artifact", self.artifact,
                                        "--date", "251107"]), 0)
        self.assertIn("/unknown/251107/unknown/", self.out)

    def test_path_components_cannot_add_levels(self):
        self.assertEqual(uf.path_component(" a/b..c "), "a-b-c")
        self.assertEqual(uf.path_component("  "), "unknown")

    def test_the_date_defaults_to_today(self):
        self.assertEqual(self.run_main(["--dry-run", "--artifact", self.artifact, "--branch", "b",
                                        "--git-id", "i"]), 0)
        self.assertRegex(self.out, r"Date='\d{6}'")


class UsageErrors(Base):
    def usage(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            uf.main(list(argv))
        return raised.exception.code, err.getvalue()

    def test_exit_3_is_the_usage_error_never_2(self):
        # argparse exits with 2, which means "unsafe state" for this tool.
        code, err = self.usage("--no-such-option")
        self.assertEqual(code, 3)
        self.assertIn("unrecognized arguments", err)

    def test_options_that_contradict_each_other_are_refused(self):
        for argv, message in (
                (["--host", "h", "--c64-host", "c", "--upload-only", "--confirm-flash"], "mutually exclusive"),
                (["--host", "h", "--confirm-flash"], "--confirm-flash needs --c64-host"),
                (["--host", "h", "--c64-host", "c", "--confirm-power-cycle"], "needs --confirm-flash"),
                (["--c64-host", "c"], "--host (or U2_FLASH_HOST) is required")):
            with self.subTest(argv=argv):
                code, err = self.usage(*argv)
                self.assertEqual(code, 3)
                self.assertIn(message, err)


class Rest(Base):
    def test_a_get_is_repeated_after_a_dropped_connection_and_a_put_is_not(self):
        self.bench()
        rest = uf.Rest(self.b.host.address, timeout=2)
        self.b.host.down = True
        with self.assertRaises(uf.Failure) as raised:
            rest.request("GET", "/v1/x")
        self.assertIn(f"GET /v1/x on {self.b.host.address}", str(raised.exception))
        self.assertEqual(len(self.b.requests), uf.GET_ATTEMPTS)
        self.assertEqual(self.clock.sleeps, [uf.GET_RETRY_SECONDS] * (uf.GET_ATTEMPTS - 1))
        self.b.requests.clear()
        with self.assertRaises(uf.Failure):
            rest.request("PUT", "/v1/x")
        self.assertEqual(len(self.b.requests), 1)

    def test_a_slow_device_times_out(self):
        self.bench()
        self.b.host.delay = 1.0
        with self.assertRaises(uf.Failure) as raised:
            uf.Rest(self.b.host.address, timeout=0.1).request("PUT", "/v1/x")
        self.assertIn("timed out", str(raised.exception))

    def test_the_password_goes_out_as_x_password(self):
        self.bench()
        self.b.host.password = "pw"
        self.assertEqual(uf.Rest(self.b.host.address, "pw").request("GET", "/v1/x")[0], 404)
        self.assertEqual(uf.Rest(self.b.host.address, "wrong").request("GET", "/v1/x")[0], 403)
        self.assertEqual(uf.Rest(self.b.host.address).request("GET", "/v1/x")[0], 403)

    def test_menu_screen_contract(self):
        self.bench()
        rest = uf.Rest(self.b.cartridge.address)
        self.assertIsNone(rest.menu_screen())
        self.b.overlay_open = True
        self.assertEqual(len(rest.menu_screen()), 2000)
        self.b.screen_content_type = "text/plain"
        with self.assertRaises(uf.Failure):
            rest.menu_screen()
        self.b.screen_content_type = "application/octet-stream"
        with mock.patch.object(Cartridge, "handle", lambda *a: (200, "application/octet-stream", b"short")):
            with self.assertRaisesRegex(uf.Failure, "expected 2000 bytes, got 5"):
                rest.menu_screen()

    def test_a_refused_menu_button_or_key_is_a_failure(self):
        self.bench()
        rest = uf.Rest(self.b.host.address)
        self.b.host.down = False
        with mock.patch.object(Host, "handle", lambda *a: json_reply(500, {})):
            with self.assertRaisesRegex(uf.Failure, "menu_button: HTTP 500"):
                rest.press_menu_button()
            with self.assertRaisesRegex(uf.Failure, "input .*: HTTP 500"):
                rest.tap(["a"])
            self.assertFalse(rest.input_supported())

    def test_input_support_is_only_a_clean_200(self):
        self.bench()
        self.assertTrue(uf.Rest(self.b.host.address).input_supported())
        self.assertFalse(uf.Rest(self.b.cartridge.address).input_supported())

    def test_files_info_variants(self):
        self.bench()
        rest = uf.Rest(self.b.cartridge.address)
        self.b.tree["USB1"]["f"] = 7
        self.assertEqual(rest.files_info("USB1/f")["size"], 7)
        self.assertIsNone(rest.files_info("USB1/none"))
        for override, message in ((json_reply(500, {}), "files:info: HTTP 500"),
                                  ((200, "application/json", b"not json"), "malformed response"),
                                  (json_reply(200, {"files": []}), "no file record"),
                                  ((200, "application/json", b"[1]"), "malformed response")):
            with self.subTest(message=message):
                self.b.files_info_override = override
                with self.assertRaisesRegex(uf.Failure, message):
                    rest.files_info("USB1/f")

    def test_screen_ram_text_is_none_when_it_cannot_be_read(self):
        self.bench()
        rest = uf.Rest(self.b.host.address)
        self.b.script = ["Flashing"]
        self.assertTrue(rest.screen_ram_text().startswith("Flashing"))
        self.b.readmem_status = 500
        self.assertIsNone(rest.screen_ram_text())
        self.b.readmem_status = 200
        with mock.patch.object(Host, "handle", lambda *a: (200, "application/octet-stream", b"x")):
            self.assertIsNone(rest.screen_ram_text())
        self.b.host.down = True
        self.assertIsNone(rest.screen_ram_text())



class Navigation(Base):
    def menu(self, **kwargs):
        self.bench(**kwargs)
        self.b.overlay_open = True
        return uf.Menu(uf.Rest(self.b.cartridge.address), uf.Rest(self.b.host.address))

    def test_the_selected_row_is_found_by_colour_inside_the_row_range_only(self):
        body = screen_bytes([(3, 6, "stale", 1), (10, 20, "Reset", 12), (11, 20, "Power Cycle", 1)])
        screen = uf.Screen(body)
        self.assertEqual(screen.selected_row(), 3)
        self.assertEqual(screen.selected_row(row_range=(8, 17)), 11)
        self.assertEqual(screen.selected_row(min_col=17, row_range=(8, 17)), 11)
        self.assertIsNone(screen.selected_row(row_range=(12, 17)))
        # Only the first non-blank cell of a row counts.
        self.assertIsNone(uf.Screen(screen_bytes([(4, 0, "a", 12), (4, 5, "b", 1)])).selected_row())

    def test_the_cursor_is_moved_one_key_at_a_time_and_confirmed(self):
        menu = self.menu()
        screen = uf.select_row(menu, uf.first_token("USB1"), "USB1")
        self.assertEqual(self.b.keys, [DOWN, DOWN])
        self.assertEqual(screen.selected_row(), 4)
        uf.select_row(menu, uf.first_token("Flash"), "Flash")
        self.assertEqual(self.b.keys[2:], [UP, UP])

    def test_a_missing_row_is_reported_with_the_screen(self):
        menu = self.menu()
        with self.assertRaisesRegex(uf.Failure, "'nothing' not found in the listing; screen:\n.*Ultimate II\\+L"):
            uf.select_row(menu, uf.first_token("nothing"), "nothing")
        self.assertEqual(self.b.keys, [])

    def test_a_screen_without_a_selected_row_stops_the_navigation(self):
        menu = self.menu()
        body = screen_bytes([(0, 0, "title", 12), (2, 0, "Temp", 12)])
        with mock.patch.object(Bench, "cartridge_screen", lambda bench: body):
            with self.assertRaisesRegex(uf.Failure, "could not determine the cursor row"):
                uf.select_row(menu, uf.first_token("Temp"), "Temp")

    def test_keys_that_do_nothing_end_in_a_failure_after_the_step_limit(self):
        menu = self.menu()
        self.b.overlay_open = True
        with mock.patch.object(Bench, "cartridge_key", lambda *a: None):
            with self.assertRaisesRegex(uf.Failure, "could not reach 'USB1' after 3 steps"):
                uf.select_row(menu, uf.first_token("USB1"), "USB1", max_steps=3)
        self.assertEqual(len(self.b.keys), 3)

    def test_the_menu_not_being_open_is_a_failure(self):
        menu = self.menu()
        self.b.overlay_open = False
        with self.assertRaisesRegex(uf.Failure, "the menu is not open"):
            menu.screen()

    def test_the_root_is_not_found_when_left_does_nothing(self):
        menu = self.menu(start_path=["USB1"])
        self.b.tree["USB1"] = {"Firmware": {}}
        with mock.patch.object(Bench, "cartridge_key", lambda *a: None):
            with self.assertRaisesRegex(uf.Failure, "after 15 LEFT presses"):
                uf.go_to_filesystem_root(menu, "USB1")
        self.assertEqual(len(self.b.keys), 15)

    def test_the_marker_wait_reports_the_last_screen_or_that_nothing_opened(self):
        menu = self.menu()
        self.b.foreign_title = True
        with self.assertRaisesRegex(uf.Failure, r"not found; last screen:\n\*\*\* C64 Ultimate"):
            menu.wait_marker(uf.U2L_MENU_MARKER)
        self.b.overlay_open = False
        with self.assertRaisesRegex(uf.Failure, "never opened"):
            menu.wait_marker(uf.U2L_MENU_MARKER)

    def test_the_marker_wait_gives_up_after_its_timeout(self):
        menu = self.menu()
        self.b.foreign_title = True
        start = self.clock.now
        with self.assertRaises(uf.Failure):
            menu.wait_marker(uf.U2L_MENU_MARKER, timeout=3, interval=1)
        self.assertEqual(self.clock.now - start, 3)

    def test_a_menu_that_stays_closed_is_a_failure(self):
        menu = self.menu()
        self.b.overlay_open = False
        self.b.button_inert = True
        with self.assertRaisesRegex(uf.Failure, "stayed closed"):
            menu.open()

    def test_closing_answers_the_save_popup_with_no_and_otherwise_uses_run_stop(self):
        menu = self.menu()
        self.b.save_popup = True
        menu.close_from_anywhere()
        self.assertEqual(self.b.keys, [["n"]])
        self.assertFalse(self.b.overlay_open)
        menu.close_from_anywhere()                  # closed already: no key
        self.assertEqual(self.b.keys, [["n"]])
        self.b.overlay_open = True
        menu.close_from_anywhere()
        self.assertEqual(self.b.keys[1:], [RUN_STOP])

    def test_a_menu_that_ignores_keys_is_toggled_with_the_button_as_a_last_resort(self):
        menu = self.menu()
        with mock.patch.object(Bench, "cartridge_key", lambda *a: None):
            menu.close_from_anywhere()
        self.assertEqual(len(self.b.keys), uf.MENU_CLOSE_ATTEMPTS)
        self.assertFalse(self.b.overlay_open)
        self.assertEqual(self.b.requests.count(("cartridge", "PUT", uf.MENU_BUTTON_PATH)), 1)

    def test_a_menu_that_survives_the_button_is_a_failure(self):
        menu = self.menu()
        self.b.button_inert = True
        with mock.patch.object(Bench, "cartridge_key", lambda *a: None):
            with self.assertRaisesRegex(uf.Failure, "stayed open"):
                menu.close_from_anywhere()

    def test_a_menu_that_closes_on_the_last_attempt_needs_no_button(self):
        menu = self.menu()
        count = []

        def last_key_closes(bench, keys):
            count.append(keys)
            if len(count) == uf.MENU_CLOSE_ATTEMPTS:
                bench.overlay_open = False
        with mock.patch.object(Bench, "cartridge_key", last_key_closes):
            menu.close_from_anywhere()
        self.assertEqual(self.b.requests.count(("cartridge", "PUT", uf.MENU_BUTTON_PATH)), 0)


class UploadFailures(Base):
    def test_a_missing_volume_names_the_entries_at_the_ftp_root(self):
        self.bench()
        self.b.tree.pop("USB1")
        self.b.tree["USB0"] = {}
        self.b.ftp_mkd_refused.add("/USB1")
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("could not create remote directory /USB1: 553", self.out)
        self.assertIn("entries at the FTP root: Flash, Temp, USB0", self.out)

    def test_the_root_listing_is_optional(self):
        self.bench()
        self.b.ftp_mkd_refused.add("/USB1")
        self.b.tree.pop("USB1")
        self.b.ftp_nlst_fails = True
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("entries at the FTP root: (unavailable)", self.out)
        self.b.ftp_nlst_fails = False
        self.b.tree.clear()
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("entries at the FTP root: (none)", self.out)

    def test_a_directory_made_by_someone_else_in_the_meantime_is_fine(self):
        self.bench()
        ftp = FakeFTP(self.b)
        real_mkd = FakeFTP.mkd

        def racing_mkd(self, path):
            real_mkd(self, path)
            raise ftplib.error_perm("550 exists")
        with mock.patch.object(FakeFTP, "mkd", racing_mkd):
            uf.ftp_ensure_dirs(ftp, "/USB1/a")
        self.assertIn("a", self.b.tree["USB1"])

    def test_an_unreachable_cartridge_stops_the_run(self):
        self.bench()
        self.b.ftp_refuse = True
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("upload failed: FTP error: refused", self.out)

    def test_an_existing_remote_file_is_never_overwritten(self):
        self.bench()
        self.assertEqual(self.run_main(self.args("--upload-only")), 0)
        self.b.ftp_events.clear()
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("refusing to overwrite", self.out)
        self.assertNotIn("stor", [e[0] for e in self.b.ftp_events])

    def test_a_partial_upload_of_an_earlier_attempt_is_replaced(self):
        self.bench()
        self.b.tree["USB1"] = {"Firmware": {"latest": {"main": {"251107": {"abc1234": {"update.u2l.uploading": 5}}}}}}
        self.assertEqual(self.run_main(self.args("--upload-only")), 0, self.out)
        self.assertIn("Removing a partial upload", self.out)

    def test_a_size_mismatch_after_the_transfer_is_caught_and_the_name_stays_unused(self):
        self.bench()
        self.b.ftp_truncate = 1
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("FTP upload size mismatch", self.out)
        self.assertNotIn("update.u2l", self.b.tree["USB1"]["Firmware"]["latest"]["main"]["251107"]["abc1234"])

    def test_a_failing_ftp_goodbye_does_not_hide_the_upload(self):
        self.bench()
        self.b.ftp_quit_error = True
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("FTP error: 421 bye", self.out)

    def test_the_upload_must_be_visible_through_rest_with_the_same_size(self):
        self.bench()
        for override, message in ((json_reply(404, {}), "remote file not found through REST"),
                                  (json_reply(200, {"files": {"size": 1}}), "remote size 1 !="),
                                  (json_reply(500, {}), "HTTP 500")):
            with self.subTest(message=message):
                self.b.tree["USB1"].clear()
                self.b.files_info_override = override
                self.assertEqual(self.run_main(self.args("--upload-only")), 1)
                self.assertIn("upload verification failed", self.out)
                self.assertIn(message, self.out)


class FlashFailures(Base):
    def test_a_host_without_key_injection_is_refused_before_any_key(self):
        self.bench()
        self.b.input_status = 501
        self.assertEqual(self.flash(), 1)
        self.assertIn("does not support REST keyboard injection", self.out)
        self.assertEqual(self.b.keys, [])

    def test_the_menu_never_opening_is_a_safe_stop(self):
        self.bench()
        self.b.button_inert = True
        self.assertEqual(self.flash(), 1)
        self.assertIn("could not open the U2+L menu", self.out)

    def test_a_menu_that_cannot_be_closed_is_a_safe_stop(self):
        self.bench()
        self.b.overlay_open = True
        with mock.patch.object(Bench, "cartridge_key", lambda *a: None):
            self.b.button_inert = True
            self.assertEqual(self.flash(), 1)
        self.assertIn("could not open the U2+L menu: the menu stayed open", self.out)

    def test_the_hosts_own_menu_in_place_of_the_overlay_is_a_safe_stop(self):
        self.bench()
        self.b.foreign_title = True
        self.assertEqual(self.flash(), 1)
        self.assertIn("menu opened but 'Ultimate II+L' not found", self.out)
        self.assertEqual(self.b.keys, [])

    def test_a_missing_directory_stops_the_navigation(self):
        self.bench()
        self.b.tree["USB1"]["Other"] = {}
        with mock.patch.object(uf, "upload_artifact"), mock.patch.object(uf, "verify_upload"):
            self.assertEqual(self.flash(), 1)
        self.assertIn("navigation to update.u2l failed: 'Firmware' not found in the listing", self.out)
        self.assertNotIn(RETURN, self.b.keys)

    def test_a_different_screen_after_entering_a_directory_stops_the_navigation(self):
        self.bench()
        original = Bench.cartridge_key

        def switch_screens(bench, keys):
            original(bench, keys)
            if keys == RIGHT:
                bench.foreign_title = True
        with mock.patch.object(Bench, "cartridge_key", switch_screens):
            self.assertEqual(self.flash(), 1)
        self.assertIn("expected the U2+L overlay", self.out)

    def test_a_file_that_does_not_open_a_context_menu_stops_before_run_update(self):
        self.bench()
        original = Bench.cartridge_key

        def ignore_return(bench, keys):
            if keys != RETURN:
                original(bench, keys)
        with mock.patch.object(Bench, "cartridge_key", ignore_return):
            self.assertEqual(self.flash(), 1)
        self.assertIn("could not open the 'Run Update' context action: expected a context menu with 'Run Update'",
                      self.out)
        self.assertEqual(self.b.keys.count(RETURN), 1)

    def test_a_failing_back_out_after_the_dry_validation_is_ignored(self):
        self.bench()
        self.b.input_fails = lambda keys: keys == RUN_STOP
        self.assertEqual(self.run_main(self.args()), 1)
        self.assertIn("STOPPED_BEFORE_CONFIRM", self.out)

    def test_a_rest_failure_right_after_run_update_is_an_unsafe_state(self):
        self.bench()
        self.b.input_fails = lambda keys: keys == RETURN and self.b.context
        self.assertEqual(self.flash("--confirm-power-cycle"), 2)
        self.assertIn("REST failure after triggering Run Update", self.out)
        self.assertIn("Do NOT power-cycle", self.out)
        self.assertFalse(self.b.power_cycled)

    def test_losing_the_host_while_the_updater_runs_is_an_unsafe_state(self):
        self.bench()
        self.b.input_fails = lambda keys: keys == ["n"]
        self.assertEqual(self.flash("--confirm-power-cycle"), 2)
        self.assertIn("REST failure after triggering Run Update", self.out)
        self.assertFalse(self.b.power_cycled)

    def test_danger_text_during_the_flash_is_an_unsafe_state(self):
        for word in ("Verify error 12", "write FAILED", "abort", "Problem", "retry 3"):
            with self.subTest(word=word):
                self.bench()
                self.b.script = [Prompt(CONFIRM), "Flashing FPGA", word]
                self.assertEqual(self.flash("--confirm-power-cycle"), 2)
                self.assertIn("danger text", self.out)
                self.assertIn("UNSAFE/UNKNOWN", self.out)
                self.assertFalse(self.b.power_cycled)

    def test_power_off_text_without_the_progress_markers_is_an_unsafe_state(self):
        for script, seen in (([TURN_OFF], "[]"), (["Flashing FPGA", "Done!", TURN_OFF], "['fpga']"),
                             (["Flashing FPGA", "Flashing Application", TURN_OFF], "['app', 'fpga']")):
            with self.subTest(seen=seen):
                self.bench()
                self.b.script = script
                self.assertEqual(self.flash("--confirm-power-cycle"), 2)
                self.assertIn(f"progress markers are incomplete (seen: {seen})", self.out)
                self.assertFalse(self.b.power_cycled)

    def test_a_flash_that_never_completes_times_out_unsafe_without_a_power_cycle(self):
        self.bench()
        self.b.script = [Prompt(CONFIRM), "Flashing FPGA", "Flashing Application 50%"]
        self.assertEqual(self.flash("--confirm-power-cycle", "--flash-timeout", "30"), 2)
        self.assertIn("flash monitoring timed out after 30.0s", self.out)
        self.assertFalse(self.b.power_cycled)

    def test_unreadable_screen_ram_until_the_deadline_times_out(self):
        self.bench()
        self.b.readmem_status = 500
        self.assertEqual(self.flash("--flash-timeout", "20"), 2)
        self.assertIn("timed out", self.out)

    def test_the_power_off_text_must_be_seen_on_consecutive_reads(self):
        self.bench()
        self.b.script = ["Flashing FPGA", "Flashing Application", "Done!", TURN_OFF, "Flashing Application",
                         TURN_OFF, "Flashing Application", "Flashing Application 80%"]
        self.assertEqual(self.flash("--flash-timeout", "40"), 2)
        self.assertIn("timed out", self.out)


class PowerCycle(Base):
    def test_a_host_menu_without_the_expected_entry_is_left_alone(self):
        self.bench(host_matches=False)
        self.assertEqual(self.flash("--confirm-power-cycle"), 0, self.out)
        self.assertFalse(self.b.power_cycled)
        self.assertIn("power cycle of", self.out)
        self.assertIn("'Power Cycle' not found in the listing", self.out)
        self.assertIn("Power-cycle the host by hand", self.out)
        self.assertFalse(self.b.keys[-1] == RETURN and self.b.host_menu is None)

    def test_the_host_menu_is_opened_from_the_closed_state(self):
        self.bench()
        self.flash("--confirm-power-cycle")
        self.assertIn(("host", "PUT", uf.MENU_BUTTON_PATH), self.b.requests)
        self.assertIn("Observed the power-off instruction", self.out)
        self.assertIn("[F1 menu]", self.out)
        self.assertIn("[Power & Reset submenu]", self.out)

    def test_a_host_menu_that_is_open_is_closed_before_the_power_cycle(self):
        self.bench()
        self.b.host_menu = "top"
        report = uf.Report()
        args = mock.Mock(confirm_power_cycle=True, c64_host=self.b.host.address)
        self.assertEqual(uf.power_cycle(uf.Rest(self.b.host.address), report, args), 0)
        self.assertEqual(self.b.keys[0], RUN_STOP)
        self.assertTrue(self.b.power_cycled)
        self.assertTrue(report.reboot_performed)


class Locking(Base):
    def lock_text(self):
        with open(device_lock.lock_path("c64u")) as handle:
            return handle.read()

    def test_the_run_holds_the_c64u_lock_with_a_holder_line_and_releases_it(self):
        self.bench()
        seen = []
        real = uf.run

        def spy(args):
            seen.append(self.lock_text())
            return real(args)
        with mock.patch.object(uf, "run", spy):
            self.assertEqual(self.run_main(self.args("--upload-only")), 0)
        self.assertRegex(seen[0], rf"^pid={os.getpid()} device=c64u cmd=u2_flash.py upload-only 127\.0\.0\.1:\d+ since=\S+\n$")
        self.assertEqual(self.lock_text(), "")

    def test_a_lock_held_by_someone_else_refuses_without_touching_a_device(self):
        self.bench()
        other = device_lock.acquire(device_lock.lock_path("c64u"), 0, "c64u", "u64ii_jtag.py probe")
        self.addCleanup(device_lock.release, other)
        self.assertEqual(self.run_main(self.args("--upload-only")), 1)
        self.assertIn("The C64U is in use (pid=", self.out)
        self.assertIn("u64ii_jtag.py probe", self.out)
        self.assertEqual(self.b.requests, [])
        self.assertEqual(self.b.ftp_events, [])
        self.assertIn("u64ii_jtag.py probe", self.lock_text())      # the other holder's line survives

    def test_a_lock_held_by_an_ancestor_covers_the_run(self):
        self.bench()
        path = device_lock.lock_path("c64u")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write(f"pid={os.getppid()} device=c64u cmd=with_device_locks.py since=2000-01-01T00:00:00\n")
        holder = open(path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        self.addCleanup(holder.close)
        self.assertEqual(self.run_main(self.args("--upload-only")), 0, self.out)
        holder.seek(0)
        self.assertIn("with_device_locks.py", holder.read())        # untouched

    def test_a_wait_gives_the_other_holder_time_to_finish(self):
        self.bench()
        other = device_lock.acquire(device_lock.lock_path("c64u"), 0, "c64u", "something")
        timer = threading.Timer(0.5, device_lock.release, [other])
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(self.run_main(self.args("--upload-only", "--wait", "10")), 0, self.out)

    def test_an_interrupt_is_an_unsafe_state_and_releases_the_lock(self):
        self.bench()
        with mock.patch.object(uf, "run", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(self.args()), 2)
        self.assertIn("Interrupted", self.out)
        self.assertEqual(self.lock_text(), "")


if __name__ == "__main__":
    unittest.main()
