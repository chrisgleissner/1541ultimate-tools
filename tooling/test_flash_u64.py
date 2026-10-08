#!/usr/bin/env python3
"""Host tests for flash_u64.py.

The pure parts (the .u64 header, the answer table, symbol lookup, the code
check, gdb/MI values) are tested directly. The whole run is tested against a
bench model: the Intel tools, ping and the power button are fakes behind
subprocess, nios2-elf-gdb is a fake process that speaks gdb/MI over a real
pipe, and time, sockets and REST are fakes. Nothing touches a device, the
toolchain or the shared device lock.

    python3 tooling/test_flash_u64.py
"""

import contextlib
import glob
import io
import json
import os
import re
import runpy
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
import warnings
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flash_u64 as fu  # noqa: E402


def u64_file(image, load=0x03000000, entry=0x03000234):
    return struct.pack("<III", load, len(image), entry) + image


class ParseU64(unittest.TestCase):
    def test_header(self):
        image = bytes(range(256)) * 4
        self.assertEqual(fu.parse_u64(u64_file(image)), (0x03000000, 0x03000234, image))

    def test_length_mismatch(self):
        raw = u64_file(b"\0" * 1024)[:-1]
        with self.assertRaises(fu.FlashError):
            fu.parse_u64(raw)

    def test_entry_outside(self):
        with self.assertRaises(fu.FlashError):
            fu.parse_u64(u64_file(b"\0" * 16, entry=0x03000100))

    def test_short(self):
        with self.assertRaises(fu.FlashError):
            fu.parse_u64(b"\0" * 8)


class Answers(unittest.TestCase):
    def test_keeps_settings_and_flash_disk(self):
        self.assertEqual(fu.answer_for("Reformat Flash Disk?", fu.YES | fu.NO), fu.NO)
        self.assertEqual(fu.answer_for("About to update. Continue?", fu.YES | fu.NO), fu.YES)
        self.assertEqual(fu.answer_for("Reset Configuration? (Recommended)", fu.YES | fu.NO), fu.NO)

    def test_esp32_popups_are_acknowledged(self):
        for text in ("Flashing ESP32 Success!", "Flashing ESP32 Failed!",
                     "Could not set ESP32 to download mode"):
            self.assertEqual(fu.answer_for(text, fu.OK), fu.OK)

    def test_unknown_or_changed_popup_is_not_answered(self):
        self.assertIsNone(fu.answer_for("Error reading Flash Disk. Format?", fu.YES | fu.NO))
        self.assertIsNone(fu.answer_for("Problem with Flash.. Abort!", fu.OK))
        self.assertIsNone(fu.answer_for("Reformat Flash Disk", fu.YES | fu.NO))
        # Same text, different buttons: the popup changed, so do not guess.
        self.assertIsNone(fu.answer_for("Reformat Flash Disk?", fu.OK))


class Symbols(unittest.TestCase):
    NM = "\n".join([
        "03032c5c 00000214 T UserInterface::popup(char const*, unsigned char)",
        "03032e70 0000002c T UserInterface::popup(char const*, int, char const**, char const*)",
        "030378d8 00000394 T flash_buffer_at(Flash*, Screen*, int, bool, void*, void*, char const*, char const*)",
        "03559ab4 00000004 g flash_buffer_at(Flash*, Screen*, int, bool, void*, void*, char const*, char const*)::last_sector",
        "0304f09c 00000120 T update_esp32()",
        "0304ee84 00000218 T update_esp32_impl()",
        "0304e298 00000084 t turn_off()",
    ])

    def test_lookup_takes_the_exact_overload(self):
        self.assertEqual(fu.find_symbols(self.NM), {
            "popup": (0x03032c5c, 0x214), "flash_buffer_at": (0x030378d8, 0x394),
            "update_esp32": (0x0304f09c, 0x120), "turn_off": (0x0304e298, 0x84)})

    def test_missing_symbol(self):
        with self.assertRaises(fu.FlashError):
            fu.find_symbols("03032c5c 00000214 T UserInterface::popup(char const*, unsigned char)")


def words(*values):
    return struct.pack(f"<{len(values)}I", *values)


class CodeCheck(unittest.TestCase):
    LOAD = 0x03000000
    # addi sp,sp,-8 ; stw ra,4(sp) ; movhi r4,%hi(str) ; addi r4,r4,%lo(str) ;
    # call f ; add r2,r3,r4 x5 (opcode 0x3a, R-type)
    CODE = words(0xdefffe04, 0xdfc00115, 0x01000034 | (0x0300 << 6), 0x21000004 | (0x1234 << 6),
                 0x00000000 | (0x00c0d0e << 6), *([0x1885883a] * 5))

    def check(self, reference):
        image = b"\0" * 0x40 + self.CODE + b"\0" * 0x40
        ref = b"\0" * 0x40 + reference + b"\0" * 0x40
        fu.check_code(image, ref, self.LOAD, {"f": (self.LOAD + 0x40, len(self.CODE))})

    def test_identical(self):
        self.check(self.CODE)

    def test_moved_data_and_call_targets_are_allowed(self):
        moved = bytearray(self.CODE)
        moved[8:20] = words(0x01000034 | (0x0301 << 6), 0x21000004 | (0x1228 << 6),
                            0x00000000 | (0x00c0d0b << 6))
        self.check(bytes(moved))

    def test_different_prologue_is_refused(self):
        other = bytearray(self.CODE)
        other[0:4] = words(0xdefffc04)
        with self.assertRaises(fu.FlashError):
            self.check(bytes(other))

    def test_function_outside_the_image_is_refused(self):
        with self.assertRaisesRegex(fu.FlashError, "lies outside the image"):
            fu.check_code(self.CODE, self.CODE, self.LOAD, {"f": (self.LOAD - 4, 8)})

    def test_different_instructions_are_refused(self):
        other = bytearray(self.CODE)
        other[20:40] = words(*([0x1887883a] * 5))
        with self.assertRaises(fu.FlashError):
            self.check(bytes(other))


