"""Constrained Nios II debug checks via Intel's existing USB-Blaster backend.

No GDB command interpreter, images, register writes or breakpoint packets are
exposed. See docs/jtag-verify.md for the disruptive test preconditions.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import time
import traceback
import urllib.request

from u64ii_gdbstub import checksum

# Intel 18.1 maps the putative bstatus GDB slot to an unavailable register.
# No live stepping until pre-debug interrupt context has independent evidence.
STEPPING_QUALIFIED = False
RAM_END = 0x02000000  # Conservative subset of U64's 64 MiB DDR, never I/O.


class DebugError(RuntimeError):
    pass


class DebugRunError(DebugError):
    def __init__(self, detail, evidence):
        super().__init__(detail)
        self.evidence = evidence


def ram(address, count):
    if type(address) is not int or address & 3 or not 0x20 <= address < address + count <= RAM_END:
        raise DebugError("debug read/step is outside aligned application DDR")


def predict(word, registers):
    """Only R1 immediate ALU and direct conditional branches; never loads/stores.

    A partial ISA oracle is deliberate: skip other instructions rather than let
    a debugger implement stepping by planting a software breakpoint.
    """
    op, a, b = word & 63, word >> 27, (word >> 22) & 31
    immediate = (word >> 6) & 0xFFFF
    signed = immediate if immediate < 0x8000 else immediate - 0x10000
    expected = list(registers[:33])
    expected[32] += 4
    if op == 0x04:  # addi
        expected[b] = (registers[a] + signed) & 0xFFFFFFFF
    elif op == 0x34:  # orhi, including movhi
        expected[b] = registers[a] | (immediate << 16)
    elif op in (0x06, 0x26, 0x2E):  # br, beq, bgeu
        if op == 0x06 or (op == 0x26 and registers[a] == registers[b]) or (
                op == 0x2E and registers[a] >= registers[b]):
            expected[32] += signed
    else:
        return None
    expected[0] = 0
    ram(expected[32], 4)
    return expected


class DebugSession:
    """Fixed RSP policy beneath the tests. The socket is not a public interface."""
    def __init__(self, connection):
        self.__connection = connection
        self.resumed = False
        self.__outside_interrupt = False

    def __byte(self):
        remaining = self.__deadline - time.monotonic()
        if remaining <= 0:
            raise DebugError("RSP reply deadline reached")
        self.__connection.settimeout(min(remaining, 2))
        value = self.__connection.recv(1)
        if not value:
            raise DebugError("debug connection closed")
        return value

    def __exchange(self, payload):
        self.__deadline = time.monotonic() + 2
        self.__connection.settimeout(2)
        self.__connection.sendall(b"$" + payload + b"#" + checksum(payload))
        return self.__reply()

    def __reply(self):
        # A negative acknowledgement is a measured failure, never retried.
        while True:
            first = self.__byte()
            if first == b"$":
                break
            if first not in (b"+",):
                raise DebugError("unexpected RSP acknowledgement")
        raw = bytearray()
        while True:
            byte = self.__byte()
            if byte == b"#":
                break
            raw += byte
            if len(raw) > 4096:
                raise DebugError("oversized RSP reply")
        received = self.__byte() + self.__byte()
        if checksum(raw) != received.lower():
            raise DebugError("RSP checksum mismatch")
        self.__connection.sendall(b"+")
        # RSP escaping/run-length encoding are applied after wire checksum.
        decoded, index = bytearray(), 0
        while index < len(raw):
            value = raw[index]
            if value == 0x7D:
                index += 1
                if index == len(raw):
                    raise DebugError("truncated RSP escape")
                decoded.append(raw[index] ^ 0x20)
            elif value == 0x2A:
                index += 1
                if not decoded or index == len(raw) or not 29 <= raw[index] <= 126:
                    raise DebugError("invalid RSP repeat")
                decoded.extend(bytes((decoded[-1],)) * (raw[index] - 29))
            else:
                decoded.append(value)
            if len(decoded) > 4096:
                raise DebugError("oversized decoded RSP reply")
            index += 1
        return bytes(decoded)

    def stopped(self):
        reply = self.__exchange(b"?")
        if not reply.startswith((b"S05", b"T05", b"S02", b"T02")):
            raise DebugError(f"unexpected debug stop: {reply[:80]!r}")

    def registers(self):
        reply = self.__exchange(b"g")
        if len(reply) < 36 * 8:
            raise DebugError("incomplete Nios II register reply")
        try:
            raw = bytes.fromhex(reply[:33 * 8].decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise DebugError("invalid Nios II register reply") from exc
        values = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
        try:
            status = int.from_bytes(bytes.fromhex(reply[35 * 8:36 * 8].decode("ascii")), "little")
            self.__outside_interrupt = status != 0xFFFFFFFF and bool(status & 1)
        except (UnicodeError, ValueError) as exc:
            raise DebugError("invalid pre-debug status") from exc
        if values[0] != 0:
            raise DebugError("Nios II zero register is not zero")
        return values

    def __read(self, address, count):
        ram(address, count)
        if count not in (4, 256):
            raise DebugError("debug read length is not permitted")
        reply = self.__exchange(f"m{address:x},{count:x}".encode("ascii"))
        try:
            raw = bytes.fromhex(reply.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise DebugError("invalid instruction readback") from exc
        if len(raw) != count:
            raise DebugError("incomplete instruction readback")
        return raw

    def __word(self, address):
        return int.from_bytes(self.__read(address, 4), "little")

    def checked_memory(self):
        address = self.registers()[32]
        first = self.__read(address, 256)
        if self.__read(address, 256) != first:
            raise DebugError("halted code-memory readback changed")
        return dict(address=f"0x{address:08x}", compared_bytes=256,
                    sha256=hashlib.sha256(first).hexdigest())

    def checked_step(self):
        if not STEPPING_QUALIFIED:
            return None
        before = self.registers()
        # Only a qualified status source could exclude interrupt/critical code.
        # The shipped Intel backend cannot establish that source; gate above.
        if not self.__outside_interrupt:
            return None
        word = self.__word(before[32])
        expected = predict(word, before)
        if expected is None:
            return None
        reply = self.__exchange(b"s")  # Server hardware step, no address override.
        if not reply.startswith((b"S05", b"T05")):
            raise DebugError(f"hardware step did not trap: {reply[:80]!r}")
        after = self.registers()
        if after != expected:
            raise DebugError(f"step oracle mismatch at PC 0x{before[32]:08x}")
        return dict(pc_before=f"0x{before[32]:08x}", instruction=f"0x{word:08x}",
                    pc_after=f"0x{after[32]:08x}")

    def resume(self):
        # Intel 18.1 acknowledges D but leaves the CPU paused. Explicit c at
        # the current PC, followed by closing a one-shot server, resumes it.
        self.__deadline = time.monotonic() + 2
        self.__connection.settimeout(2)
        self.__connection.sendall(b"$c#" + checksum(b"c"))
        if self.__byte() != b"+":
            raise DebugError("continue/resume not acknowledged")
        self.resumed = True

    def interrupt(self):
        if not self.resumed:
            raise DebugError("remote interrupt requires a running session")
        self.__deadline = time.monotonic() + 2
        self.__connection.settimeout(2)
        self.resumed = False
        self.__connection.sendall(b"\x03")
        reply = self.__reply()
        if not reply.startswith((b"S02", b"T02", b"S05", b"T05")):
            raise DebugError("remote interrupt did not stop the CPU")



def health(host):
    # Fixed read-only route: no caller-supplied path, shell or REST commands.
    if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in host):
        raise DebugError("invalid health hostname")
    with urllib.request.urlopen(f"http://{host}/v1/info", timeout=3) as response:
        info = json.loads(response.read(16384))
    if info.get("product") != "Ultimate 64 Elite" or info.get("errors"):
        raise DebugError("health endpoint is not a healthy Ultimate 64 Elite")
    return {k: info.get(k) for k in ("product", "firmware_version", "git_commit_hash", "fpga_version")}


def tools(root):
    root = Path(root).resolve()
    server = root / "quartus/bin/nios2-gdb-server"
    config = root / "quartus/bin/jtagconfig"
    if not server.is_file() or not config.is_file() or not Path("/usr/bin/stdbuf").is_file():
        raise DebugError("Intel FPGA root must contain jtagconfig and nios2-gdb-server")
    version = (root / "quartus/version.txt").read_text().splitlines()[0]
    if version != "Version=18.1.0.625":
        raise DebugError("unqualified Intel tool version; no debug attach permitted")
    return server, config


def connect_ready(port):
    """Wait only for the vendor server's local listener, not for target retries."""
    deadline = time.monotonic() + 1
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except ConnectionRefusedError:
            if time.monotonic() >= deadline:
                raise DebugError("local Intel debug listener did not start")
            time.sleep(.02)


