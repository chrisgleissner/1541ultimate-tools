#!/usr/bin/env python3
"""gdb server for an Ultimate 64 Elite II or C64 Ultimate, over JTAG.

The CPU of these boards has no debug module, so this server does not stop or
step it. What it offers instead is everything gdb can do with memory and saved
registers:

  - memory reads through the FPGA's JTAG memory port, while the firmware keeps
    running (the CPU has no data cache, so what is read is what it wrote);
  - every FreeRTOS task as a gdb thread, with the registers the task had when
    it last trapped. The RISC-V port of FreeRTOS saves them in a 30-word frame
    on the task's stack and stores the frame address in the task's control
    block (software/FreeRTOS/Source/portable/risc-v/port_asm.S), so a
    backtrace of any task that is not running shows where it waits, and after
    a GURU MEDITATION the crashed task's frame shows where it faulted;
  - with --allow-writes, memory writes.

`continue` and `step` are refused. The task that is running at the moment of a
read has no saved frame that is current; its registers are those of its last
trap, which gdb marks as a snapshot in the thread list.

Nothing here is linked into or patched into the firmware. The server needs the
ELF the running application was built from, for its symbols.

Usage (see also u64ii_gdb.sh, which starts both sides):
    u64ii_gdbstub.py --elf target/u64ii/riscv/ultimate/result/ultimate.elf
    riscv32-unknown-elf-gdb ultimate.elf -ex 'target extended-remote :3333'
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# FreeRTOS RISC-V port trap frame (port_asm.S): portCONTEXT_SIZE = 30 words.
FRAME_WORDS = 30
FRAME_BYTES = FRAME_WORDS * 4
FRAME_PC, FRAME_RA, FRAME_X5, FRAME_MSTATUS = 0, 1, 2, 29

# FreeRTOS structures as this firmware builds them (checked against the ELF:
# ptype/o struct tskTaskControlBlock, List_t, ListItem_t).
TCB_TOP_OF_STACK = 0
TCB_STATE_LIST_ITEM = 4
TCB_PRIORITY = 44
TCB_NAME = 52
TCB_NAME_LEN = 16
LIST_ITEMS = 0
LIST_END = 8                    # MiniListItem_t xListEnd
ITEM_NEXT = 4
ITEM_OWNER = 12
LIST_BYTES = 20

TASK_LISTS = (
    ("pxReadyTasksLists", "ready"),
    ("xDelayedTaskList1", "blocked"),
    ("xDelayedTaskList2", "blocked"),
    ("xPendingReadyList", "ready"),
    ("xSuspendedTaskList", "suspended"),
    ("xTasksWaitingTermination", "deleted"),
)

# The system memory the JTAG port can read without side effects: DDR, below
# the I/O space. Reading I/O registers could pop FIFOs, so it is refused.
MEMORY_LIMIT = 0x10000000


class StubError(Exception):
    pass


def log(message: str) -> None:
    print(f"[u64ii-gdb] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
def read_symbols(elf: str) -> Dict[str, Tuple[int, int]]:
    """name -> (address, size), from nm; the plain host nm reads RISC-V ELFs."""
    for tool in ("riscv32-unknown-elf-nm", os.path.expanduser("~/riscv/bin/riscv32-unknown-elf-nm"), "nm"):
        try:
            out = subprocess.run([tool, "-S", elf], capture_output=True, text=True, check=True).stdout
            break
        except (OSError, subprocess.CalledProcessError):
            continue
    else:
        raise StubError("no nm found to read the ELF's symbols")
    symbols = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4:
            symbols[parts[3]] = (int(parts[0], 16), int(parts[1], 16))
        elif len(parts) == 3:
            symbols.setdefault(parts[2], (int(parts[0], 16), 0))
    return symbols


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
class Task:
    def __init__(self, tcb: int, name: str, state: str, priority: int, top: int):
        self.tcb, self.name, self.state, self.priority, self.top = tcb, name, state, priority, top


class Target:
    """Memory access plus FreeRTOS knowledge; `read`/`write` are injected."""

    def __init__(self, read: Callable[[int, int], bytes], write: Optional[Callable[[int, bytes], None]],
                 symbols: Dict[str, Tuple[int, int]]):
        self._read, self._write, self.sym = read, write, symbols
        for name in ("pxCurrentTCB", "__global_pointer$") + tuple(n for n, _ in TASK_LISTS):
            if name not in symbols:
                raise StubError(f"symbol {name} not in the ELF; is it the running application's?")

    # -- memory -------------------------------------------------------------
    def read(self, address: int, length: int) -> bytes:
        if address < 0 or length < 0 or address + length > MEMORY_LIMIT:
            raise StubError(f"read outside RAM at 0x{address:X}")
        start, end = address & ~3, (address + length + 3) & ~3
        return self._read(start, end - start)[address - start:address - start + length]

    def word(self, address: int) -> int:
        return struct.unpack("<L", self.read(address, 4))[0]

    def write(self, address: int, data: bytes) -> None:
        if self._write is None:
            raise StubError("memory writes are off; start the server with --allow-writes")
        if address < 0 or address + len(data) > MEMORY_LIMIT:
            raise StubError(f"write outside RAM at 0x{address:X}")
        start, end = address & ~3, (address + len(data) + 3) & ~3
        block = bytearray(self._read(start, end - start))
        block[address - start:address - start + len(data)] = data
        self._write(start, bytes(block))

    # -- tasks --------------------------------------------------------------
    def _walk(self, list_addr: int) -> List[int]:
        count = self.word(list_addr + LIST_ITEMS)
        end = list_addr + LIST_END
        owners, item = [], self.word(end + ITEM_NEXT)
        while item != end and len(owners) <= count and len(owners) < 256:
            owners.append(self.word(item + ITEM_OWNER))
            item = self.word(item + ITEM_NEXT)
        return owners

    def tasks(self) -> List[Task]:
        current = self.word(self.sym["pxCurrentTCB"][0])
        found: Dict[int, Task] = {}
        for name, state in TASK_LISTS:
            base, size = self.sym[name]
            for i in range(max(1, size // LIST_BYTES)):
                for tcb in self._walk(base + i * LIST_BYTES):
                    if tcb and tcb not in found:
                        found[tcb] = self._task(tcb, state)
        if current and current not in found:
            found[current] = self._task(current, "running")
        elif current:
            found[current].state = "running"
        return sorted(found.values(), key=lambda t: (t.state != "running", t.tcb))

    def _task(self, tcb: int, state: str) -> Task:
        raw = self.read(tcb, TCB_NAME + TCB_NAME_LEN)
        name = raw[TCB_NAME:TCB_NAME + TCB_NAME_LEN].split(b"\0")[0].decode("latin-1")
        top, prio = struct.unpack_from("<L", raw, TCB_TOP_OF_STACK)[0], struct.unpack_from("<L", raw, TCB_PRIORITY)[0]
        return Task(tcb, name, state, prio, top)

    def registers(self, task: Task) -> List[int]:
        """x0-x31 and pc from the task's saved trap frame."""
        frame = struct.unpack(f"<{FRAME_WORDS}L", self.read(task.top, FRAME_BYTES))
        regs = [0] * 33
        regs[1] = frame[FRAME_RA]
        regs[2] = task.top + FRAME_BYTES
        regs[3] = self.sym["__global_pointer$"][0]
        regs[4] = 0                                   # tp is not saved by the port
        for i in range(5, 32):
            regs[i] = frame[FRAME_X5 + i - 5]
        regs[32] = frame[FRAME_PC]
        return regs