class MiValues(unittest.TestCase):
    def test_register(self):
        self.assertEqual(fu.mi_value('^done,value="0x3032c5c"'), 0x03032c5c)

    def test_pointer_with_symbol(self):
        self.assertEqual(fu.mi_value('^done,value="(void (*)()) 0x3032c5c <popup>"'), 0x03032c5c)

    def test_decimal_and_negative(self):
        self.assertEqual(fu.mi_value('^done,value="4"'), 4)
        self.assertEqual(fu.mi_value('^done,value="-1"'), 0xffffffff)

    def test_pointer_into_a_template(self):
        record = '^done,value="(void (*)()) 0x3001234 <IndexedList<Path*>::append(Path*)+12>"'
        self.assertEqual(fu.mi_value(record), 0x3001234)

    def test_no_number_is_a_flash_error(self):
        for record in ('^done,value="<optimized out>"', "^done"):
            with self.assertRaises(fu.FlashError):
                fu.mi_value(record)


# ---------------------------------------------------------------- bench model

LOAD, ENTRY = 0x03000000, 0x03000100
IMAGE = bytes((7 * i + 3) & 0xff for i in range(0x1000))
ADDR = {"popup": LOAD + 0x200, "flash_buffer_at": LOAD + 0x400,
        "update_esp32": LOAD + 0x600, "turn_off": LOAD + 0x800}
NM = "\n".join([f"{ADDR[key]:08x} 00000040 T {name}" for key, name in fu.SYMBOLS.items()]
               + ["03000a00 00000010 T UserInterface::popup(char const*, int, char const**, char const*)"])
YES_NO = fu.YES | fu.NO
# software/userinterface/ui_elements.h: BUTTON_OK 1, BUTTON_YES 2, BUTTON_NO 4
EXPECTED_ANSWERS = [("Reformat Flash Disk?", 4), ("About to update. Continue?", 2),
                    ("Flashing ESP32 Success!", 1), ("Flashing ESP32 Failed!", 1),
                    ("Could not set ESP32 to download mode", 1),
                    ("Reset Configuration? (Recommended)", 4)]
HANG_LIMIT = 10     # real seconds before a blocked test counts as hung


class Hung(Exception):
    pass


def guard_against_hangs(test):
    """A read that blocks in the code under test fails the test instead of hanging it."""
    def expire(signum, frame):
        raise Hung(f"blocked for {HANG_LIMIT} s")
    previous = signal.signal(signal.SIGALRM, expire)
    test.addCleanup(signal.signal, signal.SIGALRM, previous)
    signal.setitimer(signal.ITIMER_REAL, HANG_LIMIT)
    test.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)