def run(adapter, root, host, cycles, required_steps):
    """Caller holds the target lock and confirms identity, idle state and opt-in."""
    server, config = tools(root)
    if adapter.kind != "blaster" or (adapter.vid, adapter.pid) != (0x09FB, 0x6001) or not adapter.ports:
        raise DebugError("Intel debug backend supports only location-mapped 09fb:6001")
    cable = f"USB-Blaster [{adapter.bus}-" + ".".join(map(str, adapter.ports)) + "]"
    listing = subprocess.run([str(config)], check=True, capture_output=True, text=True, timeout=10).stdout
    lines = listing.splitlines()
    match = [i for i, line in enumerate(lines) if line.endswith(") " + cable)]
    if len(match) != 1 or match[0] + 1 == len(lines) or "02B050DD" not in lines[match[0] + 1].upper():
        raise DebugError("Intel cable location or target ID does not match verified adapter")
    baseline = health(host)
    evidence, memory_evidence, reads, skipped, interrupts = [], [], 0, 0, 0
    started = time.monotonic()
    for cycle in range(cycles):
        with tempfile.TemporaryDirectory(prefix="jtag-verify-debug-") as directory:
            log = Path(directory) / "server.log"
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            try:
                with log.open("w") as output:
                    # No image, reset, cache initialization, --go, or arbitrary args.
                    process = subprocess.Popen(["/usr/bin/stdbuf", "-oL", "-eL", str(server), "--cable", cable, "--device", "1",
                        "--tcpport", str(port), "--tcptimeout", "5"], stdout=output, stderr=subprocess.STDOUT)
                    session = connection = None
                    try:
                        deadline = time.monotonic() + 5
                        while True:
                            banner = log.read_text()
                            if "Processor is already paused" in banner:
                                raise DebugError("CPU was already paused; preserving its state")
                            if "Pausing target processor: OK" in banner:
                                break
                            if process.poll() is not None or time.monotonic() >= deadline:
                                raise DebugError("Intel server did not confirm a running-to-paused transition: " + banner[-500:])
                            time.sleep(0.02)
                        connection = connect_ready(port)
                        session = DebugSession(connection)
                        session.stopped()
                        snapshot = session.registers()
                        halt_deadline = time.monotonic() + .5
                        for _ in range(10):
                            if time.monotonic() >= halt_deadline:
                                break
                            if session.registers() != snapshot:
                                raise DebugError("registers changed while CPU was halted")
                            reads += 1
                        for _ in range(8):
                            if time.monotonic() >= halt_deadline:
                                break
                            step = session.checked_step()
                            if step is None:
                                skipped += 1
                                break
                            evidence.append(dict(cycle=cycle, **step))
                        memory_evidence.append(dict(cycle=cycle, **session.checked_memory()))
                        session.resume()
                        if health(host) != baseline:
                            raise DebugError("CPU did not serve matching health while continued")
                        session.interrupt()
                        interrupts += 1
                        if session.registers() != session.registers():
                            raise DebugError("registers changed after remote interrupt")
                        reads += 2
                        session.resume()
                    finally:
                        # A cleanup failure remains a failure; it is not a retry that
                        # can turn the measurement green. Never reset or redeploy.
                        try:
                            if session is None and "Processor is already paused" not in log.read_text():
                                connection = connect_ready(port)
                                session = DebugSession(connection)
                            if session is not None and not session.resumed:
                                session.resume()
                        finally:
                            if connection is not None:
                                connection.close()
                            try:
                                if session is not None and session.resumed:
                                    # Let the acknowledged continue and server exit
                                    # finish before considering process termination.
                                    process.wait(timeout=2)
                                elif process.poll() is None:
                                    process.terminate()
                                    process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                process.terminate()
                                try:
                                    process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=3)
                            if health(host) != baseline:
                                raise DebugError("identity/version changed after debug resume")
            except Exception as exc:
                raise DebugRunError(f"debug cycle {cycle} failed: {exc}", dict(
                    failed_cycle=cycle, stable_register_reads=reads, steps=evidence,
                    memory_checks=memory_evidence, remote_interrupts=interrupts,
                    server_log=log.read_text()[-8192:] if log.exists() else "",
                    exception_chain=traceback.format_exc(),
                    elapsed_s=time.monotonic() - started)) from exc
        if (cycle + 1) % 10 == 0 or cycle + 1 == cycles:
            print(f"[jtag-verify] debug {cycle + 1}/{cycles}: {reads} stable register reads, "
                  f"{len(evidence)} checked steps, {interrupts} remote interrupts, health OK", file=sys.stderr, flush=True)
    return dict(requirement="DEBUG-SESSION", safety="DISRUPTIVE",
                status="PASS" if len(evidence) >= required_steps else "UNVERIFIED",
                detail=("halt/read/continue/interrupt/resume completed; live stepping disabled: interrupt context unverified"
                        if not STEPPING_QUALIFIED else "bounded halt/read/step/continue/interrupt/resume completed"),
                backend="Intel Nios II GDB server, Quartus 18.1.0.625",
                stepping_qualification="UNVERIFIED: unavailable pre-debug interrupt status; live stepping disabled",
                cycles=cycles, stable_register_reads=reads, steps=evidence,
                memory_checks=memory_evidence, remote_interrupts=interrupts,
                required_steps=required_steps, skipped_nonpermitted_instructions=skipped,
                elapsed_s=time.monotonic() - started, health_after_each_resume="PASS",
                firmware_identity=baseline,
                capability_coverage=dict(
                    attach_halt="PASS", register_reads="PASS", code_memory_reads="PASS",
                    guarded_hardware_step="PASS" if len(evidence) >= required_steps else "UNVERIFIED",
                    remote_continue_interrupt="PASS", resume_health="PASS",
                    hardware_breakpoints="UNVERIFIED: installed bitstream resource count not measured",
                    hardware_watchpoints="UNVERIFIED: installed bitstream resource count not measured",
                    software_breakpoints="PROHIBITED: require program memory modification",
                    memory_register_writes="PROHIBITED", reset_reload="PROHIBITED",
                    source_level_step_next_finish="UNVERIFIED: no matching ELF; no software breakpoint fallback"))
