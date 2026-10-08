#!/usr/bin/env python3
"""JTAG access to an Ultimate 64 Elite II or C64 Ultimate through an FT232H
or a USB-Blaster.

These boards are not built like the Ultimate 64. The FPGA is a Xilinx Artix-7
(XC7A50T or XC7A100T), the application CPU is a RISC-V soft core, and the
application is started by a bootloader inside the FPGA image rather than by a
debugger. There is no Nios II and no nios2-download; a USB-Blaster serves only
as a plain JTAG cable. The FPGA
image offers a user JTAG chain on USER4 instead
(fpga/io/jtag/vhdl_source/jtag_client_xilinx.vhd):

  - register 0 reads the fixed word 0xDEAD1541
  - register 2 is an 8-bit output register; bit 7 holds the CPU in reset
  - registers 4-6 read and write memory over the system memory bus
  - register 0xA is a FIFO of every byte the CPU writes to its UART

The bootloader jumps to the address at 0xFFF8 when 0xFFFC holds 0x1571BABE
(software/portable/riscv/bootloader_u64ii.c). Running an application is
therefore: hold the CPU in reset, write the image to 0x30000, plant that
pair, release reset. recovery/u64ii/recover.py does the same after it has
configured the FPGA; this tool does it with or without reconfiguring.

Nothing here writes the SPI flash. Every change is volatile: a power cycle
brings back the flashed FPGA image and the flashed application.

Wiring (Adafruit FT232H, I2C mode switch OFF, 3.3 V pin NOT connected):
  D0 -> TCK (P5 pin 1)   D1 -> TDI (P5 pin 9)   D2 <- TDO (P5 pin 3)
  D3 -> TMS (P5 pin 5)   GND -> GND (P5 pin 2 or 10)

Wiring (Altera USB-Blaster or a clone, 09fb:6001, selected with --url blaster):
its 10-pin plug has the same layout as P5 and goes straight on. TCK is set by
the cable, not by --frequency.

Switch the machine on before running anything, so that the adapter never
drives the pins of an unpowered FPGA.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import struct
import sys
import time
from typing import List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 7-series instructions, 6-bit IR (UG470).
IR_LENGTH = 6
IR_CFG_IN = 0x05
IR_IDCODE = 0x09
IR_JPROGRAM = 0x0B
IR_JSTART = 0x0C
IR_USER4 = 0x23
IR_BYPASS = 0x3F
IR_CAPTURE_INIT = 1 << 4
IR_CAPTURE_DONE = 1 << 5

# Registers of the user chain.
USER_ID = 0x0
USER_OUTPUTS = 0x2
USER_DEBUG = 0x3
USER_READ_FIFO = 0x4
USER_MEM_COMMAND = 0x5
USER_MEM_DATA = 0x6
USER_CONSOLE = 0xA

USER_ID_VALUE = 0xDEAD1541
OUTPUT_CPU_RESET = 0x80

APP_ADDRESS = 0x30000
BOOT_MAGIC_ADDRESS = 0xFFF8
BOOT_MAGIC_VALUE = 0x1571BABE

# The CPU's instruction cache (fpga/cpu_unit/rvlite/vhdl_source/icache.vhd) is
# 2 KB, direct-mapped, one 32-bit word per entry, over the lowest 32 MB. Its
# reset clears the state machine but not the tags, so after a CPU-only reset it
# still serves the previous application's instructions for any address that
# application ran from. A new image at the same addresses then executes stale
# code and dies before printing anything.
ICACHE_BYTES = 2048
RISCV_NOP = 0x00000013
# Where the cache-flush trampoline goes: 2 KB aligned, below the application,
# clear of the bootloader's calibration words (0x100) and RAM test
# (0x10000-0x2FFFF), and nothing the bootloader writes.
TRAMPOLINE_ADDRESS = 0x8000
# The bootloader's own start address (target/u64ii/riscv/bootloader/linker.x);
# it runs from the boot ROM, outside the cached area.
BOOTLOADER_ADDRESS = 0x80000000

READ_COMMAND_WORDS = 256        # one read command moves at most 256 words
WRITE_CHUNK = 16384
# How many empty FIFO scans a memory read tolerates before it gives up.
READ_STALL_SCANS = 200

XILINX_MANUFACTURER = 0x093     # IDCODE bits 11..0
# IDCODE with the revision nibble masked off -> (part, bitstream in external/).
SUPPORTED = {
    0x0362C093: ("XC7A50T", "u64e2_50t.bit"),
    0x03631093: ("XC7A100T", "u64e2_100t.bit"),
}
KNOWN_OTHER = {
    0x0362E093: "XC7A15T", 0x0362D093: "XC7A35T",
    0x03632093: "XC7A75T", 0x03636093: "XC7A200T",
    0x41111043: "Lattice LFE5U-25 (an Ultimate II+L, not this board)",
}

DEFAULT_URL = os.environ.get("U64II_JTAG_URL", "ftdi://ftdi:232h/1")
# A USB-Blaster has no pyftdi vendor name of its own; Blaster.open registers
# "altera" and "usbblaster". --url blaster resolves to this URL plus a serial.
BLASTER_URL = "ftdi://altera:usbblaster/1"
DEFAULT_FREQUENCY = float(os.environ.get("U64II_JTAG_FREQUENCY", "3e6"))


class JtagError(RuntimeError):
    pass


def bitstream_idcode(data: bytes) -> Optional[int]:
    """The IDCODE a 7-series bitstream is built for, from its IDCODE register write."""
    at = data.find(b"\x30\x01\x80\x01")       # type 1 write, IDCODE register, 1 word
    if at < 0 or at + 8 > len(data):
        return None
    return int.from_bytes(data[at + 4:at + 8], "big")


# One FT232H, several users: a deploy, a console reader, a monitor. They share
# the per-device flock that `with-device-locks c64u -- ...` takes, so they
# take turns instead of failing to open the adapter or interleaving scans.
LOCK_PATH = os.path.join(os.environ.get("DEVICE_LOCK_DIR", "/tmp/1541ultimate-device-locks"),
                         "c64u.lock")


def _ancestors() -> set:
    pids, pid = set(), os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as handle:
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


class DeviceLock:
    """The c64u flock. Waits up to `wait` seconds; 0 means do not wait.

    A lock held by one of this process's ancestors (with-device-locks writes
    its pid into the file) already covers this process and is not taken again.
    """

    def __init__(self, wait: float = 120.0):
        self.wait = wait
        self.handle = None

    def __enter__(self) -> "DeviceLock":
        if os.environ.get("U64II_JTAG_LOCK") == "off":
            return self
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        handle = open(LOCK_PATH, "a+")
        deadline = time.monotonic() + self.wait
        announced = False
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return self
            except BlockingIOError:
                handle.seek(0)
                holder = handle.read()
                if any(f"pid={pid} " in holder for pid in _ancestors()):
                    handle.close()
                    return self
                if time.monotonic() >= deadline:
                    handle.close()
                    raise JtagError(f"the FT232H is in use ({LOCK_PATH}: "
                                    f"{holder.strip() or 'held'})")
                if not announced and self.wait:
                    log(f"waiting for {LOCK_PATH}")
                    announced = True
                time.sleep(0.2)

    def __exit__(self, *exc) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


def log(message: str) -> None:
    print(f"[u64ii-jtag] {message}", flush=True)


# ---------------------------------------------------------------------------
# MPSSE
# ---------------------------------------------------------------------------
# FT232H: AD0 TCK, AD1 TDI, AD2 TDO, AD3 TMS. TDI and TMS change on the
# falling edge and TDO is sampled on the rising edge.
PIN_TCK, PIN_TDI, PIN_TDO, PIN_TMS = 0x01, 0x02, 0x04, 0x08
DIRECTION = PIN_TCK | PIN_TDI | PIN_TMS

OP_W_BYTES_LSB = 0x19
OP_W_BITS_LSB = 0x1B
OP_RW_BYTES_LSB = 0x39
OP_RW_BITS_LSB = 0x3B
OP_W_BYTES_MSB = 0x11
OP_W_BITS_MSB = 0x13
OP_W_TMS = 0x4B
OP_RW_TMS = 0x6B
OP_SET_LOW = 0x80
OP_LOOPBACK_OFF = 0x85
OP_SEND_IMMEDIATE = 0x87
OP_CLOCK_BITS = 0x8E
OP_CLOCK_BYTES = 0x8F


class Mpsse:
    """Queues MPSSE commands and reads back what they sampled."""

    def __init__(self, ftdi):
        self.ftdi = ftdi
        self.pending = bytearray()
        self.expect = 0

    @classmethod
    def open(cls, url: str, frequency: float) -> "Mpsse":
        try:
            from pyftdi.ftdi import Ftdi
        except ImportError as exc:
            raise JtagError("pyftdi is missing; start this through "
                            "tooling/u64ii_jtag.sh, which provides it") from exc
        ftdi = Ftdi()
        # TMS starts high, so no stray clock can leave Test-Logic-Reset.
        ftdi.open_mpsse_from_url(url, direction=DIRECTION, initial=PIN_TMS,
                                 frequency=frequency, latency=1)
        ftdi.write_data(bytes((OP_LOOPBACK_OFF,)))
        return cls(ftdi)

    def release(self) -> None:
        """Make every pin an input and keep it so after the port closes.

        A plain close returns the FT232H to its UART mode, which drives AD0
        (TCK) and AD2 (TDO) as outputs.
        """
        try:
            self.pending.clear()
            self.expect = 0
            self.ftdi.write_data(bytes((OP_SET_LOW, 0x00, 0x00)))
            self.ftdi.close(freeze=True)
        except Exception:                                   # noqa: BLE001
            self.ftdi.close()

    def queue(self, command: bytes, reads: int = 0) -> None:
        self.pending += command
        self.expect += reads

    def flush(self) -> bytes:
        if not self.pending:
            return b""
        wanted = self.expect
        if wanted:
            self.pending.append(OP_SEND_IMMEDIATE)
        self.ftdi.write_data(bytes(self.pending))
        self.pending, self.expect = bytearray(), 0
        if not wanted:
            return b""
        data = bytes(self.ftdi.read_data_bytes(wanted, attempt=200))
        if len(data) != wanted:
            raise JtagError(f"the FT232H returned {len(data)} of {wanted} bytes")
        return data

    def tms(self, bits: int, count: int, tdi: int = 0, read: bool = False) -> None:
        """Clock up to 7 TMS bits, first bit in bit 0, with TDI held at `tdi`."""
        opcode = OP_RW_TMS if read else OP_W_TMS
        self.queue(bytes((opcode, count - 1, (bits & 0x7F) | ((tdi & 1) << 7))),
                   1 if read else 0)

    def clocks(self, count: int) -> None:
        while count >= 8:
            chunk = min(count // 8, 0x10000)
            self.queue(bytes((OP_CLOCK_BYTES, (chunk - 1) & 0xFF, (chunk - 1) >> 8)))
            count -= chunk * 8
        if count:
            self.queue(bytes((OP_CLOCK_BITS, count - 1)))

    def bytes_out(self, data: bytes, read: bool = False, msb_first: bool = False) -> None:
        opcode = (OP_W_BYTES_MSB if msb_first
                  else OP_RW_BYTES_LSB if read else OP_W_BYTES_LSB)
        for start in range(0, len(data), 0x10000):
            chunk = data[start:start + 0x10000]
            size = len(chunk) - 1
            self.queue(bytes((opcode, size & 0xFF, size >> 8)) + chunk,
                       len(chunk) if read else 0)

    def bits_out(self, value: int, count: int, read: bool = False,
                 msb_first: bool = False) -> None:
        opcode = (OP_W_BITS_MSB if msb_first
                  else OP_RW_BITS_LSB if read else OP_W_BITS_LSB)
        self.queue(bytes((opcode, count - 1, value & 0xFF)), 1 if read else 0)


# ---------------------------------------------------------------------------
# USB-Blaster
# ---------------------------------------------------------------------------
# Every byte sent is one of two kinds. Bit 7 clear: the pin levels, bit 0 TCK,
# 1 TMS, 2 nCE, 3 nCS, 4 TDI, 5 output enable (the LED), and with bit 6 set
# the cable answers one byte whose bit 0 is TDO. Bit 7 set: shift mode, bits
# 5..0 count the data bytes that follow, each clocked out LSB first with TMS
# low, and with bit 6 set every one comes back. As openFPGALoader's
# usbBlaster.cpp.
BL_TCK, BL_TMS, BL_TDI, BL_READ, BL_SHIFT = 0x01, 0x02, 0x10, 0x40, 0x80
BL_IDLE = 0x04 | 0x08 | 0x20          # nCE and nCS high, outputs enabled
BL_PACKET = 64                        # one USB packet
BL_SHIFT_MAX = 63


# Each byte with its bits in the opposite order, for bytes.translate.
_REVERSED = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


class Blaster:
    """What Mpsse offers the Tap, over a USB-Blaster.

    Reads come back in MPSSE layout so the Tap decodes them unchanged: a byte
    shift returns its bytes, a bit shift or TMS clock returns one byte with
    the sampled bits entering from the top. MSB-first shifts, which only the
    bitstream uses, are LSB-first shifts of bit-reversed bytes.
    """

    def __init__(self, ftdi):
        self.ftdi = ftdi
        self.tms_level = BL_TMS
        self.ops: List[tuple] = []      # (bytes to send, bytes read back, decoder)

    @classmethod
    def open(cls, url: str = BLASTER_URL, frequency: float = 0) -> "Blaster":
        try:
            from pyftdi.ftdi import Ftdi
        except ImportError as exc:
            raise JtagError("pyftdi is missing; start this through "
                            "tooling/u64ii_jtag.sh, which provides it") from exc
        for add, args in ((Ftdi.add_custom_vendor, (0x09FB, "altera")),
                          (Ftdi.add_custom_product, (0x09FB, 0x6001, "usbblaster"))):
            try:
                add(*args)
            except ValueError:          # already known
                pass
        ftdi = Ftdi()
        try:
            ftdi.open_from_url(url)
        except Exception as exc:                            # noqa: BLE001
            # pyftdi raises USBError, ValueError or its own errors here
            raise JtagError(f"cannot open the USB-Blaster at {url}: {exc}. Is another "
                            "program holding it, for example Quartus' jtagd?") from exc
        ftdi.set_latency_timer(2)
        ftdi.purge_buffers()
        self = cls(ftdi)
        # A cable left in shift mode takes up to 63 more bytes as data, so send
        # more than that, TMS high throughout: the TAP ends in Test-Logic-Reset.
        flush = bytes((BL_IDLE | BL_TMS, BL_IDLE | BL_TMS | BL_TCK)) * 128
        for start in range(0, len(flush), BL_PACKET):
            self._write(flush[start:start + BL_PACKET])
        self._write(bytes((BL_IDLE | BL_TMS,)))
        time.sleep(0.02)
        ftdi.purge_buffers()
        return self

    def release(self) -> None:
        """Turn the outputs off before closing, so the cable drives no pin."""
        try:
            self.ops.clear()
            self._write(bytes((0,)))
        except Exception:                                   # noqa: BLE001
            pass
        self.ftdi.close()

    # -- queueing --------------------------------------------------------------
    def _bits(self, tms: List[int], tdi: List[int], read: bool) -> None:
        out = bytearray()
        for m, d in zip(tms, tdi):
            base = BL_IDLE | (BL_TMS if m else 0) | (BL_TDI if d else 0)
            out += bytes((base, base | BL_TCK | (BL_READ if read else 0)))
            self.tms_level = BL_TMS if m else 0
        out.append(BL_IDLE | self.tms_level)              # TCK back low
        n = len(tms)
        decode = None
        if read:
            def decode(raw, n=n):
                return bytes((sum((b & 1) << (8 - n + i) for i, b in enumerate(raw)),))
        self.ops.append((bytes(out), n if read else 0, decode))

    def tms(self, bits: int, count: int, tdi: int = 0, read: bool = False) -> None:
        self._bits([(bits >> i) & 1 for i in range(count)], [tdi & 1] * count, read)

    def clocks(self, count: int) -> None:
        self._bits([self.tms_level >> 1] * count, [0] * count, False)

    def bytes_out(self, data: bytes, read: bool = False, msb_first: bool = False) -> None:
        if msb_first:
            data = data.translate(_REVERSED)
        if self.tms_level:
            raise JtagError("byte shift with TMS high")
        for start in range(0, len(data), BL_SHIFT_MAX):
            chunk = data[start:start + BL_SHIFT_MAX]
            head = BL_SHIFT | (BL_READ if read else 0) | len(chunk)
            self.ops.append((bytes((head,)) + chunk, len(chunk) if read else 0,
                             bytes if read else None))

    def bits_out(self, value: int, count: int, read: bool = False,
                 msb_first: bool = False) -> None:
        order = range(7, 7 - count, -1) if msb_first else range(count)
        self._bits([0] * count, [(value >> i) & 1 for i in order], read)

    # -- transfer --------------------------------------------------------------
    def _write(self, data: bytes) -> None:
        if self.ftdi.write_data(data) != len(data):
            raise JtagError("the USB-Blaster took a short write")

    def _read(self, wanted: int) -> bytes:
        data = bytes(self.ftdi.read_data_bytes(wanted, attempt=200))
        if len(data) != wanted:
            raise JtagError(f"the USB-Blaster returned {len(data)} of {wanted} bytes")
        return data

    def flush(self) -> bytes:
        """Send the queue in packets of at most 64 bytes, reading after each."""
        result = bytearray()
        packet, reads, decoders = bytearray(), 0, []
        pieces, self.ops = self.ops, []

        def send():
            nonlocal packet, reads, decoders
            if packet:
                self._write(bytes(packet))
                raw = self._read(reads) if reads else b""
                at = 0
                for n, decode in decoders:
                    result.extend(decode(raw[at:at + n]))
                    at += n
            packet, reads, decoders = bytearray(), 0, []

        for data, nread, decode in pieces:
            if data[0] & BL_SHIFT or len(data) <= BL_PACKET:
                if len(packet) + len(data) > BL_PACKET:
                    send()
                packet += data
                reads += nread
                if decode:
                    decoders.append((nread, decode))
            else:
                # A long pin-level run is idle clocking (reads are at most 8
                # bits), so it splits anywhere.
                send()
                for start in range(0, len(data), BL_PACKET):
                    self._write(data[start:start + BL_PACKET])
        send()
        return bytes(result)


def find_blasters() -> List[Tuple[Optional[str], int, int]]:
    """(serial, bus, address) of every attached USB-Blaster."""
    try:
        from pyftdi.usbtools import UsbTools
    except ImportError as exc:
        raise JtagError("pyftdi is missing; start this through "
                        "tooling/u64ii_jtag.sh, which provides it") from exc
    found = UsbTools.find_all([(0x09FB, 0x6001)])
    return sorted(((desc.sn, desc.bus, desc.address) for desc, _ in found),
                  key=lambda d: (d[1], d[2]))


def blaster_url(name: str) -> str:
    """The pyftdi URL for 'blaster' or 'blaster:<serial>'.

    A plain 'blaster' must be the only one attached: with a second one, say the
    one on an Ultimate 64, it could open the wrong machine's cable. The part
    after the colon is a serial number, or pyftdi's bus:address (hex) for
    clones whose serial numbers clash.
    """
    blasters = find_blasters()
    places = [f"{bus:x}:{address:x}" for _, bus, address in blasters]
    listing = ", ".join(f"blaster:{sn}" if sn else f"blaster:{place}"
                        for (sn, _, _), place in zip(blasters, places))
    _, _, which = name.partition(":")
    if which:
        serials = [sn for sn, _, _ in blasters]
        if which not in serials and which not in places:
            raise JtagError(f"no USB-Blaster '{which}' is attached"
                            + (f"; attached: {listing}" if blasters else ""))
        if serials.count(which) > 1:
            raise JtagError(f"several USB-Blasters report the serial number {which}; "
                            "name one by bus and address: "
                            + ", ".join(f"blaster:{p}" for p in places))
        return f"ftdi://altera:usbblaster:{which}/1"
    if not blasters:
        raise JtagError("no USB-Blaster is attached")
    if len(blasters) > 1:
        raise JtagError(f"{len(blasters)} USB-Blasters are attached; name the one on "
                        f"this machine with --url or U64II_JTAG_URL: {listing}")
    return f"ftdi://altera:usbblaster:{blasters[0][0] or places[0]}/1"


def open_cable(url: str, frequency: float):
    """The cable the URL names: a USB-Blaster for 'blaster', 'blaster:<serial>'
    or an altera URL, else an FT232H."""
    if url.split(":", 1)[0] in ("blaster", "usbblaster"):
        url = blaster_url(url)
    if url.startswith("ftdi://altera:") or url.startswith("ftdi://0x9fb:"):
        return Blaster.open(url, frequency)
    return Mpsse.open(url, frequency)


# ---------------------------------------------------------------------------
# TAP
# ---------------------------------------------------------------------------
class Tap:
    """One 7-series TAP. Every method starts and ends in Run-Test/Idle."""

    def __init__(self, mpsse: Mpsse):
        self.m = mpsse
        self.instruction: Optional[int] = None

    def reset(self) -> None:
        self.m.tms(0b011111, 6)          # five ones to Test-Logic-Reset, then Idle
        self.m.flush()
        self.instruction = IR_IDCODE

    def _shift(self, tdi: int, nbits: int, read: bool) -> int:
        """Shift `nbits` LSB first, leave through Exit1 and Update to Idle."""
        m = self.m
        head = nbits - 1
        whole, rest = divmod(head, 8)
        if whole:
            m.bytes_out((tdi & ((1 << (8 * whole)) - 1)).to_bytes(whole, "little"), read)
        if rest:
            m.bits_out(tdi >> (8 * whole), rest, read)
        m.tms(0b1, 1, tdi=tdi >> head, read=read)      # last bit, to Exit1
        m.tms(0b01, 2)                                  # Update, Idle
        raw = m.flush()
        if not read:
            return 0
        value = int.from_bytes(raw[:whole], "little")
        index = whole
        if rest:
            value |= (raw[index] >> (8 - rest)) << (8 * whole)
            index += 1
        return value | (((raw[index] >> 7) & 1) << head)

    def ir(self, instruction: int, read: bool = False) -> int:
        self.m.tms(0b0011, 4)            # Select-DR, Select-IR, Capture-IR, Shift-IR
        captured = self._shift(instruction, IR_LENGTH, read)
        self.instruction = instruction
        return captured

    def dr(self, tdi: int, nbits: int, read: bool = False) -> int:
        self.m.tms(0b001, 3)             # Select-DR, Capture-DR, Shift-DR
        return self._shift(tdi, nbits, read)

    def dr_bitstream(self, data: bytes) -> None:
        """Shift a configuration bitstream, each byte most significant bit first."""
        m = self.m
        m.tms(0b001, 3)
        m.bytes_out(data[:-1], msb_first=True)
        m.bits_out(data[-1], 7, msb_first=True)
        m.tms(0b011, 3, tdi=data[-1] & 1)               # last bit, Update, Idle
        m.flush()

    def idle(self, clocks: int) -> None:
        self.m.clocks(clocks)
        self.m.flush()

    def idcode(self) -> int:
        self.reset()                     # Test-Logic-Reset selects IDCODE
        return self.dr(0xFFFFFFFF, 32, read=True)

    def ir_capture(self) -> int:
        """The IR capture value, read while loading BYPASS."""
        return self.ir(IR_BYPASS, read=True)

    def bypass_delay(self) -> int:
        """TDI-to-TDO delay in bits with every device in BYPASS; -1 if none fits.

        A single device gives 1. Zero means TDO follows TDI, which is what the
        Adafruit board does when its I2C mode switch joins D1 and D2.
        """
        self.ir(IR_BYPASS)
        pattern = 0xA5C3 << 8
        seen = self.dr(pattern, 32, read=True)
        for delay in range(8):
            if (seen >> delay) & 0xFFFF00 == pattern & 0xFFFF00:
                return delay
        return -1


# ---------------------------------------------------------------------------
# User chain
# ---------------------------------------------------------------------------
class UserChain:
    """The Ultimate's user JTAG chain behind USER4.

    The first bit of every USER4 data scan chooses between the chain's 4-bit
    register select (1) and the selected register (0). The bit that selects
    the register is not shifted into it, so a register read returns its bit 0
    twice: once on that select bit and once on the first data bit.
    """

    def __init__(self, tap: Tap):
        self.tap = tap
        self.register: Optional[int] = None

    def select(self, register: int) -> None:
        if self.tap.instruction != IR_USER4:
            self.tap.ir(IR_USER4)
        self.tap.dr(1 | (register << 1), 5)
        self.register = register

    def scan(self, register: int, tdi: int, nbits: int, read: bool = False) -> int:
        if self.register != register or self.tap.instruction != IR_USER4:
            self.select(register)
        return self.tap.dr(tdi << 1, nbits + 1, read) >> 1

    def user_id(self) -> int:
        return self.scan(USER_ID, 0, 32, read=True)

    def debug_word(self) -> int:
        return self.scan(USER_DEBUG, 0, 32, read=True)

    def set_outputs(self, value: int) -> None:
        self.scan(USER_OUTPUTS, value, 8)

    # -- memory commands, 12 bits in a 16-bit word --------------------------
    def _commands(self, words: List[int]) -> None:
        tdi = 0
        for i, word in enumerate(words):
            tdi |= (word & 0xFFFF) << (16 * i)
        self.scan(USER_MEM_COMMAND, tdi, 16 * len(words))

    @staticmethod
    def _address(address: int) -> List[int]:
        return [((0x4 | i) << 8) | ((address >> (8 * i)) & 0xFF) for i in range(4)]

    def write(self, address: int, data: bytes) -> None:
        if address & 3 or len(data) & 3 or not data:
            raise JtagError("memory writes must be whole, aligned words")
        self._commands(self._address(address) + [0x0180])   # write, incrementing
        self.scan(USER_MEM_DATA, int.from_bytes(data, "little"), 8 * len(data))

    def drain(self, register: int, pops: int) -> Tuple[int, bytes]:
        """One scan of a FIFO register: its fill level, then `pops` bytes.

        After every eighth data bit the chain loads the FIFO's next byte and
        pops it when TDI is 0, so TDI is 0 for the bytes this scan reads and 1
        on its last bit. The level is captured before anything is popped;
        popping more than it reported returns stale data.
        """
        nbits = 8 * (pops + 1)
        value = self.scan(register, 1 << (nbits - 1), nbits, read=True)
        level = value & 0xFF
        return level, (value >> 8).to_bytes(pops, "little") if pops else b""

    def read(self, address: int, length: int) -> bytes:
        if address & 3 or length & 3:
            raise JtagError("memory reads must be whole, aligned words")
        out = bytearray()
        while len(out) < length:
            words = min((length - len(out)) // 4, READ_COMMAND_WORDS)
            self._commands(self._address(address + len(out)) + [0x0300 | (words - 1)])
            wanted, known, stalls = words * 4, 0, 0
            block = bytearray()
            while len(block) < wanted:
                pops = min(known, wanted - len(block))
                level, data = self.drain(USER_READ_FIFO, pops)
                block += data
                known = max(0, level - pops)
                if pops == 0 and level == 0:
                    stalls += 1
                    if stalls > READ_STALL_SCANS:
                        raise JtagError(f"memory read at 0x{address + len(out):08X} "
                                        "stalled: the read FIFO stays empty")
                else:
                    stalls = 0
            out += block
        return bytes(out)


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------
class Board:
    def __init__(self, url: str = DEFAULT_URL, frequency: float = DEFAULT_FREQUENCY,
                 mpsse: Optional[Mpsse] = None):
        self.mpsse = mpsse or open_cable(url, frequency)
        self.tap = Tap(self.mpsse)
        self.chain = UserChain(self.tap)
        self.part: Optional[str] = None
        self.bitstream: Optional[str] = None
        self.idcode_value = 0
        self.tap.reset()

    def close(self) -> None:
        self.mpsse.release()

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- checks ----------------------------------------------------------------
    def identify(self) -> int:
        """Refuse anything but a powered, correctly wired, supported Artix-7.

        Only the IDCODE read, which every TAP selects on reset, happens before
        the part is known. An IR scan sized for the Artix would load a random
        instruction into another device, and on an ECP5 that can clear its
        configuration.
        """
        idcode = self.tap.idcode()
        if idcode in (0x00000000, 0xFFFFFFFF):
            raise JtagError(
                f"IDCODE reads 0x{idcode:08X}: no device answers. Is the machine "
                "switched on, is GND connected, is TDO on D2, and is the Adafruit "
                "I2C mode switch off?")
        masked = idcode & 0x0FFFFFFF
        if masked not in SUPPORTED:
            name = KNOWN_OTHER.get(masked) or KNOWN_OTHER.get(idcode)
            if name is None:
                raise JtagError(f"IDCODE 0x{idcode:08X} is not a device this tool knows")
            raise JtagError(f"IDCODE 0x{idcode:08X} is {name}; no Ultimate 64 Elite II "
                            "or C64 Ultimate uses it")
        self.part, self.bitstream = SUPPORTED[masked]
        self.idcode_value = idcode
        delay = self.tap.bypass_delay()
        if delay != 1:
            raise JtagError(f"the chain has a bypass delay of {delay}, expected one "
                            "device; check the wiring"
                            + (", or lower --frequency" if isinstance(self.mpsse, Mpsse)
                               else ""))
        return idcode

    def design_loaded(self) -> bool:
        return self.chain.user_id() == USER_ID_VALUE

    def require_design(self) -> None:
        if not self.design_loaded():
            raise JtagError("the FPGA does not run an Ultimate design (user ID is not "
                            "0xDEAD1541); load one with --fpga")

    # -- FPGA ------------------------------------------------------------------
    def configure(self, path: str) -> None:
        with open(path, "rb") as handle:
            data = handle.read()
        if b"\xaa\x99\x55\x66" not in data[:4096]:
            raise JtagError(f"{path} has no 7-series sync word; not a bitstream")
        # JPROGRAM erases the running design, so a bitstream for another part
        # is refused while the board still runs.
        built_for = bitstream_idcode(data)
        if built_for is not None and built_for & 0x0FFFFFFF != self.idcode_value & 0x0FFFFFFF:
            raise JtagError(f"{path} is built for IDCODE 0x{built_for:08X}, not for this "
                            f"{self.part} (0x{self.idcode_value:08X}); the FPGA was not touched")
        log(f"configuring the {self.part} from {path} ({len(data)} bytes, volatile)")
        started = time.monotonic()
        self.tap.reset()
        self.tap.ir(IR_JPROGRAM)
        try:
            self.tap.idle(10000)
            deadline = time.monotonic() + 1.0
            while not self.tap.ir_capture() & IR_CAPTURE_INIT:
                if time.monotonic() > deadline:
                    raise JtagError("the FPGA did not clear its configuration after JPROGRAM")
                time.sleep(0.01)
            self.tap.ir(IR_CFG_IN)
            self.tap.dr_bitstream(data)
            self.tap.ir(IR_JSTART)
            self.tap.idle(2000)
            status = self.tap.ir_capture()
            self.tap.reset()
            self.chain.register = None
            time.sleep(0.5)
            if not self.design_loaded():
                raise JtagError("configuration did not bring up an Ultimate design "
                                f"(IR capture 0x{status:02X})")
        except BaseException:
            log("the FPGA is now unconfigured: the machine stays dark until 'fpga' "
                "succeeds or it is power-cycled")
            raise
        log(f"configured in {time.monotonic() - started:.1f}s")

    # -- application -----------------------------------------------------------
    def write_verified(self, address: int, data: bytes, verify: bool) -> None:
        for attempt in (1, 2):
            self.chain.write(address, data)
            if not verify:
                return
            back = self.chain.read(address, len(data))
            if back == data:
                return
            first = next(i for i in range(len(data)) if back[i] != data[i])
            log(f"verify failed at 0x{address + first:08X} (attempt {attempt}): "
                f"wrote {data[first]:02X}, read {back[first]:02X}")
        raise JtagError(f"memory at 0x{address:08X} does not hold what was written")

    @staticmethod
    def cache_flush_trampoline(target: int) -> bytes:
        """Code that evicts every instruction cache entry, then jumps to `target`.

        A cache-sized run of NOPs fills each entry with the trampoline's own
        addresses, which the application never executes, so every fetch the
        application makes afterwards misses and reads RAM.
        """
        upper = (target + 0x800) >> 12
        lower = target - (upper << 12)
        lui_t0 = (upper << 12) | (5 << 7) | 0x37              # lui  t0, upper
        jalr = ((lower & 0xFFF) << 20) | (5 << 15) | 0x67     # jalr x0, lower(t0)
        words = [RISCV_NOP] * (ICACHE_BYTES // 4) + [lui_t0, jalr]
        return struct.pack(f"<{len(words)}L", *words)

    def request_boot(self, target: int) -> None:
        """Point the boot request at the cache-flush trampoline, which jumps to `target`."""
        self.write_verified(TRAMPOLINE_ADDRESS, self.cache_flush_trampoline(target), True)
        self.write_verified(BOOT_MAGIC_ADDRESS,
                            struct.pack("<LL", TRAMPOLINE_ADDRESS, BOOT_MAGIC_VALUE), True)

    def release_after_failure(self, flush: bool) -> None:
        """Release a CPU held in reset after a failed load, and say what is left.

        With `flush`, the boot request goes through the cache flush into the
        bootloader, as in reset_cpu. Otherwise, or if that write fails too, the
        request is cleared, and the bootloader boots the flashed application
        without the flush.
        """
        flushed = False
        if flush:
            try:
                self.request_boot(BOOTLOADER_ADDRESS)
                flushed = True
            except BaseException as exc:                    # noqa: BLE001
                log(f"could not set up the cache flush: {exc}")
        if not flushed:
            try:
                self.chain.write(BOOT_MAGIC_ADDRESS, bytes(8))
            except BaseException as exc:                    # noqa: BLE001
                log(f"could not clear the boot request: {exc}")
        try:
            self.chain.set_outputs(0)
            log("CPU released; the bootloader starts the flashed application")
        except BaseException as exc:                        # noqa: BLE001
            log(f"could not release the CPU ({exc}): it stays in reset until "
                "'reset' succeeds or the machine is power-cycled")

    def run_application(self, image: bytes, verify: bool = True) -> None:
        """Load `image` at 0x30000 and let the bootloader start it.

        The boot request points at a cache-flush trampoline, which then jumps
        to 0x30000 (see ICACHE_BYTES).
        """
        image += b"\x00" * (-len(image) % 4)
        log(f"holding the CPU in reset; loading {len(image)} bytes at 0x{APP_ADDRESS:X}"
            f"{' with read-back' if verify else ''}")
        started = time.monotonic()
        self.chain.set_outputs(OUTPUT_CPU_RESET)
        try:
            for offset in range(0, len(image), WRITE_CHUNK):
                self.write_verified(APP_ADDRESS + offset,
                                    image[offset:offset + WRITE_CHUNK], verify)
            self.request_boot(APP_ADDRESS)
        except BaseException:
            log("load failed")
            self.release_after_failure(flush=True)
            raise
        self.chain.set_outputs(0)
        log(f"CPU released after {time.monotonic() - started:.1f}s; the bootloader "
            "starts the loaded application")

    def reset_cpu(self) -> None:
        """Restart the CPU so that the bootloader loads the flashed application.

        The bootloader copies that application to the addresses the running
        one used, so the instruction cache is flushed first: the boot request
        runs the trampoline, which jumps back into the bootloader, and the
        bootloader has cleared the request by then and boots from flash.
        """
        self.chain.set_outputs(OUTPUT_CPU_RESET)
        try:
            self.request_boot(BOOTLOADER_ADDRESS)
        except BaseException:
            self.release_after_failure(flush=False)
            raise
        self.chain.set_outputs(0)

    def console(self, seconds: float, sink=sys.stdout) -> int:
        """Copy the CPU's UART output to `sink` for `seconds` (0 = until Ctrl-C)."""
        deadline = None if seconds <= 0 else time.monotonic() + seconds
        known, total = 0, 0
        while deadline is None or time.monotonic() < deadline:
            level, data = self.chain.drain(USER_CONSOLE, min(known, 255))
            known = max(0, level - len(data))
            if data:
                sink.write(data.decode("latin-1"))
                sink.flush()
                total += len(data)
            elif not level:
                time.sleep(0.05)
        return total


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def repo_path(*parts: str) -> str:
    return os.path.join(os.environ.get("ULTIMATE_REPO_DIR", ROOT), *parts)