class Clock:
    """time.monotonic and time.sleep, where sleeping only moves the clock."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def stop_at(key, pc=None, **regs):
    pc = ADDR[key] if pc is None else pc
    return types.SimpleNamespace(pc=pc, regs=dict(regs, pc=pc), message=None, ra=None, memory={})


def popup(message, flags, at=[0x03100000]):
    at[0] += 0x100
    ra = LOAD + 0xc00 + (at[0] & 0x3ff)
    # Bits above the low byte of r6 are not part of the unsigned char argument.
    return types.SimpleNamespace(pc=ADDR["popup"], message=message, ra=ra,
                                 regs={"pc": ADDR["popup"], "r5": at[0], "r6": 0x1200 | flags,
                                       "ra": ra, "r2": 0x55},
                                 memory={at[0]: message.encode("latin-1") + b"\0stale text"})


def updater_run():
    """Every popup in the answer table, both flash writes and the ESP32 check."""
    return [popup("Reformat Flash Disk?", YES_NO), popup("About to update. Continue?", YES_NO),
            stop_at("flash_buffer_at", r6=0), stop_at("flash_buffer_at", r6=0x290000),
            stop_at("update_esp32"), popup("Flashing ESP32 Success!", fu.OK),
            popup("Flashing ESP32 Failed!", fu.OK),
            popup("Could not set ESP32 to download mode", fu.OK),
            popup("Reset Configuration? (Recommended)", YES_NO), stop_at("turn_off")]


class FakeServer:
    """nios2-gdb-server: listening while it runs."""

    def __init__(self, bench):
        self.bench = bench
        self.pid = 4242
        self.returncode = None
        self.terminated = self.killed = self.reaped = False

    def poll(self):
        if self.returncode is None and self.bench.server_exit is not None:
            self.returncode = self.bench.server_exit
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.bench.server_ignores_term:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("nios2-gdb-server", timeout)
        self.reaped = True
        return self.returncode


class FakeGdb:
    """nios2-elf-gdb --interpreter=mi2 on a real pipe, against a CPU that stops
    where the bench's event list says."""

    def __init__(self, bench):
        self.bench = bench
        read, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read, "r")
        self.stdin = types.SimpleNamespace(write=self.input, flush=lambda: None, close=self.eof)
        self.pid = 4343
        self.returncode = None
        self.killed = self.stdin_closed = False
        self.commands, self.inserted, self.returns = [], [], []
        self.pending = ""
        self.regs = {"pc": bench.halted_pc}
        self.stop = bench.halted
        if self.stop:
            self.regs.update(self.stop.regs)
        if bench.gdb_starts:
            self.emit('=thread-group-added,id="i1"\n(gdb) \n')
        else:
            self.exit(1)

    def emit(self, text):
        if self.write_fd is not None:
            os.write(self.write_fd, text.encode("latin-1"))

    def exit(self, code):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None
        if self.returncode is None:
            self.returncode = code

    def close_pipes(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None
        self.stdout.close()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("nios2-elf-gdb", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.exit(-9)

    def terminate(self):
        self.exit(-15)

    def input(self, text):
        if self.returncode is not None:
            raise BrokenPipeError("gdb has exited")
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.commands.append(line)
            self.command(line)

    def eof(self):
        self.stdin_closed = True
        if not self.bench.gdb_ignores_eof:
            self.exit(0)

    def command(self, line):
        error = self.bench.gdb_errors.get(line.split(" ")[0])
        if error:
            return self.emit(f'^error,msg="{error}"\n(gdb) \n')
        if line == "-exec-continue":
            return self.resume()
        if line == "-gdb-exit":
            self.emit("^exit\n")
            return self.exit(0)
        if line.startswith("-target-select"):
            return self.emit('^connected,frame={addr="0x00000000"}\n(gdb) \n')
        m = re.fullmatch(r'-data-evaluate-expression "\$(\w+)"', line)
        if m:
            return self.done(f'value="{self.render(m.group(1))}"')
        m = re.fullmatch(r"-data-read-memory-bytes (\S+) (\d+)", line)
        if m:
            at, length = int(m.group(1), 0), int(m.group(2))
            data = self.bench.memory.get(at, b"").ljust(length, b"\xee")[:length]
            return self.done(f'memory=[{{begin="{at:#x}",offset="0x00000000",'
                             f'end="{at + length:#x}",contents="{data.hex()}"}}]')
        m = re.fullmatch(r'-interpreter-exec console "set \$(\w+) = (\S+)"', line)
        if m:
            self.regs[m.group(1)] = int(m.group(2), 0)
        m = re.fullmatch(r"-break-insert \*(\S+)", line)
        if m:
            self.inserted.append(int(m.group(1), 0))
        self.done()

    def done(self, results=""):
        self.emit(("^done," + results if results else "^done") + "\n(gdb) \n")

    def render(self, register):
        value = self.regs.get(register, 0)
        if register == "pc":
            names = {addr: fu.SYMBOLS[key] for key, addr in ADDR.items()}
            return f"(void (*)()) {value:#x} <{names.get(value, 'main()+64')}>"
        if register == "r6":
            return str(value)
        return f"{value:#x}"

    def resume(self):
        if self.stop is not None and self.stop.message is not None:
            returned = self.regs["pc"] == self.stop.ra
            self.returns.append((self.stop.message, self.regs["r2"] if returned else None))
        leaving_turn_off = self.regs["pc"] == ADDR["turn_off"]
        self.emit('^running\n*running,thread-id="all"\n(gdb) \n')
        if leaving_turn_off:
            self.bench.switch_off()
            return
        if not self.bench.events:
            return          # the CPU runs on and never stops again
        self.stop = self.bench.events.pop(0)
        self.regs.update(self.stop.regs)
        self.emit(f'*stopped,reason="breakpoint-hit",frame={{addr="{self.stop.pc:#010x}"}},'
                  'thread-id="1"\n(gdb) \n')


class Bench:
    """The U64, its JTAG cable, its network and its power button."""

    def __init__(self, test, clock, lock_path):
        self.test = test
        self.clock = clock
        self.lock_path = lock_path
        self.on = True
        self.flashed = False
        self.rest = True                # REST answers whenever the machine is on
        self.ping_alive = None          # None: ping answers while the machine is on
        self.fpga_visible = None        # None: jtagconfig sees the FPGA while it is on
        self.memory = {}
        self.halted, self.halted_pc = None, 0
        self.set_events(updater_run())
        self.nm = NM
        self.failing = {}               # tool -> stderr of a failed run
        self.missing_tools = set()
        self.damage_elf = False
        self.download_output = "Verifying 03000000 ( 0%)\nVerified OK\n"
        self.downloaded = None
        self.press_works = [True]
        self.press_rc = 0
        self.presses = []               # whether the machine was on at each press
        self.person_after = None        # REST polls until a person presses the button
        self.port_busy = False
        self.listen_after = 1
        self.connects = 0
        self.server_exit = None
        self.server_ignores_term = False
        self.gdb_starts = True
        self.gdb_ignores_eof = False
        self.gdb_errors = {}
        self.ran, self.urls, self.servers, self.gdbs = [], [], [], []
        self.lock_held = []             # whether the u64 lock was held at download and press
        self.info_before = {"firmware_version": "3.15", "git_commit_hash": "5ee21b65",
                            "fpga_version": "122"}
        self.info_after = {"firmware_version": "3.15a", "git_commit_hash": "d5686424f",
                           "fpga_version": "126", "errors": []}
        self.subprocess = types.SimpleNamespace(
            run=self.run, Popen=self.popen, call=self.call, PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired)

    def set_events(self, events, halted=None):
        self.events = list(events)
        self.halted = halted
        for event in self.events + ([halted] if halted else []):
            self.memory.update(event.memory)

    def switch_off(self):
        self.on = False
        self.flashed = True

    def pings(self):
        return self.on if self.ping_alive is None else self.ping_alive

    def fpga_on(self):
        return self.on if self.fpga_visible is None else self.fpga_visible

    def note_lock(self):
        with open(self.lock_path, "a+") as handle:
            try:
                fu.fcntl.flock(handle.fileno(), fu.fcntl.LOCK_EX | fu.fcntl.LOCK_NB)
                self.lock_held.append(False)
            except BlockingIOError:
                self.lock_held.append(True)

    def run(self, cmd, **kwargs):
        if isinstance(cmd, str):
            self.test.assertTrue(kwargs.get("shell"))
            return self.press(cmd)
        self.ran.append(cmd)
        tool = cmd[0]
        if tool in self.missing_tools:
            raise FileNotFoundError(2, "No such file or directory", tool)
        if tool in self.failing:
            return subprocess.CompletedProcess(cmd, 1, "", self.failing[tool] + "\n")
        stdout = ""
        if tool == "nios2-elf-nm":
            stdout = self.nm
        elif tool == "nios2-elf-objcopy":
            shutil.copyfile(cmd[-2], cmd[-1])
        elif tool == "nios2-elf-ld":
            with open(cmd[2]) as handle:
                self.ld_script = handle.read()
            with open(cmd[-1], "rb") as handle:
                data = handle.read()
            with open(cmd[cmd.index("-o") + 1], "wb") as handle:
                handle.write(data[:-1] if self.damage_elf else data)
        elif tool == "nios2-download":
            self.note_lock()
            with open(cmd[1], "rb") as handle:
                self.downloaded = handle.read()
            stdout = self.download_output
        elif tool == "jtagconfig":
            stdout = "1) USB-Blaster [1-6]\n" + ("  02D020DD   5CEBA4(.|ES)/5CEFA4\n" if self.fpga_on()
                                                 else "  Unable to read device chain\n")
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    def press(self, cmd):
        self.note_lock()
        self.presses.append(self.on)
        works = self.press_works.pop(0) if self.press_works else True
        if works and not self.on:
            self.on = True
        return subprocess.CompletedProcess(cmd, self.press_rc)

    def popen(self, cmd, **kwargs):
        self.ran.append(cmd)
        if cmd[0] in self.missing_tools:
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        if cmd[0] == "nios2-gdb-server":
            self.servers.append(FakeServer(self))
            return self.servers[-1]
        gdb = FakeGdb(self)
        self.gdbs.append(gdb)
        self.test.addCleanup(gdb.close_pipes)
        return gdb

    def call(self, cmd, **kwargs):
        self.ran.append(cmd)
        return 0 if self.pings() else 1

    def connect(self, address, timeout):
        running = any(server.poll() is None for server in self.servers)
        self.connects += running
        if self.port_busy or (running and self.connects >= self.listen_after):
            return io.BytesIO()
        raise ConnectionRefusedError(111, "Connection refused")

    def urlopen(self, url, timeout):
        self.urls.append(url)
        if not self.on and self.person_after is not None:
            self.person_after -= 1
            self.on = self.person_after <= 0
        info = self.info_after if self.flashed else self.info_before
        if not (self.on and self.rest) or info is None:
            raise urllib.error.URLError("unreachable")
        return io.BytesIO(json.dumps(info).encode())

    def select(self, rlist, wlist, xlist, timeout):
        """Nothing to read means the whole timeout passes, on the fake clock."""
        ready = select.select(rlist, wlist, xlist, 0)
        if not ready[0]:
            self.clock.now += timeout
        return ready


class BenchTest(unittest.TestCase):
    def setUp(self):
        guard_against_hangs(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.scratch = os.path.join(self.dir, "tmp")
        os.mkdir(self.scratch)
        self.u64 = os.path.join(self.dir, "update.u64")
        with open(self.u64, "wb") as handle:
            handle.write(u64_file(IMAGE, LOAD, ENTRY))
        self.sym_elf = os.path.join(self.dir, "update.elf")
        with open(self.sym_elf, "wb") as handle:    # the fake objcopy -O binary copies it
            handle.write(IMAGE)
        self.lock_path = os.path.join(self.dir, "locks", "u64.lock")
        self.clock = Clock()
        self.bench = Bench(self, self.clock, self.lock_path)
        self.out = io.StringIO()
        for patch in (mock.patch.object(fu, "time", self.clock),
                      mock.patch.object(fu, "T0", self.clock.now),
                      mock.patch.object(fu, "subprocess", self.bench.subprocess),
                      mock.patch.object(fu, "select", types.SimpleNamespace(select=self.bench.select),
                                        create=True),
                      mock.patch.object(fu.socket, "create_connection", self.bench.connect),
                      mock.patch.object(fu.urllib.request, "urlopen", self.bench.urlopen),
                      mock.patch.object(tempfile, "tempdir", self.scratch),
                      mock.patch.object(fu, "ENV", None),
                      mock.patch.object(fu, "LOCK_PATH", self.lock_path),
                      mock.patch.dict(os.environ, {"INTEL_FPGA_ROOT": os.path.join(self.dir, "intel")}),
                      contextlib.redirect_stdout(self.out)):
            self.enterContext(patch)
        for name in ("FLASH_U64_LOCK", "U64_HOST", "U64_POWER_BUTTON_CMD"):
            os.environ.pop(name, None)

    def argv(self, *extra, button="press-the-button"):
        argv = ["--u64", self.u64, "--sym-elf", self.sym_elf, "--host", "u64.test"]
        if button:
            argv += ["--power-button-cmd", button]
        return argv + list(extra)

    def main(self, *extra, **kwargs):
        return fu.main(self.argv(*extra, **kwargs))

    @property
    def log(self):
        return self.out.getvalue()

    @property
    def gdb(self):
        return self.bench.gdbs[-1]

    @property
    def server(self):
        return self.bench.servers[-1]


class Flash(BenchTest):
    def test_whole_run(self):
        self.assertEqual(self.main(), 0, self.log)
        gdb = self.gdb
        self.assertEqual(gdb.returns, EXPECTED_ANSWERS)
        first_continue = gdb.commands.index("-exec-continue")
        self.assertEqual(gdb.commands[:first_continue], [
            "-gdb-set confirm off", "-gdb-set remotetimeout 30", "-target-select remote :2342",
            '-interpreter-exec console "set $status = 0"',
            '-interpreter-exec console "set $ienable = 0"',
            '-interpreter-exec console "set *(unsigned char *)0x4000000 = 0"',
            f'-interpreter-exec console "set $pc = {ENTRY:#x}"',
            *[f"-break-insert *{addr:#x}" for addr in ADDR.values()]])
        self.assertEqual(gdb.commands[-3:], ["-break-delete", "-exec-continue", "-gdb-exit"])
        self.assertEqual(gdb.returncode, 0)
        self.assertEqual(self.bench.events, [])
        self.assertEqual(self.bench.downloaded, IMAGE)
        self.assertEqual(self.bench.presses, [False])
        self.assertEqual(self.bench.lock_held, [True, True])
        self.assertIn(["nios2-gdb-server", "--tcpport", "2342", "--tcppersist"], self.bench.ran)
        # Attached for 25 s after turn_off(), then 5 s before checking that it is off.
        self.assertEqual(self.clock.slept[:2], [25, 5])
        for line in ("before: 3.15 5ee21b65 FPGA 122",
                     "popup 'Reformat Flash Disk?' -> NO",
                     "popup 'About to update. Continue?' -> YES",
                     "writing flash at 0x0\n", "writing flash at 0x290000\n",
                     "checking the WiFi module",
                     "popup 'Reset Configuration? (Recommended)' -> NO",
                     "updater finished", "machine is off", "pressing the power button (1/2)",
                     "after: 3.15a d5686424f FPGA 126 errors []"):
            self.assertIn(line, self.log)
        self.assertTrue(self.log.rstrip().endswith("done"))

    def test_gdb_server_is_stopped_and_reaped(self):
        self.assertEqual(self.main(), 0, self.log)
        self.assertTrue(self.server.terminated)
        self.assertTrue(self.server.reaped)

    def test_server_that_ignores_sigterm_is_killed(self):
        self.bench.server_ignores_term = True
        self.assertEqual(self.main(), 0, self.log)
        self.assertTrue(self.server.killed)
        self.assertTrue(self.server.reaped)

    def test_no_temporary_files_are_left(self):
        self.assertEqual(self.main(), 0, self.log)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_no_temporary_files_are_left_after_a_failed_download(self):
        self.bench.download_output = "Error: target not responding\n"
        self.assertEqual(self.main(), 1)
        self.assertIn("nios2-download: Error: target not responding", self.log)
        self.assertEqual(self.bench.servers, [])
        self.assertEqual(os.listdir(self.scratch), [])

    def test_wrap_failure(self):
        self.bench.failing["nios2-elf-ld"] = "nios2-elf-ld: cannot open linker script"
        self.assertEqual(self.main(), 1)
        self.assertIn("FAILED: nios2-elf-ld: nios2-elf-ld: cannot open linker script", self.log)
        self.assertIsNone(self.bench.downloaded)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_no_rest_before_is_not_an_error(self):
        self.bench.info_before = None
        self.assertEqual(self.main(), 0, self.log)
        self.assertNotIn("before:", self.log)

    def test_no_fpga_on_the_chain(self):
        self.bench.fpga_visible = False
        self.assertEqual(self.main(), 1)
        self.assertIn("no U64 FPGA on the JTAG chain", self.log)
        self.assertIsNone(self.bench.downloaded)

    def test_symbol_lookup_reports_a_failing_nm(self):
        self.bench.failing["nios2-elf-nm"] = "nios2-elf-nm: 'update.elf': No such file"
        self.assertEqual(self.main(), 1)
        self.assertIn("No such file", self.log)

    def test_symbol_elf_from_another_build_is_refused(self):
        other = bytearray(IMAGE)
        other[0x200:0x240] = bytes(0x40)
        with open(self.sym_elf, "wb") as handle:
            handle.write(other)
        self.assertEqual(self.main(), 1)
        self.assertIn("the code differs from the symbol ELF", self.log)
        self.assertIsNone(self.bench.downloaded)

    def test_machine_that_still_pings_is_not_pressed(self):
        self.bench.ping_alive = True
        self.assertEqual(self.main(), 1)
        self.assertIn("did not switch itself off", self.log)
        self.assertEqual(self.bench.presses, [])

    def test_machine_still_on_the_jtag_chain_is_not_pressed(self):
        self.bench.fpga_visible = True
        self.assertEqual(self.main(), 1)
        self.assertIn("did not switch itself off", self.log)
        self.assertEqual(self.bench.presses, [])


class Popups(BenchTest):
    def test_unknown_popup_is_left_unanswered(self):
        self.bench.set_events([popup("Error reading Flash Disk. Format?", YES_NO)])
        self.assertEqual(self.main(), 2)
        self.assertIn("unexpected popup 'Error reading Flash Disk. Format?' (buttons 0x6)", self.log)
        self.assertEqual(self.gdb.returns, [])
        self.assertFalse(any("$r2" in c for c in self.gdb.commands))
        self.assertEqual(self.bench.presses, [])

    def test_known_text_with_other_buttons_is_left_unanswered(self):
        self.bench.set_events([popup("Reformat Flash Disk?", fu.OK)])
        self.assertEqual(self.main(), 2)
        self.assertFalse(any("$r2" in c for c in self.gdb.commands))

    def test_unanswered_popup_keeps_the_server_for_resume(self):
        self.bench.set_events([popup("Error reading Flash Disk. Format?", YES_NO)])
        self.assertEqual(self.main(), 2)
        self.assertFalse(self.server.terminated)
        self.assertIn("pid 4242", self.log)

    def test_unanswered_popup_ends_gdb(self):
        self.bench.set_events([popup("Error reading Flash Disk. Format?", YES_NO)])
        self.assertEqual(self.main(), 2)
        self.assertIsNotNone(self.gdb.returncode)
        self.assertTrue(self.gdb.stdin_closed)

    def test_unexpected_stop(self):
        self.bench.set_events([stop_at("popup", pc=0x03000f00)])
        self.assertEqual(self.main(), 3)
        self.assertIn("unexpected stop at 0x03000f00", self.log)
        self.assertTrue(self.server.reaped)
        self.assertIsNotNone(self.gdb.returncode)

    def test_gdb_that_ignores_eof_is_killed(self):
        self.bench.set_events([stop_at("popup", pc=0x03000f00)])
        self.bench.gdb_ignores_eof = True
        self.assertEqual(self.main(), 3)
        self.assertTrue(self.gdb.killed)

    def test_updater_that_never_stops_times_out(self):
        self.bench.set_events([popup("Reformat Flash Disk?", YES_NO)])
        self.assertEqual(self.main(), 1)
        self.assertIn("gdb timed out", self.log)
        self.assertTrue(self.server.reaped)


class Resume(BenchTest):
    def test_resume_at_popup(self):
        run = updater_run()
        self.bench.set_events(run[2:], halted=run[1])
        self.assertEqual(self.main("--resume"), 0, self.log)
        self.assertEqual(self.gdb.returns, EXPECTED_ANSWERS[1:])
        self.assertIsNone(self.bench.downloaded)
        self.assertEqual(self.bench.servers, [])
        self.assertFalse(any("set $pc = 0x3000100" in c for c in self.gdb.commands))
        self.assertEqual(self.bench.presses, [False])

    def test_resume_elsewhere_is_refused(self):
        self.bench.halted_pc = ADDR["flash_buffer_at"]
        self.assertEqual(self.main("--resume"), 4)
        self.assertIn("--resume: the CPU is not halted at popup()", self.log)
        self.assertEqual(self.gdb.inserted, [])


class PowerOn(BenchTest):
    def test_person_presses_the_button(self):
        self.bench.person_after = 3
        self.assertEqual(self.main(button=""), 0, self.log)
        self.assertIn("press the U64 power button now", self.log)
        self.assertEqual(self.bench.presses, [])

    def test_person_never_presses(self):
        start = self.clock.now
        self.assertEqual(self.main(button=""), 1)
        self.assertIn("the machine did not come back on", self.log)
        self.assertGreaterEqual(self.clock.now - start, 3600)
        self.assertLess(self.clock.now - start, 3700)

    def test_second_press_when_the_first_does_not_take(self):
        self.bench.press_works = [False, True]
        self.assertEqual(self.main("--up-timeout", "30"), 0, self.log)
        self.assertEqual(self.bench.presses, [False, False])
        self.assertIn("pressing the power button (2/2)", self.log)

    def test_two_presses_that_do_not_take(self):
        self.bench.press_works = [False, False]
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.bench.presses, [False, False])
        self.assertIn("the machine did not come back on", self.log)

    def test_failing_button_command_is_logged(self):
        self.bench.press_rc = 1
        self.assertEqual(self.main(), 0, self.log)
        self.assertIn("power button command exited 1", self.log)

    def test_no_second_press_on_a_machine_that_is_on(self):
        self.bench.rest = False
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.bench.presses, [False])
        self.assertIn("is on but", self.log)


class ExpectCommit(BenchTest):
    def test_match(self):
        self.assertEqual(self.main("--expect-commit", "d5686424f"), 0, self.log)

    def test_mismatch(self):
        self.assertEqual(self.main("--expect-commit", "abcdef123"), 1)
        self.assertIn("expected commit abcdef123, the device reports d5686424f", self.log)

    def test_shorter_hash_on_the_device_matches(self):
        self.bench.info_after["git_commit_hash"] = "d5686424"
        self.assertEqual(self.main("--expect-commit", "d5686424f"), 0, self.log)

    def test_full_hash_expected_matches(self):
        self.assertEqual(self.main("--expect-commit", "d5686424f" + "0" * 31), 0, self.log)

    def test_missing_hash_does_not_match(self):
        del self.bench.info_after["git_commit_hash"]
        self.assertEqual(self.main("--expect-commit", "d5686424f"), 1)


class Main(BenchTest):
    def test_missing_u64_file(self):
        os.remove(self.u64)
        self.assertEqual(self.main(), 1)
        self.assertIn("FAILED:", self.log)

    def test_missing_tool(self):
        self.bench.missing_tools.add("nios2-elf-gdb")
        self.assertEqual(self.main(), 1)
        self.assertIn("FAILED:", self.log)
        self.assertTrue(self.server.reaped)

    def test_gdb_does_not_start(self):
        self.bench.gdb_starts = False
        self.assertEqual(self.main(), 1)
        self.assertIn("gdb exited", self.log)
        self.assertTrue(self.gdb.stdout.closed)
        self.assertTrue(self.server.reaped)

    def test_gdb_refuses_the_connection(self):
        self.bench.gdb_errors["-target-select"] = "Remote connection closed"
        self.assertEqual(self.main(), 1)
        self.assertIn('gdb -target-select remote :2342: ^error,msg="Remote connection closed"', self.log)
        self.assertTrue(self.server.reaped)
        self.assertIsNotNone(self.gdb.returncode)

    def test_host_and_button_from_the_environment(self):
        os.environ["U64_HOST"] = "envhost"
        os.environ["U64_POWER_BUTTON_CMD"] = "env-button"
        argv = ["--u64", self.u64, "--sym-elf", self.sym_elf]
        self.assertEqual(fu.main(argv), 0, self.log)
        self.assertIn("http://envhost/v1/info", self.bench.urls)
        self.assertEqual(self.bench.presses, [False])

    def test_intel_tools_missing(self):
        del os.environ["INTEL_FPGA_ROOT"]
        os.environ.pop("QUARTUS_ROOTDIR", None)
        os.environ["HOME"] = self.dir
        with mock.patch.object(fu.glob, "glob", return_value=[]):
            self.assertEqual(self.main(), 1)
        self.assertIn("could not locate the Intel FPGA tools", self.log)
        self.assertEqual(self.bench.ran, [])

    def test_lock_in_use(self):
        os.makedirs(os.path.dirname(self.lock_path))
        with open(self.lock_path, "a+") as other:
            fu.fcntl.flock(other.fileno(), fu.fcntl.LOCK_EX | fu.fcntl.LOCK_NB)
            other.write("pid=999999 cmd=another-run\n")
            other.flush()
            with mock.patch.object(fu, "ancestors", return_value={11, 12}):
                self.assertEqual(self.main(), 1)
        self.assertIn("the u64 is in use", self.log)
        self.assertEqual(self.bench.ran, [])

    def test_run_as_a_script(self):
        with mock.patch.object(sys, "argv", ["flash_u64.py", "--help"]):
            with self.assertRaises(SystemExit) as exit:
                runpy.run_path(fu.__file__, run_name="__main__")
        self.assertEqual(exit.exception.code, 0)
        self.assertIn("--power-button-cmd", self.log)


class GdbServer(BenchTest):
    def test_waits_until_the_port_answers(self):
        self.bench.listen_after = 3
        server = fu.start_gdb_server(2342)
        self.assertIs(server, self.server)
        self.assertEqual(sum(self.clock.slept), 1.0)

    def test_server_that_never_listens_is_killed(self):
        self.bench.listen_after = 10 ** 6
        with self.assertRaisesRegex(fu.FlashError, "did not start"):
            fu.start_gdb_server(2342)
        self.assertTrue(self.server.killed)
        self.assertTrue(self.server.reaped)

    def test_server_that_exits_is_reported_at_once(self):
        self.bench.server_exit = 1
        with self.assertRaisesRegex(fu.FlashError, "exited"):
            fu.start_gdb_server(2342)
        self.assertLess(sum(self.clock.slept), 1)

    def test_port_in_use_is_refused(self):
        self.bench.port_busy = True
        with self.assertRaisesRegex(fu.FlashError, "2342 is already in use"):
            fu.start_gdb_server(2342)
        self.assertEqual(self.bench.servers, [])


class GdbMi(BenchTest):
    def test_error_record(self):
        gdb = fu.Gdb()
        self.bench.gdb_errors["-gdb-set"] = "No symbol table"
        with self.assertRaisesRegex(fu.FlashError, "gdb -gdb-set confirm off: \\^error"):
            gdb.cmd("-gdb-set confirm off")
        gdb.close()

    def test_string_ends_at_the_nul(self):
        gdb = fu.Gdb()
        self.bench.memory[0x100] = "Gr\xfc\xdfe\0junk".encode("latin-1")
        self.assertEqual(gdb.string(0x100), "Gr\xfc\xdfe")
        gdb.close()

    def test_silent_gdb_times_out(self):
        gdb = fu.Gdb()
        with self.assertRaisesRegex(fu.FlashError, "gdb timed out"):
            gdb.read_until(lambda line: False, 5)
        gdb.close()

    def test_gdb_exiting_mid_read(self):
        gdb = fu.Gdb()
        self.gdb.exit(1)
        with self.assertRaisesRegex(fu.FlashError, "gdb exited"):
            gdb.read_until(lambda line: False, 5)
        gdb.close()

    def test_close_after_gdb_has_gone(self):
        gdb = fu.Gdb()
        self.gdb.exit(1)

        def broken_pipe():
            raise BrokenPipeError("unflushed input")
        self.gdb.stdin.close = broken_pipe
        gdb.close()
        self.assertTrue(self.gdb.stdout.closed)

    def test_lines_split_across_reads(self):
        gdb = fu.Gdb()
        self.gdb.emit("~\"one\"\n^do")
        self.gdb.emit("ne\n")
        self.assertEqual(gdb.read_until(lambda line: line.startswith("^"), 5), ['~"one"', "^done"])
        gdb.close()


class Toolchain(BenchTest):
    def test_round_trip(self):
        workdir = os.path.join(self.dir, "work")
        os.mkdir(workdir)
        elf = fu.wrap_elf(IMAGE, LOAD, ENTRY, workdir)
        with open(elf, "rb") as handle:
            self.assertEqual(handle.read(), IMAGE)
        self.assertEqual(self.bench.ld_script,
                         "ENTRY(_start)\n_start = 0x3000100;\nSECTIONS { . = 0x3000000; .text : { *(.data) } }\n")
        self.assertEqual(self.bench.ran[0][:7], ["nios2-elf-objcopy", "-I", "binary", "-O",
                                                 "elf32-littlenios2", "-B", "nios2"])

    def test_round_trip_mismatch(self):
        self.bench.damage_elf = True
        with self.assertRaisesRegex(fu.FlashError, "does not reproduce the image"):
            fu.wrap_elf(IMAGE, LOAD, ENTRY, self.dir)

    def test_objcopy_failure(self):
        self.bench.failing["nios2-elf-objcopy"] = "bad format"
        with self.assertRaisesRegex(fu.FlashError, "nios2-elf-objcopy: bad format"):
            fu.wrap_elf(IMAGE, LOAD, ENTRY, self.dir)
        with self.assertRaisesRegex(fu.FlashError, f"nios2-elf-objcopy {self.sym_elf}: bad format"):
            fu.elf_image(self.sym_elf)

    def test_elf_image_closes_its_file(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(fu.elf_image(self.sym_elf), IMAGE)
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_fpga_on_chain(self):
        self.assertTrue(fu.fpga_on_chain())
        self.bench.on = False
        self.assertFalse(fu.fpga_on_chain())

    def test_rest_info(self):
        self.assertEqual(fu.rest_info("u64.test")["git_commit_hash"], "5ee21b65")
        self.bench.on = False
        self.assertIsNone(fu.rest_info("u64.test"))

    def test_pingable(self):
        self.assertTrue(fu.pingable("u64.test"))
        self.assertEqual(self.bench.ran[-1], ["ping", "-c1", "-W1", "u64.test"])
        self.bench.on = False
        self.assertFalse(fu.pingable("u64.test"))


class IntelEnv(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.enterContext(mock.patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": self.dir}))
        os.environ.pop("INTEL_FPGA_ROOT", None)
        os.environ.pop("QUARTUS_ROOTDIR", None)
        real_glob = glob.glob
        opt = os.path.join(self.dir, "opt")
        self.enterContext(mock.patch.object(
            fu.glob, "glob", lambda pattern: real_glob(pattern.replace("/opt/", opt + "/", 1)
                                                       if pattern.startswith("/opt/") else pattern)))

    def install(self, path, executable=True):
        tool = os.path.join(self.dir, path, "quartus", "bin", "jtagconfig")
        os.makedirs(os.path.dirname(tool))
        with open(tool, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(tool, 0o755 if executable else 0o644)
        return os.path.join(self.dir, path)

    def test_intel_fpga_root(self):
        os.environ["INTEL_FPGA_ROOT"] = "/x/18.1"
        env = fu.intel_env()
        self.assertEqual(env["QUARTUS_ROOTDIR"], "/x/18.1/quartus")
        self.assertEqual(env["PATH"], "/x/18.1/quartus/bin:/x/18.1/nios2eds/bin:"
                         "/x/18.1/nios2eds/bin/gnu/H-x86_64-pc-linux-gnu/bin:/usr/bin")
        self.assertNotIn("QUARTUS_ROOTDIR", os.environ)

    def test_quartus_rootdir(self):
        os.environ["QUARTUS_ROOTDIR"] = "/y/20.1/quartus/"
        self.assertEqual(fu.intel_env()["QUARTUS_ROOTDIR"], "/y/20.1/quartus")

    def test_home_install_with_a_working_jtagconfig(self):
        self.install("intelFPGA_lite/17.1", executable=False)
        root = self.install("intelFPGA_lite/18.1")
        self.install("opt/intelFPGA_lite/19.1")
        self.assertEqual(fu.intel_env()["QUARTUS_ROOTDIR"], root + "/quartus")

    def test_opt_install(self):
        root = self.install("opt/altera_lite/20.1")
        self.assertEqual(fu.intel_env()["QUARTUS_ROOTDIR"], root + "/quartus")

    def test_nothing_installed(self):
        with self.assertRaisesRegex(fu.FlashError, "could not locate the Intel FPGA tools"):
            fu.intel_env()


class DeviceLock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "locks", "u64.lock")
        self.enterContext(mock.patch.object(fu, "LOCK_PATH", self.path))
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("FLASH_U64_LOCK", None)
        self.enterContext(mock.patch.object(fu, "ancestors", return_value={11, 12}))

    def held_by(self, holder):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        other = open(self.path, "a+")
        self.addCleanup(other.close)
        fu.fcntl.flock(other.fileno(), fu.fcntl.LOCK_EX | fu.fcntl.LOCK_NB)
        other.write(holder)
        other.flush()

    def free(self):
        with open(self.path, "a+") as other:
            try:
                fu.fcntl.flock(other.fileno(), fu.fcntl.LOCK_EX | fu.fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    def test_free_lock_is_taken_and_released(self):
        with fu.DeviceLock():
            self.assertFalse(self.free())
        self.assertTrue(self.free())

    def test_lock_held_by_an_ancestor_covers_this_run(self):
        self.held_by("pid=12 cmd=with-device-locks u64\n")
        with fu.DeviceLock() as lock:
            self.assertIsNone(lock.handle)

    def test_lock_held_by_another_process(self):
        self.held_by("pid=4711 cmd=other\n")
        with self.assertRaisesRegex(fu.FlashError, "the u64 is in use .*pid=4711 cmd=other"):
            with fu.DeviceLock():
                pass

    def test_lock_held_without_a_holder_line(self):
        self.held_by("")
        with self.assertRaisesRegex(fu.FlashError, ": held\\)"):
            with fu.DeviceLock():
                pass

    def test_lock_off(self):
        os.environ["FLASH_U64_LOCK"] = "off"
        with fu.DeviceLock() as lock:
            self.assertIsNone(lock.handle)
        self.assertFalse(os.path.exists(self.path))


class Ancestors(unittest.TestCase):
    def proc(self, parents):
        def fake_open(path):
            pid = int(path.split("/")[2])
            if pid not in parents:
                raise FileNotFoundError(path)
            return io.StringIO(f"{pid} (odd) name) S {parents[pid]} 1 1 0")
        self.enterContext(mock.patch.object(fu, "open", fake_open, create=True))
        self.enterContext(mock.patch.object(fu.os, "getppid", return_value=300))

    def test_this_process(self):
        self.assertIn(os.getppid(), fu.ancestors())

    def test_walk_stops_at_init(self):
        self.proc({300: 400, 400: 1})
        self.assertEqual(fu.ancestors(), {300, 400})

    def test_walk_stops_at_a_loop(self):
        self.proc({300: 400, 400: 300})
        self.assertEqual(fu.ancestors(), {300, 400})

    def test_walk_stops_at_an_unreadable_process(self):
        self.proc({300: 400})
        self.assertEqual(fu.ancestors(), {300, 400})


if __name__ == "__main__":
    unittest.main()
