#!/usr/bin/env python3
"""Host tests for u64ii_gdbstub.py against a synthetic FreeRTOS memory image.

The image holds the task lists, control blocks and trap frames in the layout
the firmware's FreeRTOS RISC-V port uses; the tests check the tasks the stub
finds, the registers it reports for each, and its handling of gdb packets,
including a full exchange over a socket.

    python3 tooling/test_u64ii_gdbstub.py
"""

import os
import socket
import struct
import sys
import threading
import unittest

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


if __name__ == "__main__":
    unittest.main()