def bitstream_for(board: Board, choice: str) -> str:
    if choice != "auto":
        return choice
    path = repo_path("external", board.bitstream)
    if not os.path.isfile(path):
        raise JtagError(f"no bitstream for the {board.part} at {path}")
    return path


def hexdump(address: int, data: bytes) -> None:
    for offset in range(0, len(data), 16):
        row = data[offset:offset + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"{address + offset:08X}  {row.hex(' '):<47}  {text}")


def cmd_probe(board: Board, args) -> int:
    idcode = board.idcode_value
    log(f"IDCODE 0x{idcode:08X}: {board.part}, revision {idcode >> 28}")
    capture = board.tap.ir_capture()
    log(f"IR capture 0x{capture:02X}: configuration "
        f"{'done' if capture & IR_CAPTURE_DONE else 'NOT done'}")
    user_id = board.chain.user_id()
    if user_id == USER_ID_VALUE:
        log("user chain answers 0xDEAD1541: an Ultimate design is running")
        log(f"user chain debug word 0x{board.chain.debug_word():08X}")
    else:
        log(f"user chain answers 0x{user_id:08X}: no Ultimate design is running")
    log(f"bitstream for this part: external/{board.bitstream}")
    return 0


def cmd_fpga(board: Board, args) -> int:
    board.configure(bitstream_for(board, args.bit))
    log("the bootloader now starts the flashed application")
    return 0


