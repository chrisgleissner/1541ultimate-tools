#!/usr/bin/env python3
"""Host tests for u64ii_gdbstub.py against a synthetic FreeRTOS memory image.

The image holds the task lists, control blocks and trap frames in the layout
the firmware's FreeRTOS RISC-V port uses; the tests check the tasks the stub
finds, the registers it reports for each, and its handling of gdb packets,
including full exchanges over a loopback socket (acks, no-ack mode, interrupts,
multi-packet replies, hang-up). Symbol reading runs against a faked `nm`, and
main() against a faked u64ii_jtag board, so nothing here touches hardware.

    python3 tooling/test_u64ii_gdbstub.py
"""

import contextlib
import io
import os
import runpy
import socket
import struct
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import u64ii_gdbstub as gs  # noqa: E402


class Image:
    def __init__(self):
        self.mem = bytearray(0x40000)

    def read(self, address, length):
        return bytes(self.mem[address:address + length])

    def write(self, address, data):
        self.mem[address:address + len(data)] = data

    def put(self, address, *words):
        struct.pack_into(f"<{len(words)}L", self.mem, address, *words)


SYMBOLS = {
    "pxCurrentTCB": (0x1000, 4),
    "__global_pointer$": (0x8800, 0),
    "pxReadyTasksLists": (0x2000, 5 * gs.LIST_BYTES),
    "xDelayedTaskList1": (0x2100, gs.LIST_BYTES),
    "xDelayedTaskList2": (0x2120, gs.LIST_BYTES),
    "xPendingReadyList": (0x2140, gs.LIST_BYTES),
    "xSuspendedTaskList": (0x2160, gs.LIST_BYTES),
    "xTasksWaitingTermination": (0x2180, gs.LIST_BYTES),
}


