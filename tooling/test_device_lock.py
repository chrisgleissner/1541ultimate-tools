"""Tests for device_lock.py and with_device_locks.py, with real processes and real flock(2) locks."""

import contextlib
import fcntl
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import device_lock  # noqa: E402
import with_device_locks as wdl  # noqa: E402

WRAPPER = os.path.join(HERE, "with_device_locks.py")

# Run in a child process: take the c64u lock the way a tool does and print what happened.
PROBE = textwrap.dedent("""
    import sys
    sys.path.insert(0, %r)
    import device_lock
    try:
        handle = device_lock.acquire(device_lock.lock_path("c64u"), 0, "c64u", "probe")
    except device_lock.Busy as busy:
        print("busy", busy.holder)
    else:
        print("covered" if handle is None else "taken")
""" % HERE)


class TempLocks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "locks")
        self.enterContext(mock.patch.dict(os.environ, {"DEVICE_LOCK_DIR": self.dir}))
        self.path = device_lock.lock_path("c64u")

    def hold(self, text=""):
        """Hold the c64u lock in this process, with `text` as the holder line."""
        os.makedirs(self.dir, exist_ok=True)
        handle = open(self.path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(text)
        handle.flush()
        self.addCleanup(handle.close)
        return handle

    def hold_elsewhere(self, text="pid=999999999 device=c64u cmd=stranger\n"):
        """Hold the c64u lock in a process that is not an ancestor of anything the test starts."""
        os.makedirs(self.dir, exist_ok=True)
        code = ("import fcntl,sys;h=open(sys.argv[1],'a+');fcntl.flock(h.fileno(),fcntl.LOCK_EX);"
                "h.write(sys.argv[2]);h.flush();print('held',flush=True);sys.stdin.read()")
        holder = subprocess.Popen([sys.executable, "-c", code, self.path, text], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def probe(self, *prefix):
        out = subprocess.run([*prefix, sys.executable, "-c", PROBE], capture_output=True, text=True,
                             check=True, env=dict(os.environ))
        return out.stdout.strip()


class PathTest(TempLocks):
    def test_directory_comes_from_the_environment(self):
        self.assertEqual(device_lock.lock_dir(), self.dir)
        self.assertEqual(self.path, os.path.join(self.dir, "c64u.lock"))

    def test_default_directory(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DEVICE_LOCK_DIR")
            self.assertEqual(device_lock.lock_path("u64"), "/tmp/1541ultimate-device-locks/u64.lock")
        with mock.patch.dict(os.environ, {"DEVICE_LOCK_DIR": ""}):
            self.assertEqual(device_lock.lock_dir(), device_lock.DEFAULT_DIR)


class AncestorsTest(unittest.TestCase):
    # A fixed parent pid: under a container entrypoint the real parent can be pid 1.
    def proc(self, parents):
        def fake_open(path):
            pid = int(path.split("/")[2])
            if pid not in parents:
                raise FileNotFoundError(path)
            return io.StringIO(f"{pid} (odd) name) S {parents[pid]} 1 1 0")
        self.enterContext(mock.patch.object(device_lock, "open", fake_open, create=True))
        self.enterContext(mock.patch.object(device_lock.os, "getppid", return_value=300))

    def test_walk_stops_at_init(self):
        self.proc({300: 400, 400: 1})
        self.assertEqual(device_lock.ancestors(), {300, 400})

    def test_walk_stops_at_a_loop(self):
        self.proc({300: 400, 400: 300})
        self.assertEqual(device_lock.ancestors(), {300, 400})

    def test_walk_stops_at_an_unreadable_process(self):
        self.proc({300: 400})
        self.assertEqual(device_lock.ancestors(), {300, 400})

    def test_malformed_stat_stops_the_walk(self):
        self.enterContext(mock.patch.object(device_lock.os, "getppid", return_value=300))
        self.enterContext(mock.patch.object(device_lock, "open", mock.mock_open(read_data="garbage"),
                                            create=True))
        self.assertEqual(device_lock.ancestors(), {300})

    def test_init_ends_the_walk(self):
        with mock.patch.object(device_lock.os, "getppid", return_value=1):
            self.assertEqual(device_lock.ancestors(), set())

    @unittest.skipIf(os.getppid() <= 1, "the test runner is a child of init")
    def test_real_parent_but_not_this_process(self):
        pids = device_lock.ancestors()
        self.assertIn(os.getppid(), pids)
        self.assertNotIn(os.getpid(), pids)


class AcquireTest(TempLocks):
    def test_takes_writes_the_holder_line_and_clears_it(self):
        handle = device_lock.acquire(self.path, 0, "c64u", "u64ii_jtag.py probe")
        with open(self.path) as other:
            line = other.read()
        self.assertRegex(line, rf"^pid={os.getpid()} device=c64u cmd=u64ii_jtag.py probe since=\d{{4}}-.*\n$")
        with self.assertRaises(device_lock.Busy) as busy:
            device_lock.acquire(self.path)
        self.assertEqual(busy.exception.holder, line.strip())
        device_lock.release(handle)
        with open(self.path) as other:
            self.assertEqual(other.read(), "")
        device_lock.release(device_lock.acquire(self.path))      # free again

    def test_device_name_defaults_to_the_file_name(self):
        handle = device_lock.acquire(self.path)
        self.addCleanup(device_lock.release, handle)
        with open(self.path) as other:
            self.assertIn("device=c64u ", other.read())

    def test_release_of_nothing_is_fine(self):
        device_lock.release(None)

    def test_busy_without_a_holder_line(self):
        self.hold()
        with self.assertRaises(device_lock.Busy) as busy:
            device_lock.acquire(self.path)
        self.assertEqual(busy.exception.holder, "")
        self.assertEqual(str(busy.exception), "held")

    def test_held_context_manager(self):
        with device_lock.held(self.path, 0, "c64u", "x") as handle:
            self.assertIsNotNone(handle)
            with self.assertRaises(device_lock.Busy):
                device_lock.acquire(self.path)
        device_lock.release(device_lock.acquire(self.path))

    def test_wait_announces_once_then_takes_the_released_lock(self):
        holder = self.hold("pid=999999999 other\n")
        announced = []
        with mock.patch.object(device_lock.time, "sleep", side_effect=lambda _s: holder.close()) as nap:
            handle = device_lock.acquire(self.path, 60, on_wait=lambda: announced.append(1))
        self.addCleanup(device_lock.release, handle)
        self.assertIsNotNone(handle)
        nap.assert_called_once_with(0.2)
        self.assertEqual(announced, [1])

    def test_wait_expires(self):
        self.hold("pid=999999999 other\n")
        with self.assertRaises(device_lock.Busy):
            device_lock.acquire(self.path, 0.3)

    def test_holder_line_of_an_ancestor_covers_this_process(self):
        self.hold("pid=4100 device=c64u cmd=x\n")
        with mock.patch.object(device_lock.os, "getppid", return_value=4100):
            self.assertIsNone(device_lock.acquire(self.path))

    def test_holder_pid_must_be_a_whole_pid_field(self):
        # "ppid=4100" and "pid=41000" do not name the ancestor 4100
        self.hold("ppid=4100 pid=41000 other\n")
        with mock.patch.object(device_lock.os, "getppid", return_value=4100), \
                mock.patch.object(device_lock, "_kernel_owners", return_value=set()):
            with self.assertRaises(device_lock.Busy):
                device_lock.acquire(self.path)


class KernelOwnersTest(TempLocks):
    PROC_LOCKS = textwrap.dedent("""\
        1: FLOCK  ADVISORY  WRITE 2578 fd:01:403524 0 EOF
        2: -> FLOCK  ADVISORY  WRITE 2600 fd:01:403524 0 EOF
        3: POSIX  ADVISORY  WRITE 2700 fd:01:403524 0 EOF
        4: FLOCK  ADVISORY  WRITE 2800 fd:01:999 0 EOF
        5: short
        """)

    def test_lists_only_flock_owners_of_this_inode(self):
        with mock.patch.object(device_lock, "open", mock.mock_open(read_data=self.PROC_LOCKS), create=True):
            self.assertEqual(device_lock._kernel_owners(403524), {2578})

    def test_unreadable_proc_locks(self):
        with mock.patch.object(device_lock, "open", side_effect=OSError("no /proc/locks"), create=True):
            self.assertEqual(device_lock._kernel_owners(1), set())

    def test_malformed_owner_is_ignored(self):
        junk = "1: FLOCK ADVISORY WRITE abc fd:01:7 0 EOF\n"
        with mock.patch.object(device_lock, "open", mock.mock_open(read_data=junk), create=True):
            self.assertEqual(device_lock._kernel_owners(7), set())

    def test_kernel_lock_of_an_ancestor_covers_a_lock_without_holder_line(self):
        handle = self.hold()                        # no holder line, like flock(1)
        inode = os.fstat(handle.fileno()).st_ino
        with mock.patch.object(device_lock, "_kernel_owners", return_value={4100}) as owners, \
                mock.patch.object(device_lock, "ancestors", return_value={4100}):
            self.assertIsNone(device_lock.acquire(self.path))
            self.assertTrue(device_lock.held_by_ancestor(handle, ""))
            owners.assert_called_with(inode)
        with mock.patch.object(device_lock, "_kernel_owners", return_value={4100}), \
                mock.patch.object(device_lock, "ancestors", return_value={4101}):
            with self.assertRaises(device_lock.Busy):
                device_lock.acquire(self.path)

    def test_closed_handle_is_not_held_by_an_ancestor(self):
        handle = open(os.devnull)
        handle.close()
        self.assertFalse(device_lock.held_by_ancestor(handle, ""))


class RealProcessTest(TempLocks):
    """The lock holder is a real ancestor process, started the way users start it."""

    def test_tool_in_the_wrapper_recognises_the_wrapper_lock(self):
        self.assertEqual(self.probe(sys.executable, WRAPPER, "c64u", "--"), "covered")

    def test_tool_outside_any_wrapper_takes_the_lock(self):
        self.assertEqual(self.probe(), "taken")

    def test_tool_refused_while_a_stranger_holds_the_lock(self):
        self.hold_elsewhere()
        self.assertEqual(self.probe(), "busy pid=999999999 device=c64u cmd=stranger")

    @unittest.skipUnless(shutil.which("flock") and os.path.exists("/proc/locks"), "needs flock(1) and /proc/locks")
    def test_plain_flock_wrapping_does_not_deadlock_the_tool(self):
        os.makedirs(self.dir)
        self.assertEqual(self.probe("flock", "-w", "3", self.path), "covered")
        self.assertEqual(self.probe("flock", "-o", "-w", "3", self.path), "covered")
        with open(self.path) as handle:
            self.assertEqual(handle.read(), "")     # flock(1) wrote no holder line

    @unittest.skipUnless(os.path.exists("/proc/locks"), "needs /proc/locks")
    def test_flock_taken_by_a_python_ancestor_without_a_holder_line(self):
        script = textwrap.dedent("""
            import fcntl, subprocess, sys
            handle = open(sys.argv[1], "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            subprocess.run([sys.executable, "-c", sys.argv[2]], check=True)
        """)
        os.makedirs(self.dir)
        out = subprocess.run([sys.executable, "-c", script, self.path, PROBE], capture_output=True,
                             text=True, check=True)
        self.assertEqual(out.stdout.strip(), "covered")

    def test_stranger_without_a_holder_line_is_refused(self):
        self.hold_elsewhere("")
        self.assertEqual(self.probe(), "busy")


TOOLS = {
    # name: (device, a statement that takes the tool's lock and, once inside, prints the lock file)
    "u64ii_jtag": ("c64u", "import u64ii_jtag as m; lock = m.DeviceLock(wait=0)"),
    "c64u_mode": ("c64u", "import c64u_mode as m; lock = m.DeviceLock('probe')"),
    "flash_u64": ("u64", "import flash_u64 as m; lock = m.DeviceLock()"),
    "jtag_verify c64u": ("c64u", "import jtag_verify as m; lock = m.target_lock('c64u', None)"),
    "jtag_verify u64": ("u64", "import jtag_verify as m; lock = m.target_lock('u64', None)"),
}


class ToolsTest(TempLocks):
    """Every tool that takes a device lock, started as a separate process."""

    def tool(self, statement, *prefix):
        code = (f"import sys; sys.path.insert(0, {HERE!r})\n{statement}\n"
                "with lock:\n"
                "    import glob; print(*[open(f).read().strip() for f in glob.glob(sys.argv[1] + '/*.lock')])\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("U64II_JTAG_LOCK", "FLASH_U64_LOCK", "C64U_MODE_LOCK", "C64U_JTAG_LOCK")}
        return subprocess.run([*prefix, sys.executable, "-c", code, self.dir], capture_output=True, text=True,
                              env=env)

    def test_holder_line_while_the_tool_holds_the_lock(self):
        for name, (device, statement) in TOOLS.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(os.environ, {"DEVICE_LOCK_DIR": tmp}):
                    self.dir = tmp
                    out = self.tool(statement)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertRegex(out.stdout, rf"^pid=\d+ device={device} cmd=\S")

    def test_tool_under_the_wrapper_runs_without_waiting(self):
        for name, (device, statement) in TOOLS.items():
            with self.subTest(name):
                out = self.tool(statement, sys.executable, WRAPPER, device, "--")
                self.assertEqual(out.returncode, 0, out.stderr)

    @unittest.skipUnless(shutil.which("flock") and os.path.exists("/proc/locks"), "needs flock(1) and /proc/locks")
    def test_tool_under_plain_flock_does_not_deadlock(self):
        os.makedirs(self.dir, exist_ok=True)
        for name, (device, statement) in TOOLS.items():
            with self.subTest(name):
                out = self.tool(statement, "flock", "-w", "5", device_lock.lock_path(device))
                self.assertEqual(out.returncode, 0, out.stderr)

    def test_tool_refused_while_a_stranger_holds_its_lock(self):
        for name, (device, statement) in TOOLS.items():
            with self.subTest(name):
                self.path = device_lock.lock_path(device)
                self.hold_elsewhere("pid=999999999 device=x cmd=stranger\n")
                out = self.tool(statement)
                self.assertNotEqual(out.returncode, 0)
                self.assertRegex(out.stderr, r"in use|busy")
                os.remove(self.path)          # the stranger keeps its (unlinked) lock


class WrapperTest(TempLocks):
    def run_wrapper(self, *args):
        return subprocess.run([sys.executable, WRAPPER, *args], capture_output=True, text=True,
                              env=dict(os.environ))

    def test_holds_the_locks_and_writes_the_holder_lines(self):
        code = ("import os,sys;"
                "print(open(os.environ['DEVICE_LOCK_DIR']+'/u64.lock').read());"
                "print(open(os.environ['DEVICE_LOCK_DIR']+'/c64u.lock').read())")
        out = self.run_wrapper("c64u", "u64", "--", sys.executable, "-c", code)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.split("\n")
        self.assertRegex(lines[0], r"^pid=\d+ device=u64 cmd=\S+ -c ")
        self.assertRegex(lines[2], r"^pid=\d+ device=c64u cmd=")
        # the wrapper's pid, not the child's, is the holder
        self.assertNotIn(f"pid={os.getpid()} ", out.stdout)

    def test_exit_status_of_the_command_and_release_afterwards(self):
        out = self.run_wrapper("c64u", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(out.returncode, 7)
        device_lock.release(device_lock.acquire(self.path))

    def test_signal_death_reports_128_plus_n(self):
        code = "import os,signal;os.kill(os.getpid(), signal.SIGKILL)"
        self.assertEqual(self.run_wrapper("c64u", "--", sys.executable, "-c", code).returncode, 128 + 9)

    def test_refuses_when_a_stranger_holds_the_lock(self):
        self.hold_elsewhere()
        out = self.run_wrapper("c64u", "--", sys.executable, "-c", "print('ran')")
        self.assertEqual(out.returncode, 75)
        self.assertEqual(out.stdout, "")
        self.assertIn("c64u is in use", out.stderr)
        self.assertIn("pid=999999999", out.stderr)

    def test_a_refused_second_device_releases_the_first(self):
        os.makedirs(self.dir)
        self.path, c64u = device_lock.lock_path("u64"), self.path
        self.hold_elsewhere("")
        self.path = c64u
        out = self.run_wrapper("c64u", "u64", "--", sys.executable, "-c", "print('ran')")
        self.assertEqual(out.returncode, 75)
        device_lock.release(device_lock.acquire(self.path))      # c64u was released again

    def test_wait_takes_the_lock_once_it_is_free(self):
        holder = self.hold_elsewhere()
        timer = threading.Timer(0.5, holder.stdin.close)
        timer.start()
        self.addCleanup(timer.join)
        out = self.run_wrapper("--wait", "10", "c64u", "--", sys.executable, "-c", "print('ran')")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "ran"), out.stderr)

    def test_nested_wrappers_share_the_lock(self):
        out = self.run_wrapper("c64u", "--", sys.executable, WRAPPER, "c64u", "--", sys.executable, "-c",
                               "print('inner')")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "inner"), out.stderr)

    def test_duplicate_devices_are_locked_once(self):
        out = self.run_wrapper("c64u", "c64u", "--", sys.executable, "-c", "print('ran')")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "ran"), out.stderr)

    def test_missing_and_unexecutable_commands(self):
        out = self.run_wrapper("c64u", "--", "no-such-command-for-with-device-locks")
        self.assertEqual(out.returncode, 127)
        self.assertIn("command not found", out.stderr)
        out = self.run_wrapper("c64u", "--", self.dir)               # a directory
        self.assertEqual(out.returncode, 126)
        device_lock.release(device_lock.acquire(self.path))

    def test_usage_errors(self):
        for argv, text in (([], "required: DEVICE"), (["c64u"], "missing --"), (["--", "true"], "required: DEVICE"),
                           (["c64u", "--"], "no COMMAND"), (["../x", "--", "true"], "invalid device name"),
                           (["a/b", "--", "true"], "invalid device name")):
            with self.subTest(argv=argv):
                out = self.run_wrapper(*argv)
                self.assertEqual(out.returncode, 2)
                self.assertIn(text, out.stderr)
        self.assertFalse(os.path.exists(self.dir))               # nothing was created

    def test_sigterm_reaches_the_command_and_the_locks_are_released(self):
        child = subprocess.Popen([sys.executable, WRAPPER, "c64u", "--", sys.executable, "-c",
                                  "import time; print('up', flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.assertEqual(child.stdout.readline().strip(), "up")
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=10), 128 + signal.SIGTERM)
        device_lock.release(device_lock.acquire(self.path))

    def test_run_in_process(self):
        # the in-process path, which the coverage run measures
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wdl.run(["c64u", "--", sys.executable, "-c", "pass"]), 0)
            self.assertEqual(wdl.run(["c64u", "--", sys.executable, "-c", "raise SystemExit(3)"]), 3)
            self.assertEqual(wdl.run(["c64u", "--", "no-such-command-for-with-device-locks"]), 127)
            self.assertEqual(wdl.run(["c64u", "--", self.dir]), 126)
            self.hold_elsewhere()
            self.assertEqual(wdl.run(["c64u", "--", sys.executable, "-c", "pass"]), 75)
            with self.assertRaises(SystemExit):
                wdl.run(["c64u"])
            with self.assertRaises(SystemExit):
                wdl.run(["c64u", "--"])

    def test_run_in_process_signal_status(self):
        killed = "import os,signal;os.kill(os.getpid(), signal.SIGKILL)"
        self.assertEqual(wdl.run(["c64u", "--", sys.executable, "-c", killed]), 128 + 9)
        handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
        wdl.run(["c64u", "--", sys.executable, "-c", "pass"])
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)), handlers)

    def test_forwards_a_signal_to_the_command(self):
        code = "import os,signal;os.kill(os.getppid(), signal.SIGTERM);import time;time.sleep(30)"
        self.assertEqual(wdl.run(["c64u", "--", sys.executable, "-c", code]), 128 + signal.SIGTERM)

    def test_main_exits_with_the_status(self):
        out = subprocess.run([sys.executable, WRAPPER, "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("DEVICE", out.stdout)


if __name__ == "__main__":
    unittest.main()