# ---------------------------------------------------------------------------
# gdb remote serial protocol
# ---------------------------------------------------------------------------
def checksum(data: bytes) -> bytes:
    return b"%02x" % (sum(data) & 0xFF)


def hexle(value: int) -> str:
    return struct.pack("<L", value & 0xFFFFFFFF).hex()


class Session:
    """Handles one gdb connection's packets; independent of sockets for testing."""

    def __init__(self, target: Target):
        self.t = target
        self.tasks: List[Task] = []
        self.selected: Optional[int] = None
        self.no_ack = False
        self.refresh()

    def refresh(self) -> None:
        self.tasks = self.t.tasks()
        if not self.tasks:
            raise StubError("no FreeRTOS tasks found; is the scheduler running?")
        if self.selected not in {t.tcb for t in self.tasks}:
            self.selected = self.tasks[0].tcb

    def task(self, tid: Optional[int] = None) -> Task:
        tid = self.selected if tid is None else tid
        for t in self.tasks:
            if t.tcb == tid:
                return t
        raise StubError(f"no task {tid:x}")

    def stop_reply(self) -> str:
        return f"T05thread:{self.tasks[0].tcb:x};"

    def handle(self, packet: str):
        """Reply to one packet: a string, a list of packets, or None to hang up."""
        try:
            return self._handle(packet)
        except StubError as exc:
            log(str(exc))
            return "E01"
        except (ValueError, IndexError, struct.error) as exc:
            # A malformed packet (empty, bad hex, missing field) must not end the server.
            log(f"malformed packet {packet[:40]!r}: {exc}")
            return "E01"
        except OSError as exc:
            # The JTAG adapter failed during a transfer; report it, keep serving.
            log(f"JTAG access failed: {exc}")
            return "E01"

    def _handle(self, p: str) -> Optional[str]:
        if p.startswith("qSupported"):
            return "PacketSize=4000;QStartNoAckMode+;qXfer:threads:read-"
        if p == "QStartNoAckMode":
            self.no_ack = True
            return "OK"
        if p == "?":
            self.refresh()
            return self.stop_reply()
        if p in ("qAttached",):
            return "1"
        if p == "qC":
            return f"QC{self.tasks[0].tcb:x}"
        if p == "qfThreadInfo":
            self.refresh()
            return "m" + ",".join(f"{t.tcb:x}" for t in self.tasks)
        if p == "qsThreadInfo":
            return "l"
        if p.startswith("qThreadExtraInfo,"):
            t = self.task(int(p.split(",")[1], 16))
            note = " (registers from its last trap)" if t.state == "running" else ""
            return f"{t.name} [{t.state}, prio {t.priority}]{note}".encode().hex()
        if p[0] == "H" and len(p) > 2:
            # Hg0 means "any thread" and Hg-1 "all threads": keep the selection.
            if p[1] == "g" and p[2:] not in ("0", "-1"):
                tid = int(p[2:], 16)
                self.task(tid)
                self.selected = tid
            return "OK"
        if p[0] == "T":
            return "OK" if int(p[1:], 16) in {t.tcb for t in self.tasks} else "E01"
        if p == "g":
            return "".join(hexle(r) for r in self.t.registers(self.task()))
        if p[0] == "p":
            n = int(p[1:], 16)
            regs = self.t.registers(self.task())
            return hexle(regs[n]) if 0 <= n < len(regs) else "E01"
        if p[0] == "m":
            addr, length = (int(x, 16) for x in p[1:].split(","))
            return self.t.read(addr, min(length, 0x800)).hex()
        if p[0] == "M":
            where, data = p[1:].split(":")
            addr, _ = (int(x, 16) for x in where.split(","))
            self.t.write(addr, bytes.fromhex(data))
            return "OK"
        if p[0] in ("G", "P"):
            return "E01"                   # registers of a live CPU cannot be set
        if p[0] in ("c", "s", "C", "S") or p.startswith("vCont;"):
            # gdb waits for a stop reply after these; send the reason first.
            note = ("The CPU cannot be resumed or stepped over JTAG; "
                    "this is a read-only view.\n").encode().hex()
            self.refresh()
            return ["O" + note, self.stop_reply()]
        if p.startswith("vCont?") or p.startswith("vMustReplyEmpty") or p.startswith("vKill"):
            return ""
        if p in ("D",) or p.startswith("D;"):
            return "OK"
        if p == "k":
            return None
        return ""                          # unsupported: empty reply