def cmd_run(board: Board, args) -> int:
    with open(args.bin, "rb") as handle:
        image = handle.read()
    if image[:4] == b"\x7fELF":
        raise JtagError(f"{args.bin} is an ELF; pass the raw ultimate.bin")
    if args.warm:
        # Swapping only the application leaves every block the old one set up
        # running; only the CPU restarts.
        log("--warm: keeping the running FPGA image; only the CPU restarts")
        board.require_design()
    else:
        board.configure(bitstream_for(board, args.fpga))
        time.sleep(1.0)                  # the flashed application has started by now
    board.run_application(image, verify=not args.no_verify)
    if args.console:
        board.console(args.console)
    return 0


def cmd_recover(board: Board, args) -> int:
    kit = repo_path("recovery", "u64ii")
    if board.part != "XC7A50T":
        raise JtagError("the recovery kit carries an XC7A50T bitstream only; use "
                        "run --fpga auto with a built ultimate.bin instead")
    board.configure(os.path.join(kit, "u64_mk2_artix.bit"))
    time.sleep(1.0)
    with open(os.path.join(kit, "ultimate.bin"), "rb") as handle:
        board.run_application(handle.read(), verify=True)
    log("the recovery application (3.14c) runs from RAM; install update.ue2 from its "
        "menu (the USB stick shows as /USB2); on a C64 Ultimate it shows a 'no system "
        "ROMs' welcome screen instead of BASIC, which is expected")
    log("to leave without flashing, use 'fpga', not 'reset': the kit's FPGA image is "
        "older, and the flashed application crashes on it")
    return 0