def build():
    """Three tasks: 'Main' running (in ready list 2), 'IDLE' ready (list 0), 'USB' blocked."""
    img = Image()
    for name, (addr, size) in SYMBOLS.items():
        if name.startswith("px") and name != "pxCurrentTCB" or name.startswith("x"):
            for i in range(max(1, size // gs.LIST_BYTES)):
                lst = addr + i * gs.LIST_BYTES
                end = lst + gs.LIST_END
                img.put(lst, 0, end, 0xFFFFFFFF, end, end)          # empty list

    def add(tcb, name, prio, lst, top, pc):
        end = lst + gs.LIST_END
        item = tcb + gs.TCB_STATE_LIST_ITEM
        img.put(item, 0, end, end, tcb, lst)
        img.put(end + 4, item, item)                                # xListEnd.next/prev
        img.put(lst, 1)
        img.put(tcb + gs.TCB_TOP_OF_STACK, top)
        img.put(tcb + gs.TCB_PRIORITY, prio)
        img.mem[tcb + gs.TCB_NAME:tcb + gs.TCB_NAME + len(name)] = name.encode()
        frame = [0] * gs.FRAME_WORDS
        frame[gs.FRAME_PC] = pc
        frame[gs.FRAME_RA] = pc + 0x100
        for i in range(5, 32):
            frame[gs.FRAME_X5 + i - 5] = 0x1000_0000 * 0 + (tcb << 8) + i
        img.put(top, *frame)

    add(0x3000, "Main", 2, 0x2000 + 2 * gs.LIST_BYTES, 0x10000, 0x31234)
    add(0x3400, "IDLE", 0, 0x2000, 0x11000, 0x35678)
    add(0x3800, "USB", 1, 0x2100, 0x12000, 0x39ABC)
    img.put(0x1000, 0x3000)
    return img


class TargetTest(unittest.TestCase):
    def setUp(self):
        self.img = build()
        self.t = gs.Target(self.img.read, None, SYMBOLS)

    def test_tasks_and_states(self):
        tasks = {t.name: t for t in self.t.tasks()}
        self.assertEqual(set(tasks), {"Main", "IDLE", "USB"})
        self.assertEqual(tasks["Main"].state, "running")
        self.assertEqual(tasks["IDLE"].state, "ready")
        self.assertEqual(tasks["USB"].state, "blocked")
        self.assertEqual(self.t.tasks()[0].name, "Main")        # running task first

    def test_registers_from_frame(self):
        usb = next(t for t in self.t.tasks() if t.name == "USB")
        regs = self.t.registers(usb)
        self.assertEqual(regs[32], 0x39ABC)                       # pc
        self.assertEqual(regs[1], 0x39ABC + 0x100)                # ra
        self.assertEqual(regs[2], 0x12000 + gs.FRAME_BYTES)       # sp
        self.assertEqual(regs[3], 0x8800)                         # gp
        self.assertEqual(regs[10], (0x3800 << 8) + 10)            # a0

    def test_unaligned_read_and_io_refused(self):
        self.img.mem[0x5001:0x5004] = b"abc"
        self.assertEqual(self.t.read(0x5001, 3), b"abc")
        with self.assertRaises(gs.StubError):
            self.t.read(0x10000000, 4)

    def test_writes_need_permission(self):
        with self.assertRaises(gs.StubError):
            self.t.write(0x5000, b"x")
        writable = gs.Target(self.img.read, self.img.write, SYMBOLS)
        writable.write(0x5002, b"zz")
        self.assertEqual(self.img.mem[0x5000:0x5004], b"\0\0zz")


class PacketTest(unittest.TestCase):
    def setUp(self):
        self.s = gs.Session(gs.Target(build().read, None, SYMBOLS))

    def test_malformed_packets_get_an_error_reply(self):
        for packet in ("", "mzz,4", "m10", "Hgzz", "p-1", "M10:zz", "qThreadExtraInfo,"):
            self.assertEqual(self.s.handle(packet), "E01", packet)
        self.assertEqual(self.s.handle("qfThreadInfo"), "m3000,3400,3800")

    def test_jtag_failure_gets_an_error_reply(self):
        def broken(address, length):
            raise OSError("adapter gone")
        s = gs.Session(gs.Target(build().read, None, SYMBOLS))
        s.t._read = broken
        self.assertEqual(s.handle("m1000,4"), "E01")

    def test_threads_and_selection(self):
        self.assertEqual(self.s.handle("qfThreadInfo"), "m3000,3400,3800")
        self.assertEqual(self.s.handle("qsThreadInfo"), "l")
        self.assertEqual(self.s.handle("?"), "T05thread:3000;")
        self.assertEqual(self.s.handle("Hg3800"), "OK")
        g = self.s.handle("g")
        self.assertEqual(len(g), 33 * 8)
        self.assertEqual(g[32 * 8:], struct.pack("<L", 0x39ABC).hex())
        self.assertEqual(self.s.handle("Hg0"), "OK")              # "any thread" keeps 3800
        self.assertEqual(self.s.handle("g")[32 * 8:], struct.pack("<L", 0x39ABC).hex())
        info = bytes.fromhex(self.s.handle("qThreadExtraInfo,3000")).decode()
        self.assertIn("running", info)

    def test_memory_and_refusals(self):
        self.assertEqual(self.s.handle("m3034,4"), b"Main".hex())
        self.assertEqual(self.s.handle("M5000,1:ff"), "E01")      # read-only
        self.assertEqual(self.s.handle("G" + "00" * 132), "E01")
        reply = self.s.handle("c")
        self.assertIsInstance(reply, list)
        self.assertTrue(reply[0].startswith("O"))
        self.assertTrue(reply[1].startswith("T05"))
        self.assertEqual(self.s.handle("vCont?"), "")
        self.assertIsNone(self.s.handle("k"))


class SocketTest(unittest.TestCase):
    def test_gdb_exchange(self):
        session = gs.Session(gs.Target(build().read, None, SYMBOLS))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        threading.Thread(target=gs.serve, args=(session, port), daemon=True).start()
        for _ in range(50):
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                threading.Event().wait(0.05)

        def send(body):
            c.sendall(b"$" + body + b"#" + gs.checksum(body))
            data = b""
            while data.count(b"#") < 1 or len(data) < data.rfind(b"#") + 3:
                data += c.recv(4096)
            return data

        self.assertTrue(send(b"qSupported").startswith(b"+$PacketSize"))
        self.assertIn(b"T05thread:3000", send(b"?"))
        c.close()


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
NM_OUTPUT = """\
00001000 00000004 B pxCurrentTCB
00008800 A __global_pointer$
00002000 00000064 b pxReadyTasksLists
00004000 T both_forms
00004000 00000010 T both_forms
00005000 00000008 T sized_first
00005004 t sized_first
         U undefined_symbol
garbage
"""


class ReadSymbolsTest(unittest.TestCase):
    def test_falls_back_through_the_nm_tools_and_parses_both_line_forms(self):
        tried = []

        def run(cmd, **kwargs):
            tried.append(cmd[0])
            self.assertEqual(cmd[1:], ["-S", "app.elf"])
            self.assertEqual(kwargs, {"capture_output": True, "text": True, "check": True})
            if cmd[0] == "riscv32-unknown-elf-nm":
                raise OSError("not installed")
            if cmd[0] != "nm":
                raise subprocess.CalledProcessError(1, cmd)
            return types.SimpleNamespace(stdout=NM_OUTPUT)

        with mock.patch("subprocess.run", side_effect=run):
            symbols = gs.read_symbols("app.elf")
        self.assertEqual(tried, ["riscv32-unknown-elf-nm",
                                 os.path.expanduser("~/riscv/bin/riscv32-unknown-elf-nm"), "nm"])
        self.assertEqual(symbols, {
            "pxCurrentTCB": (0x1000, 4),
            "__global_pointer$": (0x8800, 0),          # no size column: size 0
            "pxReadyTasksLists": (0x2000, 0x64),
            "both_forms": (0x4000, 0x10),              # a sized line wins over an unsized one
            "sized_first": (0x5000, 8),                # ... in either order
        })

    def test_no_nm_is_an_error(self):
        with mock.patch("subprocess.run", side_effect=OSError("none")):
            with self.assertRaisesRegex(gs.StubError, "no nm found"):
                gs.read_symbols("app.elf")


# ---------------------------------------------------------------------------
# Target: memory and task discovery
# ---------------------------------------------------------------------------
def unlink_all(img):
    """Empty every list in the image (the control blocks stay in memory)."""
    for name, (addr, size) in SYMBOLS.items():
        if name in ("pxCurrentTCB", "__global_pointer$"):
            continue
        for i in range(max(1, size // gs.LIST_BYTES)):
            lst = addr + i * gs.LIST_BYTES
            end = lst + gs.LIST_END
            img.put(lst, 0, end, 0xFFFFFFFF, end, end)


class TargetMemoryTest(unittest.TestCase):
    def setUp(self):
        self.img = build()
        self.reads, self.writes = [], []

        def read(address, length):
            self.reads.append((address, length))
            return self.img.read(address, length)

        def write(address, data):
            self.writes.append((address, bytes(data)))
            self.img.write(address, data)

        self.t = gs.Target(read, write, SYMBOLS)

    def test_every_required_symbol_is_checked(self):
        for name in ("pxCurrentTCB", "__global_pointer$") + tuple(n for n, _ in gs.TASK_LISTS):
            symbols = dict(SYMBOLS)
            del symbols[name]
            with self.assertRaises(gs.StubError) as cm:
                gs.Target(self.img.read, None, symbols)
            self.assertIn(name, str(cm.exception))

    def test_reads_are_widened_to_whole_words(self):
        self.img.mem[0x5000:0x5008] = bytes(range(1, 9))
        self.assertEqual(self.t.read(0x5003, 2), b"\x04\x05")
        self.assertEqual(self.reads, [(0x5000, 8)])
        self.assertEqual(self.t.read(0x5004, 0), b"")

    def test_word_is_little_endian(self):
        self.img.put(0x5000, 0x11223344)
        self.assertEqual(self.t.word(0x5000), 0x11223344)

    def test_reads_outside_ram_are_refused_without_touching_jtag(self):
        for address, length in ((-4, 4), (0x5000, -1), (gs.MEMORY_LIMIT - 2, 4)):
            with self.assertRaisesRegex(gs.StubError, "read outside RAM"):
                self.t.read(address, length)
        self.assertEqual(self.reads, [])

    def test_last_ram_word_is_readable(self):
        t = gs.Target(lambda a, n: b"\xAA" * n, None, SYMBOLS)
        self.assertEqual(t.read(gs.MEMORY_LIMIT - 4, 4), b"\xAA" * 4)

    def test_unaligned_write_preserves_neighbouring_bytes(self):
        self.img.mem[0x5000:0x5008] = b"ABCDEFGH"
        self.t.write(0x5003, b"xy")
        self.assertEqual(self.writes, [(0x5000, b"ABCxyFGH")])
        self.assertEqual(self.img.mem[0x5000:0x5008], b"ABCxyFGH")

    def test_writes_outside_ram_are_refused(self):
        for address, data in ((-1, b"x"), (gs.MEMORY_LIMIT - 1, b"xy")):
            with self.assertRaisesRegex(gs.StubError, "write outside RAM"):
                self.t.write(address, data)
        self.assertEqual(self.writes, [])


class TargetTasksTest(unittest.TestCase):
    def setUp(self):
        self.img = build()
        self.t = gs.Target(self.img.read, None, SYMBOLS)

    def link(self, lst, *tcbs):
        """Rebuild the list at `lst` so it holds `tcbs` in order."""
        end = lst + gs.LIST_END
        items = [tcb + gs.TCB_STATE_LIST_ITEM for tcb in tcbs]
        nxt = items[1:] + [end]
        for item, tcb, n in zip(items, tcbs, nxt):
            self.img.put(item + gs.ITEM_NEXT, n)
            self.img.put(item + gs.ITEM_OWNER, tcb)
        self.img.put(lst, len(tcbs))
        self.img.put(end + gs.ITEM_NEXT, items[0] if items else end)

    def test_task_fields(self):
        tasks = {t.tcb: t for t in self.t.tasks()}
        usb = tasks[0x3800]
        self.assertEqual((usb.name, usb.state, usb.priority, usb.top), ("USB", "blocked", 1, 0x12000))

    def test_running_task_not_in_any_list_is_still_found(self):
        unlink_all(self.img)
        tasks = self.t.tasks()
        self.assertEqual([(t.tcb, t.name, t.state) for t in tasks], [(0x3000, "Main", "running")])

    def test_no_current_task_means_none_is_running(self):
        self.img.put(0x1000, 0)
        tasks = self.t.tasks()
        self.assertEqual([(t.tcb, t.state) for t in tasks],
                         [(0x3000, "ready"), (0x3400, "ready"), (0x3800, "blocked")])

    def test_first_list_wins_and_null_owners_are_skipped(self):
        # A task seen in two lists (the firmware moved it during the read)
        # keeps the state of the first list walked; a zero owner is ignored.
        self.link(SYMBOLS["xSuspendedTaskList"][0], 0x3400)
        self.img.put(0x3C00 + gs.TCB_STATE_LIST_ITEM + gs.ITEM_OWNER, 0)
        self.link(SYMBOLS["xTasksWaitingTermination"][0], 0x3C00)
        self.img.put(0x3C00 + gs.TCB_STATE_LIST_ITEM + gs.ITEM_OWNER, 0)
        tasks = {t.tcb: t.state for t in self.t.tasks()}
        self.assertEqual(tasks, {0x3000: "running", 0x3400: "ready", 0x3800: "blocked"})

    def test_lists_of_size_zero_symbols_are_still_walked(self):
        symbols = dict(SYMBOLS, xDelayedTaskList1=(0x2100, 0))
        t = gs.Target(self.img.read, None, symbols)
        self.assertIn(0x3800, {task.tcb for task in t.tasks()})

    def test_every_ready_list_is_walked(self):
        self.link(0x2000 + 4 * gs.LIST_BYTES, 0x3C00)
        self.img.mem[0x3C00 + gs.TCB_NAME:0x3C00 + gs.TCB_NAME + 4] = b"Tmr\0"
        self.assertIn((0x3C00, "Tmr", "ready"), [(t.tcb, t.name, t.state) for t in self.t.tasks()])

    def test_suspended_and_deleted_states(self):
        unlink_all(self.img)
        self.link(SYMBOLS["xSuspendedTaskList"][0], 0x3400)
        self.link(SYMBOLS["xTasksWaitingTermination"][0], 0x3800)
        states = {t.tcb: t.state for t in self.t.tasks()}
        self.assertEqual(states, {0x3000: "running", 0x3400: "suspended", 0x3800: "deleted"})

    def test_corrupt_cyclic_list_is_bounded(self):
        lst = SYMBOLS["xDelayedTaskList2"][0]
        item = 0x3C00 + gs.TCB_STATE_LIST_ITEM
        self.img.put(lst, 100000)                                  # absurd count
        self.img.put(lst + gs.LIST_END + gs.ITEM_NEXT, item)
        self.img.put(item + gs.ITEM_NEXT, item)                     # points at itself
        self.img.put(item + gs.ITEM_OWNER, 0x3C00)
        self.assertEqual(self.t._walk(lst), [0x3C00] * 256)
        # The walk does not run forever, and the owner is reported once.
        self.assertEqual(sum(t.tcb == 0x3C00 for t in self.t.tasks()), 1)

    def test_walk_follows_the_chain_in_order(self):
        lst = SYMBOLS["xDelayedTaskList2"][0]
        self.link(lst, 0x3400, 0x3800, 0x3C00)
        self.assertEqual(self.t._walk(lst), [0x3400, 0x3800, 0x3C00])

    def test_name_is_nul_terminated_and_bounded_to_sixteen_bytes(self):
        base = 0x3400 + gs.TCB_NAME
        self.img.mem[base:base + 20] = b"ABCDEFGHIJKLMNOPQRST"
        self.img.mem[0x3000 + gs.TCB_NAME:0x3000 + gs.TCB_NAME + 6] = b"Caf\xe9\0x"
        names = {t.tcb: t.name for t in self.t.tasks()}
        self.assertEqual(names[0x3400], "ABCDEFGHIJKLMNOP")
        self.assertEqual(names[0x3000], "Caf\xe9")

    def test_register_layout(self):
        main = next(t for t in self.t.tasks() if t.name == "Main")
        regs = self.t.registers(main)
        self.assertEqual(len(regs), 33)
        self.assertEqual(regs[0], 0)                               # x0
        self.assertEqual(regs[4], 0)                               # tp is not saved
        self.assertEqual(regs[5], (0x3000 << 8) + 5)               # t0 = frame[2]
        self.assertEqual(regs[31], (0x3000 << 8) + 31)             # t6 = frame[28]
        self.assertEqual(regs[32], 0x31234)


# ---------------------------------------------------------------------------
# Session: packet handling
# ---------------------------------------------------------------------------
def regs_hex(value):
    return struct.pack("<L", value).hex()


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.img = build()
        self.s = gs.Session(gs.Target(self.img.read, self.img.write, SYMBOLS))

    def pc(self):
        return self.s.handle("g")[32 * 8:]

    def test_no_tasks_is_an_error(self):
        img = build()
        unlink_all(img)
        img.put(0x1000, 0)
        with self.assertRaisesRegex(gs.StubError, "no FreeRTOS tasks"):
            gs.Session(gs.Target(img.read, None, SYMBOLS))

    def test_first_task_is_selected_initially(self):
        self.assertEqual(self.s.selected, 0x3000)
        self.assertEqual(self.pc(), regs_hex(0x31234))

    def test_query_packets(self):
        self.assertEqual(self.s.handle("qSupported:multiprocess+;swbreak+"),
                         "PacketSize=4000;QStartNoAckMode+;qXfer:threads:read-")
        self.assertEqual(self.s.handle("qAttached"), "1")
        self.assertEqual(self.s.handle("qC"), "QC3000")
        self.assertEqual(self.s.handle("qTStatus"), "")              # unsupported: empty
        self.assertEqual(self.s.handle("vMustReplyEmpty"), "")
        self.assertEqual(self.s.handle("vCont?"), "")
        self.assertEqual(self.s.handle("vKill;1"), "")
        self.assertEqual(self.s.handle("D"), "OK")
        self.assertEqual(self.s.handle("D;3000"), "OK")
        self.assertEqual(self.s.handle("Z0,31234,4"), "")            # no breakpoints

    def test_no_ack_mode(self):
        self.assertFalse(self.s.no_ack)
        self.assertEqual(self.s.handle("QStartNoAckMode"), "OK")
        self.assertTrue(self.s.no_ack)

    def test_thread_extra_info(self):
        info = bytes.fromhex(self.s.handle("qThreadExtraInfo,3800")).decode()
        self.assertEqual(info, "USB [blocked, prio 1]")
        info = bytes.fromhex(self.s.handle("qThreadExtraInfo,3000")).decode()
        self.assertEqual(info, "Main [running, prio 2] (registers from its last trap)")
        self.assertEqual(self.s.handle("qThreadExtraInfo,9999"), "E01")

    def test_thread_alive(self):
        self.assertEqual(self.s.handle("T3400"), "OK")
        self.assertEqual(self.s.handle("T9999"), "E01")
        self.assertEqual(self.s.handle("T"), "E01")

    def test_thread_selection(self):
        self.assertEqual(self.s.handle("Hg3400"), "OK")
        self.assertEqual(self.pc(), regs_hex(0x35678))
        self.assertEqual(self.s.handle("Hg-1"), "OK")                # all threads: keep
        self.assertEqual(self.s.handle("Hc3800"), "OK")              # Hc does not select
        self.assertEqual(self.pc(), regs_hex(0x35678))
        self.assertEqual(self.s.handle("Hg9999"), "E01")             # unknown: unchanged
        self.assertEqual(self.s.selected, 0x3400)
        self.assertEqual(self.s.handle("Hg"), "")                    # no thread id at all

    def test_selection_survives_refresh_unless_the_task_is_gone(self):
        self.s.handle("Hg3800")
        self.assertEqual(self.s.handle("?"), "T05thread:3000;")
        self.assertEqual(self.s.selected, 0x3800)
        # USB leaves every list: the next refresh falls back to the first task.
        end = 0x2100 + gs.LIST_END
        self.img.put(0x2100, 0, end, 0xFFFFFFFF, end, end)
        self.assertEqual(self.s.handle("qfThreadInfo"), "m3000,3400")
        self.assertEqual(self.s.selected, 0x3000)
        self.assertEqual(self.pc(), regs_hex(0x31234))

    def test_single_register(self):
        self.s.handle("Hg3800")
        self.assertEqual(self.s.handle("p20"), regs_hex(0x39ABC))     # pc
        self.assertEqual(self.s.handle("p1"), regs_hex(0x39ABC + 0x100))
        self.assertEqual(self.s.handle("p2"), regs_hex(0x12000 + gs.FRAME_BYTES))
        self.assertEqual(self.s.handle("p3"), regs_hex(0x8800))
        self.assertEqual(self.s.handle("p21"), "E01")                 # past pc
        self.assertEqual(self.s.handle("p"), "E01")

    def test_g_packet_matches_every_register(self):
        regs = self.s.t.registers(self.s.task())
        self.assertEqual(self.s.handle("g"), "".join(regs_hex(r) for r in regs))

    def test_memory_read_is_capped(self):
        self.assertEqual(len(self.s.handle("m0,1000")), 0x800 * 2)
        self.assertEqual(self.s.handle("m3034,0"), "")
        self.assertEqual(self.s.handle("m10000000,4"), "E01")         # I/O space

    def test_memory_write_when_allowed(self):
        self.assertEqual(self.s.handle("M5001,2:abcd"), "OK")
        self.assertEqual(self.img.mem[0x5000:0x5004], b"\0\xab\xcd\0")
        self.assertEqual(self.s.handle("M5001,2:abc"), "E01")         # odd hex
        self.assertEqual(self.s.handle("M5001,2"), "E01")             # no data
        self.assertEqual(self.s.handle("M%x,1:00" % gs.MEMORY_LIMIT), "E01")

    def test_register_writes_are_refused(self):
        self.assertEqual(self.s.handle("P20=00000000"), "E01")
        self.assertEqual(self.s.handle("G" + "00" * 132), "E01")

    def test_resume_requests_report_and_stop(self):
        note = "The CPU cannot be resumed or stepped over JTAG; this is a read-only view.\n"
        for packet in ("c", "s", "C05", "S05", "vCont;c", "vCont;s:3000"):
            reply = self.s.handle(packet)
            self.assertEqual(reply, ["O" + note.encode().hex(), "T05thread:3000;"], packet)

    def test_kill_hangs_up(self):
        self.assertIsNone(self.s.handle("k"))

    def test_struct_error_is_an_error_reply(self):
        self.s.t.registers = mock.Mock(side_effect=struct.error("short frame"))
        self.assertEqual(self.s.handle("g"), "E01")


# ---------------------------------------------------------------------------
# Socket framing
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, port):
        for _ in range(100):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                threading.Event().wait(0.02)
        self.buf = b""

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def byte(self):
        self._need(1)
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def packet(self):
        """One `$body#cc` frame; the checksum must be right."""
        self._need(1)
        assert self.buf[:1] == b"$", self.buf
        while b"#" not in self.buf or len(self.buf) < self.buf.index(b"#") + 3:
            self._need(len(self.buf) + 1)
        end = self.buf.index(b"#")
        body, cs, self.buf = self.buf[1:end], self.buf[end + 1:end + 3], self.buf[end + 3:]
        assert cs == gs.checksum(body), (body, cs)
        return body.decode("latin-1")

    def send(self, body):
        self.sock.sendall(b"$" + body.encode() + b"#" + gs.checksum(body.encode()))

    def closed(self):
        try:
            self._need(len(self.buf) + 1)
        except (EOFError, ConnectionResetError):
            return True
        return False


class SocketFramingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gs.Session(gs.Target(build().read, None, SYMBOLS))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        threading.Thread(target=gs.serve, args=(cls.session, cls.port), daemon=True).start()

    def setUp(self):
        self.c = Client(self.port)
        self.addCleanup(self.c.sock.close)

    def test_checksum(self):
        self.assertEqual(gs.checksum(b""), b"00")
        self.assertEqual(gs.checksum(b"OK"), b"9a")
        self.assertEqual(gs.checksum(b"\xff\xff"), b"fe")             # modulo 256

    def test_hexle(self):
        self.assertEqual(gs.hexle(0x12345678), "78563412")
        self.assertEqual(gs.hexle(-1), "ffffffff")
        self.assertEqual(gs.hexle(0x1_0000_0001), "01000000")

    def test_acks_are_skipped_and_replies_acked(self):
        self.c.sock.sendall(b"+-+")
        self.c.send("qC")
        self.assertEqual(self.c.byte(), b"+")
        self.assertEqual(self.c.packet(), "QC3000")

    def test_no_ack_mode_and_reset_on_reconnect(self):
        self.c.send("QStartNoAckMode")
        self.assertEqual(self.c.byte(), b"+")                         # acked before the switch
        self.assertEqual(self.c.packet(), "OK")
        self.c.send("qC")
        self.assertEqual(self.c.packet(), "QC3000")                   # no '+' any more
        self.c.send("k")
        self.assertTrue(self.c.closed())
        # A new connection starts in ack mode again.
        c2 = Client(self.port)
        self.addCleanup(c2.sock.close)
        c2.send("qC")
        self.assertEqual(c2.byte(), b"+")
        self.assertEqual(c2.packet(), "QC3000")

    def test_interrupt_gets_a_stop_reply(self):
        self.c.sock.sendall(b"\x03")
        self.assertEqual(self.c.packet(), "T05thread:3000;")

    def test_multi_packet_reply(self):
        self.c.send("c")
        self.assertEqual(self.c.byte(), b"+")
        self.assertTrue(self.c.packet().startswith("O"))
        self.assertEqual(self.c.packet(), "T05thread:3000;")

    def test_packet_split_across_segments(self):
        self.c.sock.sendall(b"$qAtt")
        threading.Event().wait(0.05)
        self.c.sock.sendall(b"ached#" + gs.checksum(b"qAttached")[:1])
        threading.Event().wait(0.05)
        self.c.sock.sendall(gs.checksum(b"qAttached")[1:])
        self.assertEqual(self.c.byte(), b"+")
        self.assertEqual(self.c.packet(), "1")

    def test_two_packets_in_one_segment(self):
        self.c.sock.sendall(b"$qC#" + gs.checksum(b"qC") + b"$qAttached#" + gs.checksum(b"qAttached"))
        self.assertEqual(self.c.byte(), b"+")
        self.assertEqual(self.c.packet(), "QC3000")
        self.assertEqual(self.c.byte(), b"+")
        self.assertEqual(self.c.packet(), "1")

    def test_error_reply_keeps_the_connection(self):
        self.c.send("m10000000,4")
        self.assertEqual(self.c.byte(), b"+")
        self.assertEqual(self.c.packet(), "E01")
        self.c.send("qC")
        self.assertEqual(self.c.byte(), b"+")
        self.assertEqual(self.c.packet(), "QC3000")

    def test_client_hang_up_lets_the_next_one_in(self):
        self.c.sock.close()
        c2 = Client(self.port)
        self.addCleanup(c2.sock.close)
        c2.send("qAttached")
        self.assertEqual(c2.byte(), b"+")
        self.assertEqual(c2.packet(), "1")


# ---------------------------------------------------------------------------
# main(), with the JTAG board faked
# ---------------------------------------------------------------------------
def fake_jtag(img, calls):
    mod = types.ModuleType("u64ii_jtag")

    class DeviceLock:
        def __enter__(self):
            calls.append("lock")
            return self

        def __exit__(self, *exc):
            calls.append("unlock")

    class Board:
        def __init__(self, **kwargs):
            calls.append(("board", kwargs))
            self.chain = types.SimpleNamespace(read=img.read, write=img.write)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append("close")

        def identify(self):
            calls.append("identify")

        def require_design(self):
            calls.append("require_design")

    mod.DeviceLock, mod.Board = DeviceLock, Board
    return mod


class MainTest(unittest.TestCase):
    def run_main(self, argv):
        img, calls, served = build(), [], []

        def serve(session, port):
            served.append((session, port))
            raise KeyboardInterrupt

        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"u64ii_jtag": fake_jtag(img, calls)}), \
                mock.patch.object(gs, "read_symbols", return_value=SYMBOLS) as rs, \
                mock.patch.object(gs, "serve", side_effect=serve), \
                contextlib.redirect_stderr(err):
            rc = gs.main(argv)
        rs.assert_called_once_with("app.elf")
        return rc, img, calls, served, err.getvalue()

    def test_defaults(self):
        rc, img, calls, served, err = self.run_main(["--elf", "app.elf"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["lock", ("board", {}), "identify", "require_design", "close", "unlock"])
        session, port = served[0]
        self.assertEqual(port, 3333)
        self.assertIsNone(session.t._write)                           # read-only by default
        self.assertEqual([t.tcb for t in session.tasks], [0x3000, 0x3400, 0x3800])
        self.assertIn("task 00003000 Main             running   prio 2", err)
        self.assertIn("task 00003800 USB              blocked   prio 1", err)

    def test_options(self):
        rc, img, calls, served, err = self.run_main(
            ["--elf", "app.elf", "--port", "4444", "--allow-writes",
             "--url", "ftdi://ftdi:232h/1", "--frequency", "5e6"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[1], ("board", {"url": "ftdi://ftdi:232h/1", "frequency": 5e6}))
        session, port = served[0]
        self.assertEqual(port, 4444)
        self.assertEqual(session.handle("M5000,1:7f"), "OK")
        self.assertEqual(img.mem[0x5000], 0x7F)

    def test_elf_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gs.main([])
        self.assertEqual(cm.exception.code, 2)

    def test_script_reports_stub_errors_and_exits_1(self):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["u64ii_gdbstub.py", "--elf", "app.elf"]), \
                mock.patch("subprocess.run", side_effect=OSError("none")), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            runpy.run_path(gs.__file__, run_name="__main__")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[u64ii-gdb] ERROR: no nm found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
