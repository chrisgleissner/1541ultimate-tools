#!/usr/bin/env python3
"""Host tests for u64ii_menu.py without a device.

The VT100 screen model is fed byte strings directly. serve() runs in a thread
of the test process, with a socketpair standing in for the Telnet link and a
stand-in `os` module whose _exit raises SystemExit, so a session that ends
finishes its thread instead of the test process. client() talks to a real
Unix socket, and main() runs with client() and the sleeps mocked.

    python3 tooling/test_u64ii_menu.py
"""

import contextlib
import gc
import io
import os
import runpy
import socket
import sys
import tempfile
import threading
import time
import traceback
import unittest
import warnings
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import u64ii_menu as menu  # noqa: E402
from u64ii_menu import Screen  # noqa: E402


class ScreenTest(unittest.TestCase):
    def test_cursor_position_and_clear(self):
        s = Screen()
        s.feed(b"junk\x1bc\x1b[0;37;1m\x1b[2;5Hhello\x1b[1;1Hx")
        self.assertEqual(s.text().split("\n"), ["x", "    hello"])

    def test_line_drawing_and_charset_switch(self):
        s = Screen()
        s.feed(b"\x1b(0qq\x1b(Bq")
        self.assertEqual(s.text(), "--q")

    def test_telnet_negotiation_and_split_escape(self):
        s = Screen()
        s.feed(b"\xff\xfe\x22\xff\xfb\x01\x1b[3")
        s.feed(b";4Hz")
        self.assertEqual(s.text().split("\n")[2], "   z")

    def test_erase_to_end_of_line(self):
        s = Screen()
        s.feed(b"abcdef\x1b[1;3H\x1b[K")
        self.assertEqual(s.text(), "ab")


def screen(*chunks):
    s = Screen()
    for chunk in chunks:
        s.feed(chunk)
    return s


class ScreenControlTest(unittest.TestCase):
    def test_carriage_return_line_feed_and_backspace(self):
        # LF keeps the column; two backspaces walk back to column 0 and stop there.
        self.assertEqual(screen(b"abc\rX\nY\x08\x08\x08Z").text(), "Xbc\nZY")

    def test_line_feed_stops_at_the_bottom_row(self):
        s = screen(b"\n" * 40 + b"end")
        self.assertEqual(s.row, menu.ROWS - 1)
        self.assertEqual(s.text().split("\n")[-1], "end")
        self.assertEqual(len(s.text().split("\n")), menu.ROWS)

    def test_other_control_characters_are_ignored(self):
        self.assertEqual(screen(b"a\x07\x00\x0cb").text(), "ab")

    def test_long_line_is_clipped_not_wrapped(self):
        s = screen(bytes(65 + i % 26 for i in range(85)))
        self.assertEqual(s.text(), "".join(chr(65 + i % 26) for i in range(menu.COLS)))
        self.assertEqual(s.col, menu.COLS)

    def test_line_drawing_set(self):
        self.assertEqual(screen(b"\x1b(0lqkxmjtuvwn~").text(), "+-+|+++++++~")

    def test_g1_designation_leaves_g0_alone(self):
        self.assertEqual(screen(b"\x1b)0q").text(), "q")

    def test_full_reset_leaves_graphics_mode(self):
        self.assertEqual(screen(b"\x1b(0\x1bcq").text(), "q")

    def test_two_byte_escapes_are_skipped(self):
        self.assertEqual(screen(b"a\x1b7b\x1b=c").text(), "abc")

    def test_text_keeps_inner_blank_lines_and_drops_trailing_ones(self):
        self.assertEqual(screen(b"\x1b[3;1Hx  \x1b[5;1H ").text(), "\n\nx")

    def test_blank_screen_is_empty_text(self):
        self.assertEqual(Screen().text(), "")


class ScreenSplitTest(unittest.TestCase):
    def test_telnet_command_split_after_iac(self):
        self.assertEqual(screen(b"A\xff", b"\xfb\x01B").text(), "AB")

    def test_telnet_command_split_before_option(self):
        s = screen(b"A\xff\xfb")
        self.assertEqual(s.pending, b"\xff\xfb")
        s.feed(b"\x01B")
        self.assertEqual(s.text(), "AB")

    def test_lone_escape_at_the_end(self):
        self.assertEqual(screen(b"A\x1b", b"[2;1HB").text(), "A\nB")

    def test_charset_designation_split(self):
        self.assertEqual(screen(b"\x1b(", b"0q").text(), "-")

    def test_csi_without_final_byte_waits(self):
        s = screen(b"x\x1b[12;")
        self.assertEqual(s.pending, b"\x1b[12;")
        s.feed(b"3Hy")
        self.assertEqual(s.text().split("\n")[11], "  y")