def cmd_reset(board: Board, args) -> int:
    board.require_design()
    board.reset_cpu()
    log("CPU restarted through the cache flush; the bootloader boots the flashed application")
    return 0


def cmd_console(board: Board, args) -> int:
    board.require_design()
    try:
        board.console(args.secs)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_dump(board: Board, args) -> int:
    board.require_design()
    address, length = int(args.address, 0), int(args.length, 0)
    if address < 0 or length <= 0:
        raise JtagError("dump needs a non-negative address and a positive length")
    start, end = address & ~3, (address + length + 3) & ~3
    data = board.chain.read(start, end - start)[address - start:address - start + length]
    if args.output:
        with open(args.output, "wb") as handle:
            handle.write(data)
        log(f"wrote {len(data)} bytes to {args.output}")
    else:
        hexdump(address, data)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="JTAG for the Ultimate 64 Elite II and C64 Ultimate (Artix-7) "
                    "through an FT232H or a USB-Blaster. Volatile only: nothing writes the flash.")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"pyftdi URL of the FT232H (default {DEFAULT_URL}, "
                             "or $U64II_JTAG_URL); 'blaster' for a USB-Blaster")
    parser.add_argument("--frequency", type=float, default=DEFAULT_FREQUENCY,
                        help="TCK in Hz (default 3e6); lower it for long wires. "
                             "FT232H only")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="identify the FPGA and the running design; changes nothing")

    p = sub.add_parser("fpga", help="configure the FPGA (volatile); the flashed app boots")
    p.add_argument("--bit", default="auto",
                   help="bitstream; 'auto' picks external/u64e2_{50t,100t}.bit by IDCODE")

    p = sub.add_parser("run", help="load an application into RAM and start it (volatile)")
    p.add_argument("--bin", default=repo_path("target", "u64ii", "riscv", "ultimate",
                                              "result", "ultimate.bin"),
                   help="raw application image (default: the built ultimate.bin)")
    p.add_argument("--fpga", default="auto",
                   help="bitstream to configure first (default auto: external/u64e2_*.bit "
                        "by IDCODE)")
    p.add_argument("--warm", action="store_true",
                   help="keep the running FPGA image and swap only the application")
    p.add_argument("--no-verify", action="store_true",
                   help="skip reading the image back before starting it")
    p.add_argument("--console", type=float, default=0,
                   help="then show the console for this many seconds")

    sub.add_parser("recover", help="recover.py equivalent: recovery kit bitstream and application")
    sub.add_parser("reset", help="restart the CPU so it boots the flashed application")

    p = sub.add_parser("console", help="show the CPU's UART output")
    p.add_argument("--secs", type=float, default=0, help="seconds to capture (0 = until Ctrl-C)")

    p = sub.add_parser("dump", help="read memory (hexdump, or raw with -o)")
    p.add_argument("address")
    p.add_argument("length")
    p.add_argument("-o", "--output")
    return parser.parse_args(argv)


COMMANDS = {"probe": cmd_probe, "fpga": cmd_fpga, "run": cmd_run, "recover": cmd_recover,
            "reset": cmd_reset, "console": cmd_console, "dump": cmd_dump}


def main(argv=None, mpsse: Optional[Mpsse] = None) -> int:
    args = parse_args(argv)
    try:
        with DeviceLock(), Board(args.url, args.frequency, mpsse) as board:
            board.idcode_value = board.identify()
            return COMMANDS[args.command](board, args)
    except JtagError as exc:
        log(f"ERROR: {exc}")
        return 1
    except OSError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