def serve(session: Session, port: int) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    log(f"listening on 127.0.0.1:{port}; in gdb: target extended-remote :{port}")
    while True:
        conn, _ = listener.accept()
        log("gdb connected")
        session.no_ack = False
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    if buf[:1] in (b"+", b"-"):
                        buf = buf[1:]
                        continue
                    if buf[:1] == b"\x03":         # interrupt: nothing is running
                        buf = buf[1:]
                        reply = session.stop_reply()
                        conn.sendall(b"$" + reply.encode() + b"#" + checksum(reply.encode()))
                        continue
                    start = buf.find(b"$")
                    end = buf.find(b"#", start)
                    if start < 0 or end < 0 or len(buf) < end + 3:
                        break
                    body = buf[start + 1:end]
                    buf = buf[end + 3:]
                    if not session.no_ack:
                        conn.sendall(b"+")
                    reply = session.handle(body.decode("latin-1"))
                    if reply is None:
                        raise ConnectionResetError
                    for part in reply if isinstance(reply, list) else [reply]:
                        data = part.encode("latin-1")
                        conn.sendall(b"$" + data + b"#" + checksum(data))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            conn.close()
            log("gdb disconnected")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elf", required=True, help="ELF of the running application")
    parser.add_argument("--port", type=int, default=3333)
    parser.add_argument("--allow-writes", action="store_true", help="let gdb write memory")
    parser.add_argument("--url", default=None, help="pyftdi URL of the FT232H, or blaster")
    parser.add_argument("--frequency", type=float, default=None)
    args = parser.parse_args(argv)

    import u64ii_jtag as jt
    symbols = read_symbols(args.elf)
    kwargs = {}
    if args.url:
        kwargs["url"] = args.url
    if args.frequency:
        kwargs["frequency"] = args.frequency
    with jt.DeviceLock(), jt.Board(**kwargs) as board:
        board.identify()
        board.require_design()
        target = Target(board.chain.read, board.chain.write if args.allow_writes else None, symbols)
        session = Session(target)
        for t in session.tasks:
            log(f"task {t.tcb:08x} {t.name:16} {t.state:9} prio {t.priority}")
        try:
            serve(session, args.port)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StubError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