class ScreenCsiTest(unittest.TestCase):
    def test_bare_cursor_position_homes(self):
        self.assertEqual(screen(b"abc\x1b[HX").text(), "Xbc")

    def test_f_is_cursor_position_too(self):
        self.assertEqual(screen(b"\x1b[3;2fQ").text(), "\n\n Q")

    def test_cursor_position_clamps_to_the_screen(self):
        s = screen(b"\x1b[99;99HZ")
        self.assertEqual((s.row, s.col), (menu.ROWS - 1, menu.COLS))
        self.assertEqual(s.grid[menu.ROWS - 1][menu.COLS - 1], "Z")
        s.feed(b"\x1b[0;0HY")
        self.assertEqual(s.grid[0][0], "Y")

    def test_non_numeric_parameter_reads_as_zero(self):
        s = screen(b"\x1b[:;3HQ")
        self.assertEqual(s.text(), "  Q")

    def test_private_mode_sequences_are_ignored(self):
        self.assertEqual(screen(b"a\x1b[?25lb\x1b[?25hc").text(), "abc")

    def test_private_marker_is_dropped_from_the_parameters(self):
        # ?2J (selective erase of the whole display) clears like 2J; with the
        # "?" kept, the parameter would not parse and read as 0, which erases nothing.
        self.assertEqual(screen(b"abc\x1b[?2Jx").text(), "x")

    def test_erase_display_only_clears_on_mode_2(self):
        self.assertEqual(screen(b"abc\x1b[J\x1b[0J\x1b[1J").text(), "abc")
        s = screen(b"\x1b(0abc\x1b[5;5H\x1b[2Jq")
        self.assertEqual(s.text(), "q")              # cursor home, graphics off

    def test_erase_line_modes(self):
        self.assertEqual(screen(b"abcdef\x1b[1;3H\x1b[1K").text(), "   def")
        self.assertEqual(screen(b"abcdef\nxy\x1b[1;3H\x1b[2K").text(), "\n      xy")
        self.assertEqual(screen(b"abcdef\x1b[1;3H\x1b[3K").text(), "abcdef")
        self.assertEqual(screen(b"abcdef\x1b[1;3H\x1b[0K").text(), "ab")

    def test_erase_line_at_the_right_margin(self):
        s = screen(b"x" * 80 + b"\x1b[1K")
        self.assertEqual(s.text(), "")


class HelpersTest(unittest.TestCase):
    def test_socket_path_uses_the_runtime_dir(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/7"}):
            self.assertEqual(menu.socket_path("10.0.0.5"), "/run/user/7/u64ii-menu-10.0.0.5.sock")
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            self.assertEqual(menu.socket_path("c64u"), "/tmp/u64ii-menu-c64u.sock")

    def test_key_names_and_literal_text(self):
        self.assertEqual(menu.text_bytes("down"), b"\x1b[B")
        self.assertEqual(menu.text_bytes("f7"), b"\x1b[18~")
        self.assertEqual(menu.text_bytes("text:LOAD\"*\",8"), b"LOAD\"*\",8")
        self.assertEqual(menu.text_bytes("text:"), b"")
        with self.assertRaises(KeyError):
            menu.text_bytes("jump")


def ignore_leaked_sockets(test):
    """client() and serve() leave their sockets to process exit, which tests do not reach.

    Sockets the tests open themselves are closed by the tests.
    """
    test.enterContext(warnings.catch_warnings())
    warnings.simplefilter("ignore", ResourceWarning)
    test.addCleanup(gc.collect)


class ModuleProxy:
    """Stands in for a module inside u64ii_menu: the given names are replaced,
    every other attribute is the real module's. Patching the real module would
    reach every thread in the process."""

    def __init__(self, module, **replaced):
        self._module = module
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(self._module, name)


class ServeTest(unittest.TestCase):
    """serve() in a thread, with a socketpair standing in for the Telnet link."""

    host = "c64u.test"

    def setUp(self):
        ignore_leaked_sockets(self)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.device, link = socket.socketpair()
        self.addCleanup(self.device.close)
        self.addCleanup(link.close)
        self.threads = []
        threads = self.threads

        class RecordingThread(threading.Thread):
            def start(self):
                threads.append(self)
                super().start()

        self.Thread = RecordingThread
        self.connect = mock.Mock(return_value=link)
        # serve() ends the process with os._exit; here it ends only the thread.
        self.exit = mock.Mock(side_effect=SystemExit)
        patches = [mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.dir.name}),
                   mock.patch.object(menu, "socket",
                                     ModuleProxy(socket, create_connection=self.connect)),
                   mock.patch.object(menu, "threading",
                                     ModuleProxy(threading, Thread=RecordingThread))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os_patch = mock.patch.object(menu, "os", ModuleProxy(os, _exit=self.exit))
        os_patch.start()
        self.addCleanup(self.restore_os, os_patch)
        self.path = menu.socket_path(self.host)

    def restore_os(self, os_patch):
        # A thread that outlived shutdown() would reach the real os._exit and
        # end the whole test run; it keeps the stand-in instead.
        if not any(thread.is_alive() for thread in self.threads):
            os_patch.stop()

    def start(self):
        server = self.Thread(target=menu.serve, args=(self.host,), daemon=True)
        server.start()
        self.addCleanup(self.shutdown, server)
        # Ready once a request is answered: before that the path may be
        # missing, a stale file, or bound but not yet listening.
        deadline = time.monotonic() + 5
        while True:
            try:
                menu.client(self.host, "show")
                return server
            except (FileNotFoundError, ConnectionRefusedError):
                self.assertTrue(server.is_alive(), "serve ended before it answered")
                self.assertLess(time.monotonic(), deadline, "serve never answered")
                time.sleep(0.01)

    def shutdown(self, server):
        # The device side first: the pump thread sees EOF and ends while the
        # stand-in _exit is still installed.
        self.device.close()
        deadline = time.monotonic() + 5
        while server.is_alive() and time.monotonic() < deadline:
            try:
                menu.client(self.host, "stop")
            except OSError:
                pass                                        # already gone, or going
            server.join(0.5)
        for thread in self.threads:
            thread.join(5)
        self.assertEqual([t for t in self.threads if t.is_alive()], [],
                         "serve threads still running after shutdown")

    def show_until(self, text):
        deadline = time.monotonic() + 5
        while True:
            out = menu.client(self.host, "show")
            if text in out or time.monotonic() > deadline:
                return out
            time.sleep(0.01)

    def test_connects_to_telnet_and_shows_the_screen(self):
        self.start()
        self.connect.assert_called_once_with((self.host, 23), timeout=10)
        self.device.sendall(b"\xff\xfd\x18\x1b[2J\x1b[1;1HUltimate menu\x1b[3;3H\x1b(0x\x1b(BDisk")
        self.assertEqual(self.show_until("Disk"), "Ultimate menu\n\n  |Disk\n")

    def test_keys_reach_the_device_in_order(self):
        self.start()
        out = menu.client(self.host, "key", "down", "text:ab", "enter")
        self.assertEqual(out, "\n")                         # blank screen
        self.device.settimeout(5)
        sent = b""
        while len(sent) < 6:
            sent += self.device.recv(64)
        self.assertEqual(sent, b"\x1b[Bab\r")

    def test_wait_returns_once_the_text_is_shown(self):
        self.start()
        self.device.sendall(b"Flashing... Done!")
        self.show_until("Done!")
        self.assertEqual(menu.client(self.host, "wait", "Done!", "5"), "Flashing... Done!\n")

    def test_wait_polls_until_the_text_arrives(self):
        self.start()
        timer = threading.Timer(0.3, self.device.sendall, (b"PLEASE TURN OFF",))
        timer.start()
        self.addCleanup(timer.join, 5)
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        self.assertEqual(menu.client(self.host, "wait", "TURN OFF", "5"), "PLEASE TURN OFF\n")
        self.assertLess(time.monotonic() - started, 4)

    def test_wait_times_out(self):
        self.start()
        self.device.sendall(b"Main menu")
        self.show_until("Main menu")
        self.assertEqual(menu.client(self.host, "wait", "Done!", "0.3"),
                         "TIMEOUT waiting for 'Done!'\nMain menu\n")

    def test_stop_removes_the_socket_and_exits(self):
        server = self.start()
        self.assertEqual(menu.client(self.host, "stop"), "stopped\n")
        server.join(5)
        self.assertFalse(server.is_alive())
        self.assertFalse(os.path.exists(self.path))
        self.exit.assert_called_with(0)

    def test_device_closing_the_link_ends_the_session(self):
        self.start()
        self.device.close()
        pump = self.threads[1]
        pump.join(5)
        self.assertFalse(pump.is_alive())
        self.exit.assert_called_once_with(0)

    def test_stale_socket_file_is_replaced(self):
        with open(self.path, "w") as handle:
            handle.write("stale")
        self.start()
        self.device.sendall(b"fresh")
        self.assertEqual(self.show_until("fresh"), "fresh\n")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sleep = mock.Mock()
        patches = [mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.dir.name}),
                   mock.patch.object(menu, "time", ModuleProxy(time, sleep=self.sleep)),
                   mock.patch.object(menu, "client", return_value="Main menu\n")]
        _, _, self.client = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    def spawnl(self):
        return mock.patch.object(menu, "os", ModuleProxy(os, spawnl=mock.Mock()))

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = menu.main(["u64ii_menu.py", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_too_few_arguments_prints_usage(self):
        rc, out, _ = self.run_main("c64u")
        self.assertEqual(rc, 2)
        self.assertEqual(out, menu.__doc__ + "\n")
        self.client.assert_not_called()

    def test_unknown_verb_prints_usage(self):
        rc, out, _ = self.run_main("c64u", "type")
        self.assertEqual(rc, 2)
        self.assertIn("HOST start", out)
        self.client.assert_not_called()

    def test_serve_runs_the_session_in_this_process(self):
        with mock.patch.object(menu, "serve") as serve:
            self.assertEqual(self.run_main("c64u", "serve")[0], 0)
        serve.assert_called_once_with("c64u")

    def test_start_spawns_the_server_and_shows_the_screen(self):
        open(menu.socket_path("c64u"), "w").close()
        with self.spawnl() as fake_os:
            rc, out, _ = self.run_main("c64u", "start")
        self.assertEqual(rc, 0)
        fake_os.spawnl.assert_called_once_with(os.P_NOWAIT, sys.executable, sys.executable,
                                      os.path.abspath(menu.__file__), "c64u", "serve")
        self.assertEqual(self.sleep.call_args_list, [mock.call(1.5)])
        self.client.assert_called_once_with("c64u", "show")
        self.assertEqual(out, "Main menu\n")

    def test_start_gives_the_server_five_seconds_to_bind(self):
        with self.spawnl():
            self.run_main("c64u", "start")
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.1)] * 50 + [mock.call(1.5)])
        self.client.assert_called_once_with("c64u", "show")

    def test_unknown_key_is_refused_before_anything_is_sent(self):
        rc, out, err = self.run_main("c64u", "key", "down", "jump")
        self.assertEqual(rc, 2)
        self.assertEqual(err, "unknown key 'jump'\n")
        self.assertEqual(out, "")
        self.client.assert_not_called()

    def test_keys_are_sent_and_the_screen_printed(self):
        rc, out, _ = self.run_main("c64u", "key", "f1", "text:x", "enter")
        self.assertEqual(rc, 0)
        self.client.assert_called_once_with("c64u", "key", "f1", "text:x", "enter")
        self.assertEqual(out, "Main menu\n")

    def test_wait_defaults_to_twenty_seconds(self):
        self.run_main("c64u", "wait", "Done!")
        self.client.assert_called_once_with("c64u", "wait", "Done!", "20")

    def test_wait_with_a_limit(self):
        self.run_main("c64u", "wait", "Done!", "90")
        self.client.assert_called_once_with("c64u", "wait", "Done!", "90")

    def test_wait_timeout_exits_one(self):
        self.client.return_value = "TIMEOUT waiting for 'Done!'\nMain menu\n"
        rc, out, _ = self.run_main("c64u", "wait", "Done!", "1")
        self.assertEqual(rc, 1)
        self.assertEqual(out, self.client.return_value)

    def test_show_and_stop_pass_through(self):
        for verb in ("show", "stop"):
            self.client.reset_mock()
            self.assertEqual(self.run_main("c64u", verb)[0], 0)
            self.client.assert_called_once_with("c64u", verb)

    @unittest.expectedFailure
    def test_wait_without_text_prints_usage(self):
        # Confirmed defect: u64ii_menu.py:228 indexes args[0] unchecked, so
        # `HOST wait` with no TEXT raises IndexError instead of printing usage.
        # Only that IndexError or a wrong result is the expected failure. Any
        # other exception ends the test normally, which unittest reports as an
        # unexpected success and so fails the run.
        try:
            rc, out, _ = self.run_main("c64u", "wait")
        except IndexError:
            raise
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()
            return
        self.assertEqual(rc, 2)
        self.assertEqual(out, menu.__doc__ + "\n")
        self.client.assert_not_called()

    def test_script_entry_point_exits_with_the_status(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["u64ii_menu.py"]), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            runpy.run_path(menu.__file__, run_name="__main__")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("HOST key NAME", out.getvalue())


class ClientTest(unittest.TestCase):
    def setUp(self):
        ignore_leaked_sockets(self)

    def test_sends_one_tab_separated_line_and_reads_to_eof(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            server = socket.socket(socket.AF_UNIX)
            self.addCleanup(server.close)
            server.settimeout(5)
            server.bind(menu.socket_path("h"))
            server.listen(1)
            got = []

            def answer():
                conn, _ = server.accept()
                with conn, conn.makefile("rb") as reader:
                    conn.settimeout(5)
                    got.append(reader.readline())
                    conn.sendall(b"x" * 70000)           # more than one recv
                    conn.sendall("é\n".encode())

            thread = threading.Thread(target=answer, daemon=True)
            thread.start()
            out = menu.client("h", "wait", "Done!", "5")
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(got, [b"wait\tDone!\t5\n"])
        self.assertEqual(out, "x" * 70000 + "é\n")

    def test_no_session_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}), \
                self.assertRaises(FileNotFoundError):
            menu.client("h", "show")


if __name__ == "__main__":
    unittest.main()
