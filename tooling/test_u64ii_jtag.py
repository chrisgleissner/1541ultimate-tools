#!/usr/bin/env python3
"""Host tests for u64ii_jtag.py against a simulated FT232H and FPGA.

The model interprets the MPSSE commands the tool sends, clocks a 7-series TAP
edge by edge, and behind USER4 runs a Python transcription of
fpga/io/jtag/vhdl_source/jtag_client_xilinx.vhd: the select bit, the
register select, the FIFOs with their registered pop and put pulses, and the
memory command decoder. TDO is sampled on the rising edge from the value the
TAP presented after the previous edge.

It proves the tool and the transcription agree with each other. It does not
prove either agrees with the hardware; the first run on a real board does
that, starting with `probe`, whose 0xDEAD1541 check catches an off-by-one in
the scan framing.

    python3 tooling/test_u64ii_jtag.py
"""

import contextlib
import fcntl
import io
import os
import runpy
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["U64II_JTAG_LOCK"] = "off"     # never touch the real device lock
import u64ii_jtag as jt  # noqa: E402

# TAP states
TLR, RTI, SEL_DR, CAP_DR, SH_DR, EX1_DR, PA_DR, EX2_DR, UPD_DR, \
    SEL_IR, CAP_IR, SH_IR, EX1_IR, PA_IR, EX2_IR, UPD_IR = range(16)
NEXT = {
    TLR: (RTI, TLR), RTI: (RTI, SEL_DR), SEL_DR: (CAP_DR, SEL_IR),
    CAP_DR: (SH_DR, EX1_DR), SH_DR: (SH_DR, EX1_DR), EX1_DR: (PA_DR, UPD_DR),
    PA_DR: (PA_DR, EX2_DR), EX2_DR: (SH_DR, UPD_DR), UPD_DR: (RTI, SEL_DR),
    SEL_IR: (CAP_IR, TLR), CAP_IR: (SH_IR, EX1_IR), SH_IR: (SH_IR, EX1_IR),
    EX1_IR: (PA_IR, UPD_IR), PA_IR: (PA_IR, EX2_IR), EX2_IR: (SH_IR, UPD_IR),
    UPD_IR: (RTI, SEL_DR),
}


class UserChainModel:
    """jtag_client_xilinx, one rising edge of jtck at a time."""

    def __init__(self, memory):
        self.memory = memory
        self.ir_in = 0
        self.ir_shift = 0
        self.expect_sel = self.isel = self.dsel = 0
        self.bit_count = self.wbit_count = 0
        self.shiftreg_fifo = 0
        self.shiftreg_write = 0
        self.shiftreg_console = 0
        self.shiftreg_debug = 0
        self.write_vector = 0
        self.read_fifo, self.console_fifo, self.write_fifo = [], [], []
        self.read_fifo_get = self.console_fifo_get = self.write_fifo_put = 0
        # avm side
        self.address = 0
        self.write_enabled = self.incrementing = 0
        self.byte_count = 0
        self.write_data = [0, 0, 0, 0]
        self.pending_reads = []
        self.lost_writes = 0
        self.stuck = {}                  # address -> word the RAM keeps, whatever is written

    def tdo(self):
        if self.isel:
            return self.ir_shift & 1
        if self.ir_in == 0:
            return (jt.USER_ID_VALUE >> (self.bit_count & 31)) & 1
        if self.ir_in == 2:
            return self.shiftreg_write & 1
        if self.ir_in == 3:
            return self.shiftreg_debug & 1
        if self.ir_in == 4:
            return self.shiftreg_fifo & 1
        if self.ir_in == 0xA:
            return self.shiftreg_console & 1
        return 0

    def edge(self, sel, capture, shift, update, tdi):
        # Registered pulses from the previous edge act now.
        if self.read_fifo_get and self.read_fifo:
            self.read_fifo.pop(0)
        if self.console_fifo_get and self.console_fifo:
            self.console_fifo.pop(0)
        if self.write_fifo_put:
            word = (0x800 | (self.shiftreg_fifo >> 8) & 0xFF) if self.ir_in == 6 \
                else self.shiftreg_fifo & 0xFFF
            if len(self.write_fifo) < 15:
                self.write_fifo.append(word)
            else:
                self.lost_writes += 1
        self.read_fifo_get = self.console_fifo_get = self.write_fifo_put = 0

        old = dict(isel=self.isel, dsel=self.dsel, expect_sel=self.expect_sel,
                   ir_in=self.ir_in)
        if sel:
            # process 1
            if capture:
                self.ir_shift = self.ir_in
                self.expect_sel = 1
            elif shift:
                self.expect_sel = 0
                if old["expect_sel"]:
                    self.isel, self.dsel = tdi, 1 - tdi
                elif old["isel"]:
                    self.ir_shift = (tdi << 3) | (self.ir_shift >> 1)
            elif update:
                if old["isel"]:
                    self.ir_in = self.ir_shift
                self.isel = self.dsel = 0

        ir = old["ir_in"]
        if shift and old["dsel"]:
            self.shiftreg_write = (tdi << 7) | (self.shiftreg_write >> 1)
            wbc = self.wbit_count
            self.wbit_count = (wbc + 1) & 15
            if ir == 5:
                self.shiftreg_fifo = (tdi << 15) | (self.shiftreg_fifo >> 1)
                self.write_fifo_put = int(wbc == 15)
            elif ir == 6:
                self.shiftreg_fifo = (tdi << 15) | (self.shiftreg_fifo >> 1)
                self.write_fifo_put = int((wbc & 7) == 7)
            bc = self.bit_count
            self.bit_count = (bc + 1) & 31
            if ir == 4:
                if bc & 7 == 7:
                    self.shiftreg_fifo = self.read_fifo[0] if self.read_fifo else 0x5A
                    self.read_fifo_get = 1 - tdi
                else:
                    self.shiftreg_fifo >>= 1
            elif ir == 0xA:
                if bc & 7 == 7:
                    self.shiftreg_console = self.console_fifo[0] if self.console_fifo else 0x5A
                    self.console_fifo_get = 1 - tdi
                else:
                    self.shiftreg_console >>= 1
            self.shiftreg_debug >>= 1
        if sel and capture:
            self.shiftreg_write = self.write_vector
            self.bit_count = self.wbit_count = 0
            self.shiftreg_fifo = len(self.read_fifo)
            self.shiftreg_console = min(len(self.console_fifo), 255)
            self.shiftreg_debug = 0x12345678
        if update and old["dsel"] and ir == 2:
            self.write_vector = self.shiftreg_write
        self.avm()

    def avm(self):
        """The memory side runs much faster than TCK, so it drains at once."""
        while self.pending_reads and len(self.read_fifo) < 128:
            self.read_fifo.append(self.pending_reads.pop(0))
        while self.write_fifo:
            word = self.write_fifo.pop(0)
            cmd, byte = word >> 8, word & 0xFF
            if cmd in (0x0, 0x8):
                if self.write_enabled:
                    self.write_data[self.byte_count] = byte
                    self.byte_count += 1
                    if self.byte_count == 4:
                        self.memory[self.address] = self.stuck.get(self.address,
                                                                   bytes(self.write_data))
                        self.byte_count = 0
                        if self.incrementing:
                            self.address += 4
            elif cmd == 0x1:
                self.byte_count, self.write_enabled = 0, 1
                self.incrementing = byte >> 7
            elif cmd in (0x2, 0x3):
                self.write_enabled = 0
                for i in range(byte + 1):
                    addr = self.address + (4 * i if cmd == 3 else 0)
                    self.pending_reads.extend(self.memory.get(addr, b"\xee" * 4))
                if cmd == 3:
                    self.address += 4 * (byte + 1)
                while self.pending_reads and len(self.read_fifo) < 128:
                    self.read_fifo.append(self.pending_reads.pop(0))
            elif 0x4 <= cmd <= 0x7:
                self.write_enabled = 0
                shift = 8 * (cmd - 4)
                self.address = (self.address & ~(0xFF << shift)) | (byte << shift)


class ArtixModel:
    IDCODE = 0x1362C093          # revision 1 XC7A50T

    def __init__(self):
        self.state = TLR
        self.ir = jt.IR_IDCODE
        self.ir_shift = 0
        self.dr = 0
        self.dr_len = 32
        self.memory = {}
        self.chain = UserChainModel(self.memory)
        self.presented = 1
        self.config_bits = []
        self.configured = True
        self.jprogram = False
        self.accept_config = True        # False: JSTART leaves the FPGA blank
        self.ir_shifts = 0               # IR scans seen, to prove none happened

    def presented_tdo(self):
        if self.state == SH_IR:
            return self.ir_shift & 1
        if self.state == SH_DR:
            if self.ir == jt.IR_USER4 and self.configured:
                return self.chain.tdo()
            return self.dr & 1
        return 1

    def rising(self, tms, tdi):
        sampled = self.presented
        state = self.state
        if state == CAP_IR:
            capture = 0x01 | (jt.IR_CAPTURE_INIT) | (jt.IR_CAPTURE_DONE if self.configured else 0)
            self.ir_shift = capture
        elif state == SH_IR:
            self.ir_shifts += 1
            self.ir_shift = (tdi << (jt.IR_LENGTH - 1)) | (self.ir_shift >> 1)
        elif state == UPD_IR:
            self.ir = self.ir_shift & 0x3F
            if self.ir == jt.IR_JPROGRAM:
                self.configured, self.jprogram, self.config_bits = False, True, []
            if self.ir == jt.IR_JSTART and self.config_bits and self.accept_config:
                self.configured = True
        elif state == CAP_DR:
            if self.ir == jt.IR_IDCODE:
                self.dr, self.dr_len = self.IDCODE, 32
            else:
                self.dr, self.dr_len = 0, 1
        elif state == SH_DR:
            if self.ir == jt.IR_CFG_IN:
                self.config_bits.append(tdi)
            self.dr = (tdi << (self.dr_len - 1)) | (self.dr >> 1)
        user = self.ir == jt.IR_USER4 and self.configured
        self.chain.edge(user and state not in (TLR,), user and state == CAP_DR,
                        user and state == SH_DR, user and state == UPD_DR, tdi)
        self.state = NEXT[state][tms]
        if self.state == TLR:
            self.ir = jt.IR_IDCODE
        self.presented = self.presented_tdo()
        return sampled


class FakeFtdi:
    """Executes MPSSE command streams against an ArtixModel."""

    def __init__(self, model):
        self.model = model
        self.tms = 1
        self.tdi = 0
        self.out = bytearray()
        self.closed = False
        self.pins = None
        self.fail_in_reset = False       # the USB link drops once the CPU is held in reset

    def clock(self, tdi=None, tms=None):
        if tdi is not None:
            self.tdi = tdi
        if tms is not None:
            self.tms = tms
        return self.model.rising(self.tms, self.tdi)

    def write_data(self, data):
        if self.fail_in_reset and self.model.chain.write_vector & jt.OUTPUT_CPU_RESET:
            raise OSError("USB device disconnected")
        data, i = bytes(data), 0
        while i < len(data):
            op = data[i]
            if op in (0x19, 0x39, 0x11):
                n = data[i + 1] + (data[i + 2] << 8) + 1
                payload = data[i + 3:i + 3 + n]
                for byte in payload:
                    got = 0
                    for b in range(8):
                        bit = (byte >> (7 - b)) & 1 if op == 0x11 else (byte >> b) & 1
                        got |= self.clock(tdi=bit) << b
                    if op == 0x39:
                        self.out.append(got)
                i += 3 + n
            elif op in (0x1B, 0x3B, 0x13):
                n, byte = data[i + 1] + 1, data[i + 2]
                got = 0
                for b in range(n):
                    bit = (byte >> (7 - b)) & 1 if op == 0x13 else (byte >> b) & 1
                    got = (got >> 1) | (self.clock(tdi=bit) << 7)
                if op == 0x3B:
                    self.out.append(got)
                i += 3
            elif op in (0x4B, 0x6B):
                n, byte = data[i + 1] + 1, data[i + 2]
                got = 0
                for b in range(n):
                    got = (got >> 1) | (self.clock(tdi=byte >> 7, tms=(byte >> b) & 1) << 7)
                if op == 0x6B:
                    self.out.append(got)
                i += 3
            elif op == 0x8E:
                for _ in range(data[i + 1] + 1):
                    self.clock()
                i += 2
            elif op == 0x8F:
                for _ in range(8 * (data[i + 1] + (data[i + 2] << 8) + 1)):
                    self.clock()
                i += 3
            elif op == 0x80:
                self.pins = (data[i + 1], data[i + 2])
                i += 3
            elif op in (0x85, 0x87):
                i += 1
            else:
                raise AssertionError(f"unexpected MPSSE opcode 0x{op:02X}")

    def read_data_bytes(self, size, attempt=1):
        data, self.out = self.out[:size], self.out[size:]
        return data

    def close(self, freeze=False):
        self.closed = True
        self.frozen = freeze


class FakeBlaster:
    """Executes USB-Blaster byte streams against an ArtixModel.

    Pin-level bytes set TMS and TDI and clock the TAP on a rising TCK, sampling
    TDO on that edge when READ is set. A shift-mode header clocks its data
    bytes LSB first with TMS low. Like the CPLD, it keeps the pin levels a
    shift leaves behind.
    """

    def __init__(self, model):
        self.model = model
        self.tck = 0
        self.tms = 1
        self.out = bytearray()
        self.closed = False
        self.pins = None
        self.fail_in_reset = False
        self.packets = []

    def write_data(self, data):
        if self.fail_in_reset and self.model.chain.write_vector & jt.OUTPUT_CPU_RESET:
            raise OSError("USB device disconnected")
        data, i = bytes(data), 0
        self.packets.append(len(data))
        while i < len(data):
            op = data[i]
            if op & jt.BL_SHIFT:
                n = op & 0x3F
                for byte in data[i + 1:i + 1 + n]:
                    got = 0
                    for b in range(8):
                        got |= self.model.rising(0, (byte >> b) & 1) << b
                    if op & jt.BL_READ:
                        self.out.append(got)
                self.tms = 0
                i += 1 + n
            else:
                tms, tdi = (op >> 1) & 1, (op >> 4) & 1
                if (op & jt.BL_TCK) and not self.tck:
                    sampled = self.model.rising(tms, tdi)
                    if op & jt.BL_READ:
                        self.out.append(sampled)
                elif op & jt.BL_READ:
                    self.out.append(self.model.presented)
                self.tck, self.tms = op & jt.BL_TCK, tms
                self.pins = op
                i += 1
        return len(data)

    def read_data_bytes(self, size, attempt=1):
        data, self.out = self.out[:size], self.out[size:]
        return data

    def close(self, freeze=False):
        self.closed = True


CABLE = "ft232h"


def cable(model):
    """The simulated cable the running test class asks for, and its device."""
    if CABLE == "blaster":
        ftdi = FakeBlaster(model)
        return jt.Blaster(ftdi), ftdi
    ftdi = FakeFtdi(model)
    return jt.Mpsse(ftdi), ftdi


def board(model=None):
    model = model or ArtixModel()
    driver, ftdi = cable(model)
    return jt.Board(mpsse=driver), model, ftdi


class TapTest(unittest.TestCase):
    def test_idcode_and_identify(self):
        b, _, _ = board()
        self.assertEqual(b.identify(), 0x1362C093)
        self.assertEqual((b.part, b.bitstream), ("XC7A50T", "u64e2_50t.bit"))

    def test_bypass_delay_is_one_device(self):
        b, _, _ = board()
        self.assertEqual(b.tap.bypass_delay(), 1)

    def test_lattice_is_refused(self):
        model = ArtixModel()
        model.IDCODE = 0x41111043
        b, _, _ = board(model)
        with self.assertRaisesRegex(jt.JtagError, "LFE5U"):
            b.identify()
        self.assertEqual(model.ir_shifts, 0)     # an Artix IR scan can erase an ECP5

    def test_unknown_part_is_refused_without_an_ir_scan(self):
        model = ArtixModel()
        model.IDCODE = 0x12345679
        b, _, _ = board(model)
        with self.assertRaisesRegex(jt.JtagError, "not a device this tool knows"):
            b.identify()
        self.assertEqual(model.ir_shifts, 0)

    def test_unpowered_is_refused(self):
        model = ArtixModel()
        model.IDCODE = 0xFFFFFFFF
        b, _, _ = board(model)
        with self.assertRaisesRegex(jt.JtagError, "no device answers"):
            b.identify()

    def test_release_leaves_pins_inputs(self):
        b, _, ftdi = board()
        b.close()
        self.assertEqual(ftdi.pins, (0, 0))
        self.assertTrue(ftdi.frozen)


class UserChainTest(unittest.TestCase):
    def test_user_id(self):
        b, _, _ = board()
        self.assertEqual(b.chain.user_id(), jt.USER_ID_VALUE)

    def test_outputs(self):
        b, model, _ = board()
        b.chain.set_outputs(0x80)
        self.assertEqual(model.chain.write_vector, 0x80)
        b.chain.set_outputs(0)
        self.assertEqual(model.chain.write_vector, 0)

    def test_write_then_read(self):
        b, model, _ = board()
        data = bytes((i * 7 + 3) & 0xFF for i in range(4096))
        b.chain.write(0x30000, data)
        self.assertEqual(model.chain.lost_writes, 0)
        self.assertEqual(model.memory[0x30000], data[:4])
        self.assertEqual(model.memory[0x30FFC], data[-4:])
        self.assertEqual(b.chain.read(0x30000, len(data)), data)

    def test_read_spans_commands(self):
        b, model, _ = board()
        for i in range(600):
            model.memory[0x1000 + 4 * i] = struct.pack("<L", i * 0x01010101 & 0xFFFFFFFF)
        got = b.chain.read(0x1000, 2400)
        self.assertEqual(got, b"".join(model.memory[0x1000 + 4 * i] for i in range(600)))

    def test_console(self):
        b, model, _ = board()
        text = b"Hello world, U64-II!\nMagic!\n" * 20
        model.chain.console_fifo.extend(text)
        sink = io.StringIO()
        b.console(0.3, sink)
        self.assertEqual(sink.getvalue().encode("latin-1"), text)


class FlowTest(unittest.TestCase):
    def test_run_application(self):
        b, model, _ = board()
        b.identify()
        image = bytes(range(256)) * 200 + b"\x01\x02"
        b.run_application(image)
        padded = image + b"\x00\x00"
        for off in range(0, len(padded), 4):
            self.assertEqual(model.memory[jt.APP_ADDRESS + off], padded[off:off + 4])
        jump = struct.unpack("<L", model.memory[0xFFF8])[0]
        self.assertEqual(model.memory[0xFFFC], struct.pack("<L", jt.BOOT_MAGIC_VALUE))
        # The boot request points at the cache flush, 2 KB aligned.
        self.assertEqual(jump, jt.TRAMPOLINE_ADDRESS)
        self.assertEqual(jump % jt.ICACHE_BYTES, 0)
        words = [struct.unpack("<L", model.memory[jump + 4 * i])[0] for i in range(514)]
        self.assertEqual(words[:512], [jt.RISCV_NOP] * 512)
        self.assertEqual(words[512:], [0x000302B7, 0x00028067])   # lui t0,0x30; jalr x0,0(t0)
        self.assertEqual(model.chain.write_vector, 0)

    def test_failed_load_boots_the_flash_through_the_cache_flush(self):
        b, model, _ = board()
        b.identify()
        model.chain.stuck[jt.APP_ADDRESS + 0x100] = b"\xde\xad\xbe\xef"
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(jt.JtagError, "does not hold what was written"):
            b.run_application(bytes(1024))
        self.assertEqual(model.chain.write_vector, 0)
        self.assertEqual(model.memory[0xFFF8], struct.pack("<L", jt.TRAMPOLINE_ADDRESS))
        self.assertEqual(model.memory[0xFFFC], struct.pack("<L", jt.BOOT_MAGIC_VALUE))
        tail = [struct.unpack("<L", model.memory[jt.TRAMPOLINE_ADDRESS + 4 * i])[0]
                for i in (512, 513)]
        self.assertEqual(tail, [0x800002B7, 0x00028067])       # into the bootloader

    def test_failed_load_says_when_the_cpu_stays_in_reset(self):
        b, model, ftdi = board()
        b.identify()
        ftdi.fail_in_reset = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(OSError):
            b.run_application(bytes(1024))
        self.assertEqual(model.chain.write_vector, jt.OUTPUT_CPU_RESET)
        self.assertIn("stays in reset", out.getvalue())

    def test_trampoline_reaches_unaligned_targets(self):
        for target in (0x30000, 0x30800, 0x12345678 & ~3):
            words = struct.unpack("<514L", jt.Board.cache_flush_trampoline(target))
            lui, jalr = words[512], words[513]
            upper = lui & 0xFFFFF000
            imm = jalr >> 20
            imm -= (imm & 0x800) << 1
            self.assertEqual((upper + imm) & 0xFFFFFFFF, target)

    def test_failed_load_boots_flash_through_flush(self):
        b, model, _ = board()
        real_read = b.chain.read
        # Verifying the image fails; the cache flush and boot request verify.
        b.chain.read = lambda address, length: (
            bytes(length) if address >= jt.APP_ADDRESS else real_read(address, length))
        with self.assertRaises(jt.JtagError):
            b.run_application(b"\x13\x00\x00\x00" * 16)
        b.chain.read = real_read
        self.assertEqual(model.chain.write_vector, 0)            # CPU released
        self.assertEqual(model.memory[0xFFF8], struct.pack("<L", jt.TRAMPOLINE_ADDRESS))
        tail = struct.unpack("<L", model.memory[jt.TRAMPOLINE_ADDRESS + 4 * 512])[0]
        self.assertEqual(tail, 0x800002B7)                       # back into the bootloader

    def test_reset_flushes_then_reenters_bootloader(self):
        b, model, _ = board()
        b.reset_cpu()
        self.assertEqual(model.memory[0xFFF8], struct.pack("<L", jt.TRAMPOLINE_ADDRESS))
        self.assertEqual(model.memory[0xFFFC], struct.pack("<L", jt.BOOT_MAGIC_VALUE))
        tail = [struct.unpack("<L", model.memory[jt.TRAMPOLINE_ADDRESS + 4 * i])[0]
                for i in (512, 513)]
        self.assertEqual(tail, [0x800002B7, 0x00028067])       # lui t0,0x80000; jalr x0,0(t0)
        self.assertEqual(model.chain.write_vector, 0)

    def test_configure_sends_bitstream_msb_first(self):
        b, model, _ = board()
        b.identify()
        body = b"\xff" * 16 + b"\xaa\x99\x55\x66" + bytes(range(64))
        with tempfile.NamedTemporaryFile(suffix=".bit") as handle:
            handle.write(body)
            handle.flush()
            b.configure(handle.name)
        bits = model.config_bits
        sent = bytes(sum(bits[i + k] << (7 - k) for k in range(8))
                     for i in range(0, len(bits), 8))
        self.assertEqual(sent, body)
        self.assertTrue(model.configured)

    def test_bitstream_for_another_part_is_refused_before_jprogram(self):
        b, model, _ = board()
        b.identify()
        body = (b"\xff" * 16 + b"\xaa\x99\x55\x66" + b"\x30\x01\x80\x01"
                + (0x03631093).to_bytes(4, "big") + bytes(64))       # an XC7A100T image
        with tempfile.NamedTemporaryFile(suffix=".bit") as handle:
            handle.write(body)
            handle.flush()
            with self.assertRaisesRegex(jt.JtagError, "not for this XC7A50T"):
                b.configure(handle.name)
        self.assertFalse(model.jprogram)
        self.assertTrue(model.configured)

    def test_bitstream_for_this_part_is_accepted(self):
        b, model, _ = board()
        b.identify()
        body = (b"\xff" * 16 + b"\xaa\x99\x55\x66" + b"\x30\x01\x80\x01"
                + (0x0362C093).to_bytes(4, "big") + bytes(64))
        with tempfile.NamedTemporaryFile(suffix=".bit") as handle:
            handle.write(body)
            handle.flush()
            b.configure(handle.name)
        self.assertTrue(model.jprogram)
        self.assertTrue(model.configured)

    def test_failed_configuration_says_the_fpga_is_blank(self):
        model = ArtixModel()
        model.accept_config = False
        b, _, _ = board(model)
        b.identify()
        out = io.StringIO()
        with tempfile.NamedTemporaryFile(suffix=".bit") as handle:
            handle.write(b"\xaa\x99\x55\x66" + bytes(64))
            handle.flush()
            with contextlib.redirect_stdout(out), self.assertRaises(jt.JtagError):
                b.configure(handle.name)
        self.assertIn("FPGA is now unconfigured", out.getvalue())

    def test_dump_unaligned_range(self):
        model = ArtixModel()
        model.memory[0x30000] = b"\x00\x01\x02\x03"
        model.memory[0x30004] = b"\x04\x05\x06\x07"
        with tempfile.NamedTemporaryFile() as handle, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = jt.main(["dump", "0x30002", "4", "-o", handle.name],
                         mpsse=cable(model)[0])
            self.assertEqual(rc, 0)
            self.assertEqual(open(handle.name, "rb").read(), b"\x02\x03\x04\x05")

    def test_main_probe(self):
        model = ArtixModel()
        rc = jt.main(["probe"], mpsse=cable(model)[0])
        self.assertEqual(rc, 0)


def bitstream(idcode=None, size=64):
    """A minimal 7-series bitstream, optionally with an IDCODE register write."""
    body = b"\xff" * 16 + b"\xaa\x99\x55\x66"
    if idcode is not None:
        body += b"\x30\x01\x80\x01" + idcode.to_bytes(4, "big")
    return body + bytes(size)


def run_main(argv, model=None):
    """jt.main against a fresh simulated board; returns (rc, log, model, ftdi)."""
    model = model or ArtixModel()
    ftdi = FakeFtdi(model)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = jt.main(argv, mpsse=jt.Mpsse(ftdi))
    return rc, out.getvalue(), model, ftdi


def console_sink(sink):
    """Board.console binds sys.stdout when the module loads, so redirect its default."""
    return mock.patch.object(jt.Board.console, "__defaults__", (sink,))


class RecordingFtdi:
    """Records MPSSE writes and serves canned reads."""

    def __init__(self, reply=b""):
        self.writes = []
        self.reply = reply
        self.closed = None
        self.fail_writes = False

    def write_data(self, data):
        if self.fail_writes:
            raise OSError("USB device disconnected")
        self.writes.append(bytes(data))

    def read_data_bytes(self, size, attempt=1):
        self.attempt = attempt
        return self.reply[:size]

    def close(self, freeze=None):
        self.closed = freeze


class BitstreamIdcodeTest(unittest.TestCase):
    def test_reads_the_idcode_register_write(self):
        self.assertEqual(jt.bitstream_idcode(bitstream(0x13631093)), 0x13631093)

    def test_no_idcode_write_gives_none(self):
        self.assertIsNone(jt.bitstream_idcode(bitstream()))

    def test_truncated_idcode_write_gives_none(self):
        self.assertIsNone(jt.bitstream_idcode(b"\xaa\x99\x55\x66\x30\x01\x80\x01\x03\x63\x10"))


class DeviceLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "locks", "c64u.lock")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("U64II_JTAG_LOCK", None)
        lock_path = mock.patch.object(jt, "LOCK_PATH", self.path)
        lock_path.start()
        self.addCleanup(lock_path.stop)

    def hold(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(text)
        handle.flush()
        self.addCleanup(handle.close)
        return handle

    def free(self):
        with open(self.path, "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

    def test_off_touches_nothing(self):
        os.environ["U64II_JTAG_LOCK"] = "off"
        with jt.DeviceLock() as lock:
            self.assertIsNone(lock.handle)
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))

    def test_lock_is_exclusive_until_exit(self):
        with jt.DeviceLock(wait=0) as lock:
            self.assertIsNotNone(lock.handle)
            self.assertFalse(self.free())
        self.assertIsNone(lock.handle)
        self.assertTrue(self.free())

    def test_held_lock_names_its_holder(self):
        self.hold("pid=999999999 deploy\n")
        with self.assertRaises(jt.JtagError) as caught:
            with jt.DeviceLock(wait=0):
                self.fail("entered a held lock")
        self.assertEqual(str(caught.exception),
                         f"the FT232H is in use ({self.path}: pid=999999999 deploy)")

    def test_held_lock_without_a_note_says_held(self):
        self.hold("")
        with self.assertRaisesRegex(jt.JtagError, r"c64u\.lock: held\)$"):
            jt.DeviceLock(wait=0).__enter__()

    def test_lock_held_by_an_ancestor_covers_this_process(self):
        self.hold("pid=4100 with-device-locks\n")
        with mock.patch.object(jt.os, "getppid", return_value=4100), \
                jt.DeviceLock(wait=0) as lock:
            self.assertIsNone(lock.handle)          # not taken a second time
        self.assertFalse(self.free())               # the ancestor still holds it

    def test_waits_once_announced_and_takes_the_released_lock(self):
        holder = self.hold("pid=999999999 console\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(jt.time, "sleep", side_effect=lambda s: holder.close()) as nap:
            with jt.DeviceLock(wait=60) as lock:
                self.assertIsNotNone(lock.handle)
                self.assertFalse(self.free())
        nap.assert_called_once_with(0.2)
        self.assertEqual(out.getvalue(), f"[u64ii-jtag] waiting for {self.path}\n")

    def test_wait_expires_after_announcing_once(self):
        self.hold("pid=999999999 console\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(jt.JtagError, "in use"):
            jt.DeviceLock(wait=0.3).__enter__()
        self.assertEqual(out.getvalue().count("waiting for"), 1)


class AncestorsTest(unittest.TestCase):
    # A fixed parent pid: under a container entrypoint the real parent can be
    # pid 1, where the walk stops before it starts.
    PARENT = 4100

    def setUp(self):
        getppid = mock.patch.object(jt.os, "getppid", return_value=self.PARENT)
        getppid.start()
        self.addCleanup(getppid.stop)

    def test_follows_parent_links_and_stops_on_a_loop(self):
        stat = mock.mock_open(read_data="4242 (with (odd) name) S 4242 1 1")
        with mock.patch.object(jt, "open", stat, create=True):
            self.assertEqual(jt._ancestors(), {self.PARENT, 4242})

    def test_unreadable_proc_stops_at_the_parent(self):
        with mock.patch.object(jt, "open", side_effect=OSError("no /proc"), create=True):
            self.assertEqual(jt._ancestors(), {self.PARENT})

    def test_malformed_stat_stops_at_the_parent(self):
        with mock.patch.object(jt, "open", mock.mock_open(read_data="garbage"), create=True):
            self.assertEqual(jt._ancestors(), {self.PARENT})

    def test_init_ends_the_walk(self):
        with mock.patch.object(jt.os, "getppid", return_value=1):
            self.assertEqual(jt._ancestors(), set())


class RealAncestorsTest(unittest.TestCase):
    @unittest.skipIf(os.getppid() <= 1, "the test runner is a child of init")
    def test_includes_the_parent_but_not_this_process(self):
        pids = jt._ancestors()
        self.assertIn(os.getppid(), pids)
        self.assertNotIn(os.getpid(), pids)


class MpsseTest(unittest.TestCase):
    def test_open_without_pyftdi_says_how_to_get_it(self):
        with mock.patch.dict(sys.modules, {"pyftdi": None, "pyftdi.ftdi": None}), \
                self.assertRaisesRegex(jt.JtagError, "pyftdi is missing.*u64ii_jtag.sh"):
            jt.Mpsse.open("ftdi://ftdi:232h/1", 3e6)

    def test_open_starts_with_tms_high_and_loopback_off(self):
        opened = []

        class Ftdi(RecordingFtdi):
            def open_mpsse_from_url(self, url, **kwargs):
                opened.append((url, kwargs))

        package, module = types.ModuleType("pyftdi"), types.ModuleType("pyftdi.ftdi")
        module.Ftdi, package.ftdi = Ftdi, module
        with mock.patch.dict(sys.modules, {"pyftdi": package, "pyftdi.ftdi": module}):
            m = jt.Mpsse.open("ftdi://x/2", 1e6)
        self.assertEqual(opened, [("ftdi://x/2", dict(direction=jt.DIRECTION, initial=jt.PIN_TMS,
                                                      frequency=1e6, latency=1))])
        self.assertEqual(m.ftdi.writes, [bytes((jt.OP_LOOPBACK_OFF,))])

    def test_release_drops_queued_work_and_freezes_inputs(self):
        ftdi = RecordingFtdi()
        m = jt.Mpsse(ftdi)
        m.tms(1, 1, read=True)
        m.release()
        self.assertEqual(ftdi.writes, [bytes((jt.OP_SET_LOW, 0, 0))])
        self.assertTrue(ftdi.closed)
        self.assertEqual((m.pending, m.expect), (bytearray(), 0))

    def test_release_still_closes_a_failed_adapter(self):
        ftdi = RecordingFtdi()
        ftdi.fail_writes = True
        jt.Mpsse(ftdi).release()
        self.assertIsNone(ftdi.closed)                 # plain close, no freeze

    def test_flush_with_nothing_queued_writes_nothing(self):
        ftdi = RecordingFtdi()
        self.assertEqual(jt.Mpsse(ftdi).flush(), b"")
        self.assertEqual(ftdi.writes, [])

    def test_flush_without_reads_sends_no_send_immediate(self):
        ftdi = RecordingFtdi()
        m = jt.Mpsse(ftdi)
        m.tms(0b011111, 6)
        self.assertEqual(m.flush(), b"")
        self.assertEqual(ftdi.writes, [bytes((jt.OP_W_TMS, 5, 0x1F))])

    def test_flush_with_reads_asks_for_them(self):
        ftdi = RecordingFtdi(reply=b"\x12\x34")
        m = jt.Mpsse(ftdi)
        m.bits_out(0x5, 3, read=True)
        m.tms(1, 1, tdi=1, read=True)
        self.assertEqual(m.flush(), b"\x12\x34")
        self.assertEqual(ftdi.writes, [bytes((jt.OP_RW_BITS_LSB, 2, 5, jt.OP_RW_TMS, 0, 0x81,
                                              jt.OP_SEND_IMMEDIATE))])
        self.assertEqual(ftdi.attempt, 200)

    def test_short_read_is_an_error(self):
        m = jt.Mpsse(RecordingFtdi(reply=b"\x01"))
        m.bytes_out(b"ab", read=True)
        with self.assertRaisesRegex(jt.JtagError, "returned 1 of 2 bytes"):
            m.flush()

    def test_clocks_split_into_byte_and_bit_commands(self):
        m = jt.Mpsse(RecordingFtdi())
        m.clocks(8 * 0x10000 + 8 + 3)
        self.assertEqual(bytes(m.pending), bytes((jt.OP_CLOCK_BYTES, 0xFF, 0xFF,
                                                  jt.OP_CLOCK_BYTES, 0, 0,
                                                  jt.OP_CLOCK_BITS, 2)))
        m.pending.clear()
        m.clocks(0)
        self.assertEqual(m.pending, bytearray())

    def test_bytes_out_splits_at_64k_and_counts_reads(self):
        m = jt.Mpsse(RecordingFtdi())
        m.bytes_out(bytes(0x10001), read=True)
        self.assertEqual(m.expect, 0x10001)
        self.assertEqual(bytes(m.pending[:3]), bytes((jt.OP_RW_BYTES_LSB, 0xFF, 0xFF)))
        self.assertEqual(bytes(m.pending[0x10003:0x10006]), bytes((jt.OP_RW_BYTES_LSB, 0, 0)))
        self.assertEqual(len(m.pending), 2 * 3 + 0x10001)

    def test_msb_first_opcodes_never_read(self):
        m = jt.Mpsse(RecordingFtdi())
        m.bytes_out(b"\x80", msb_first=True)
        m.bits_out(0x81, 7, msb_first=True)
        self.assertEqual(bytes(m.pending), bytes((jt.OP_W_BYTES_MSB, 0, 0, 0x80,
                                                  jt.OP_W_BITS_MSB, 6, 0x81)))
        self.assertEqual(m.expect, 0)


class TapEdgeTest(unittest.TestCase):
    def test_bypass_delay_zero_when_tdo_follows_tdi(self):
        b, _, _ = board()
        b.tap.dr = lambda tdi, nbits, read=False: tdi
        self.assertEqual(b.tap.bypass_delay(), 0)

    def test_bypass_delay_none_fits(self):
        b, _, _ = board()
        b.tap.dr = lambda tdi, nbits, read=False: 0
        self.assertEqual(b.tap.bypass_delay(), -1)

    def test_wrong_bypass_delay_is_refused(self):
        b, _, _ = board()
        b.tap.bypass_delay = lambda: 0
        with self.assertRaisesRegex(jt.JtagError, "bypass delay of 0, expected one device"):
            b.identify()

    def test_100t_of_any_revision_is_supported(self):
        model = ArtixModel()
        model.IDCODE = 0x33631093
        b, _, _ = board(model)
        self.assertEqual(b.identify(), 0x33631093)
        self.assertEqual((b.part, b.bitstream, b.idcode_value),
                         ("XC7A100T", "u64e2_100t.bit", 0x33631093))

    def test_other_artix_is_named_and_refused(self):
        model = ArtixModel()
        model.IDCODE = 0x2362D093
        b, _, _ = board(model)
        with self.assertRaisesRegex(jt.JtagError,
                                    "0x2362D093 is XC7A35T; no Ultimate 64 Elite II"):
            b.identify()
        self.assertEqual(model.ir_shifts, 0)

    def test_all_zero_idcode_is_unpowered(self):
        model = ArtixModel()
        model.IDCODE = 0
        b, _, _ = board(model)
        with self.assertRaisesRegex(jt.JtagError, "0x00000000: no device answers"):
            b.identify()

    def test_ir_capture_reports_configuration_done(self):
        b, model, _ = board()
        self.assertEqual(b.tap.ir_capture(), 0x31)
        model.configured = False
        self.assertEqual(b.tap.ir_capture(), 0x11)


class MemoryEdgeTest(unittest.TestCase):
    def test_debug_word(self):
        b, _, _ = board()
        self.assertEqual(b.chain.debug_word(), 0x12345678)

    def test_misaligned_writes_are_refused_before_any_scan(self):
        for address, data in ((0x30001, b"abcd"), (0x30000, b"abc"), (0x30000, b"")):
            b, model, _ = board()
            with self.assertRaisesRegex(jt.JtagError, "whole, aligned words"):
                b.chain.write(address, data)
            self.assertIsNone(b.chain.register)
            self.assertEqual(model.memory, {})

    def test_misaligned_reads_are_refused(self):
        b, _, _ = board()
        for address, length in ((0x30002, 4), (0x30000, 6)):
            with self.assertRaisesRegex(jt.JtagError, "whole, aligned words"):
                b.chain.read(address, length)

    def test_zero_length_read_is_empty(self):
        b, _, _ = board()
        self.assertEqual(b.chain.read(0x30000, 0), b"")

    def test_unwritten_memory_reads_back_as_the_model_fill(self):
        b, _, _ = board()
        self.assertEqual(b.chain.read(0x40000, 8), b"\xee" * 8)

    def test_read_stalls_on_an_empty_fifo(self):
        b, _, _ = board()
        calls = []
        b.chain.drain = lambda register, pops: calls.append(pops) or (0, b"")
        with mock.patch.object(jt, "READ_STALL_SCANS", 3), \
                self.assertRaisesRegex(jt.JtagError,
                                       "memory read at 0x00001000 stalled: the read FIFO "
                                       "stays empty"):
            b.chain.read(0x1000, 8)
        self.assertEqual(calls, [0, 0, 0, 0])

    def test_progress_resets_the_stall_count(self):
        b, _, _ = board()
        replies = iter([(0, b"")] * 3 + [(4, b""), (0, b"abcd")]
                       + [(0, b"")] * 3 + [(4, b""), (0, b"efgh")])
        pops = []

        def drain(register, n):
            self.assertEqual(register, jt.USER_READ_FIFO)
            pops.append(n)
            level, data = next(replies)
            self.assertEqual(len(data), n)
            return level, data

        b.chain.drain = drain
        with mock.patch.object(jt, "READ_STALL_SCANS", 3):
            self.assertEqual(b.chain.read(0x1000, 8), b"abcdefgh")
        self.assertEqual(pops, [0, 0, 0, 0, 4, 0, 0, 0, 0, 4])

    def test_drain_reports_level_and_pops(self):
        b, model, _ = board()
        model.chain.console_fifo.extend(b"xyz")
        self.assertEqual(b.chain.drain(jt.USER_CONSOLE, 0), (3, b""))
        self.assertEqual(b.chain.drain(jt.USER_CONSOLE, 2), (3, b"xy"))
        self.assertEqual(b.chain.drain(jt.USER_CONSOLE, 1), (1, b"z"))
        self.assertEqual(model.chain.console_fifo, [])


class StubChain:
    """A user chain whose memory and output writes can fail on demand."""

    def __init__(self, fail_write=(), fail_outputs=False):
        self.calls = []
        self.fail_write = fail_write
        self.fail_outputs = fail_outputs

    def write(self, address, data):
        self.calls.append(("write", address, len(data)))
        if address in self.fail_write:
            raise OSError(f"write 0x{address:X} lost")

    def read(self, address, length):
        raise AssertionError("no read expected")

    def set_outputs(self, value):
        self.calls.append(("outputs", value))
        if self.fail_outputs and value == 0:
            raise OSError("cable pulled")


class BoardEdgeTest(unittest.TestCase):
    def quiet(self):
        out = io.StringIO()
        return out, contextlib.redirect_stdout(out)

    def test_board_opens_the_adapter_it_is_given(self):
        model = ArtixModel()
        ftdi = FakeFtdi(model)
        with mock.patch.object(jt.Mpsse, "open", return_value=jt.Mpsse(ftdi)) as opener:
            with jt.Board("ftdi://ftdi:232h/2", 1e6) as b:
                self.assertEqual(b.identify(), ArtixModel.IDCODE)
        opener.assert_called_once_with("ftdi://ftdi:232h/2", 1e6)
        self.assertEqual(ftdi.pins, (0, 0))

    def test_require_design_without_one(self):
        b, model, _ = board()
        b.identify()
        model.configured = False
        self.assertFalse(b.design_loaded())
        with self.assertRaisesRegex(jt.JtagError, "user ID is not 0xDEAD1541"):
            b.require_design()

    def test_file_without_sync_word_is_not_a_bitstream(self):
        b, model, _ = board()
        b.identify()
        for body in (bytes(64), bytes(4096) + b"\xaa\x99\x55\x66"):
            with tempfile.NamedTemporaryFile(suffix=".bit") as handle:
                handle.write(body)
                handle.flush()
                with self.assertRaisesRegex(jt.JtagError, "no 7-series sync word"):
                    b.configure(handle.name)
        self.assertFalse(model.jprogram)

    def test_configure_waits_for_init(self):
        b, model, _ = board()
        b.identify()
        real = b.tap.ir_capture
        busy = [2]                       # INIT stays low for the first two polls

        def ir_capture():
            if model.jprogram and busy[0]:
                busy[0] -= 1
                return 0
            return real()

        b.tap.ir_capture = ir_capture
        out, quiet = self.quiet()
        with tempfile.NamedTemporaryFile(suffix=".bit") as handle, quiet, \
                mock.patch.object(jt.time, "sleep") as nap:
            handle.write(bitstream(0x0362C093))
            handle.flush()
            b.configure(handle.name)
        self.assertEqual(nap.call_args_list, [mock.call(0.01), mock.call(0.01), mock.call(0.5)])
        self.assertTrue(model.configured)
        self.assertIn("configured in", out.getvalue())

    def test_configure_gives_up_when_init_never_rises(self):
        b, model, _ = board()
        b.identify()
        b.tap.ir_capture = lambda: 0
        clock = iter(range(0, 1000, 1))
        out, quiet = self.quiet()
        with tempfile.NamedTemporaryFile(suffix=".bit") as handle, quiet, \
                mock.patch.object(jt.time, "sleep"), \
                mock.patch.object(jt.time, "monotonic", side_effect=lambda: next(clock) * 0.4):
            handle.write(bitstream())
            handle.flush()
            with self.assertRaisesRegex(jt.JtagError, "did not clear its configuration"):
                b.configure(handle.name)
        self.assertTrue(model.jprogram)
        self.assertEqual(model.config_bits, [])
        self.assertIn("FPGA is now unconfigured", out.getvalue())

    def test_unverified_write_reads_nothing_back(self):
        b, _, _ = board()
        b.chain = StubChain()
        b.write_verified(0x30000, b"abcd", verify=False)
        self.assertEqual(b.chain.calls, [("write", 0x30000, 4)])

    def test_verify_retries_once(self):
        b, model, _ = board()
        real_read = b.chain.read
        reads = []

        def read(address, length):
            reads.append(address)
            data = real_read(address, length)
            return b"\x00" + data[1:] if len(reads) == 1 else data

        b.chain.read = read
        out, quiet = self.quiet()
        with quiet:
            b.write_verified(0x30000, b"\x11\x22\x33\x44", verify=True)
        self.assertEqual(reads, [0x30000, 0x30000])
        self.assertEqual(model.memory[0x30000], b"\x11\x22\x33\x44")
        self.assertEqual(out.getvalue(), "[u64ii-jtag] verify failed at 0x00030000 "
                                         "(attempt 1): wrote 11, read 00\n")

    def test_failure_release_reports_every_step_that_fails(self):
        b, _, _ = board()
        b.chain = StubChain(fail_write=(jt.TRAMPOLINE_ADDRESS, jt.BOOT_MAGIC_ADDRESS),
                            fail_outputs=True)
        out, quiet = self.quiet()
        with quiet:
            b.release_after_failure(flush=True)
        self.assertEqual(b.chain.calls, [("write", jt.TRAMPOLINE_ADDRESS, 2056),
                                         ("write", jt.BOOT_MAGIC_ADDRESS, 8),
                                         ("outputs", 0)])
        text = out.getvalue()
        self.assertIn("could not set up the cache flush: write 0x8000 lost", text)
        self.assertIn("could not clear the boot request: write 0xFFF8 lost", text)
        self.assertIn("could not release the CPU (cable pulled): it stays in reset", text)
        self.assertNotIn("CPU released", text)

    def test_failed_reset_clears_the_boot_request_and_releases(self):
        b, model, _ = board()
        model.chain.stuck[jt.TRAMPOLINE_ADDRESS] = b"\xde\xad\xbe\xef"
        model.memory[jt.BOOT_MAGIC_ADDRESS] = b"\x01\x02\x03\x04"
        out, quiet = self.quiet()
        with quiet, self.assertRaisesRegex(jt.JtagError, "0x00008000 does not hold"):
            b.reset_cpu()
        self.assertEqual(model.memory[jt.BOOT_MAGIC_ADDRESS], bytes(4))
        self.assertEqual(model.memory[jt.BOOT_MAGIC_ADDRESS + 4], bytes(4))
        self.assertEqual(model.chain.write_vector, 0)
        self.assertIn("CPU released; the bootloader starts the flashed application",
                      out.getvalue())
        self.assertNotIn("cache flush", out.getvalue())

    def test_run_without_verify_reads_back_only_the_boot_request(self):
        b, model, _ = board()
        real_read = b.chain.read
        reads = []
        b.chain.read = lambda address, length: reads.append(address) or real_read(address, length)
        out, quiet = self.quiet()
        with quiet:
            b.run_application(b"\x01\x02\x03\x04\x05", verify=False)
        self.assertEqual(model.memory[jt.APP_ADDRESS + 4], b"\x05\x00\x00\x00")
        self.assertEqual(reads, [jt.TRAMPOLINE_ADDRESS, jt.BOOT_MAGIC_ADDRESS])
        self.assertIn("loading 8 bytes at 0x30000\n", out.getvalue())
        self.assertNotIn("read-back", out.getvalue())

    def test_console_until_interrupted(self):
        b, model, _ = board()
        model.chain.console_fifo.extend(b"boot\n")
        sink = io.StringIO()
        with mock.patch.object(jt.time, "sleep", side_effect=KeyboardInterrupt) as nap, \
                self.assertRaises(KeyboardInterrupt):
            b.console(0, sink)
        self.assertEqual(sink.getvalue(), "boot\n")
        nap.assert_called_once_with(0.05)

    def test_console_returns_the_byte_count(self):
        b, model, _ = board()
        model.chain.console_fifo.extend(bytes(range(256)) + b"!")
        sink = io.StringIO()
        self.assertEqual(b.console(0.2, sink), 257)
        self.assertEqual(sink.getvalue(), (bytes(range(256)) + b"!").decode("latin-1"))

    @unittest.expectedFailure
    def test_console_follows_a_redirected_stdout(self):
        # Suspected defect: Board.console's `sink=sys.stdout` default is bound at
        # import (u64ii_jtag.py:671), so `console` output ignores a later
        # redirection of sys.stdout, unlike every log() line.
        # No console_sink here: patching the default would reproduce the defect
        # inside the test, and the test could then never pass after a fix.
        # Until the fix, "hello" goes to the stdout of the test run.
        b, model, _ = board()
        model.chain.console_fifo.extend(b"hello")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            b.console(0.1)
        self.assertEqual(out.getvalue(), "hello")


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        nap = mock.patch.object(jt.time, "sleep")
        self.sleep = nap.start()
        self.addCleanup(nap.stop)

    def file(self, *parts, data):
        path = os.path.join(self.dir.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def repo(self):
        return mock.patch.dict(os.environ, {"ULTIMATE_REPO_DIR": self.dir.name})

    def test_a_command_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as caught:
            jt.parse_args([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("required: command", err.getvalue())

    def test_defaults(self):
        with self.repo():
            args = jt.parse_args(["--frequency", "1e6", "run"])
        self.assertEqual((args.url, args.frequency), (jt.DEFAULT_URL, 1e6))
        self.assertEqual(args.bin, os.path.join(self.dir.name, "target", "u64ii", "riscv",
                                                "ultimate", "result", "ultimate.bin"))
        self.assertEqual((args.fpga, args.warm, args.no_verify, args.console),
                         ("auto", False, False, 0))
        self.assertEqual(jt.parse_args(["fpga"]).bit, "auto")
        self.assertEqual(jt.parse_args(["console"]).secs, 0)

    def test_repo_path_defaults_to_this_checkout(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ULTIMATE_REPO_DIR", None)
            self.assertEqual(jt.repo_path("external"), os.path.join(jt.ROOT, "external"))

    def test_probe_with_a_design(self):
        rc, out, _, _ = run_main(["probe"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "[u64ii-jtag] IDCODE 0x1362C093: XC7A50T, revision 1",
            "[u64ii-jtag] IR capture 0x31: configuration done",
            "[u64ii-jtag] user chain answers 0xDEAD1541: an Ultimate design is running",
            "[u64ii-jtag] user chain debug word 0x12345678",
            "[u64ii-jtag] bitstream for this part: external/u64e2_50t.bit",
        ])

    def test_probe_of_a_blank_fpga(self):
        model = ArtixModel()
        model.configured = False
        rc, out, _, _ = run_main(["probe"], model)
        self.assertEqual(rc, 0)
        self.assertIn("IR capture 0x11: configuration NOT done", out)
        self.assertRegex(out, r"user chain answers 0x[0-9A-F]{8}: no Ultimate design is running")
        self.assertNotIn("debug word", out)

    def test_main_reports_identify_errors_and_releases_the_pins(self):
        model = ArtixModel()
        model.IDCODE = 0xFFFFFFFF
        rc, out, _, ftdi = run_main(["probe"], model)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("[u64ii-jtag] ERROR: IDCODE reads 0xFFFFFFFF"))
        self.assertEqual(ftdi.pins, (0, 0))

    def test_main_takes_the_device_lock(self):
        with mock.patch.object(jt, "DeviceLock") as lock:
            lock.return_value.__enter__.side_effect = jt.JtagError("the FT232H is in use (x)")
            rc, out, _, _ = run_main(["probe"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "[u64ii-jtag] ERROR: the FT232H is in use (x)\n")

    def test_fpga_with_an_explicit_bitstream(self):
        path = self.file("my.bit", data=bitstream(0x0362C093))
        rc, out, model, _ = run_main(["fpga", "--bit", path])
        self.assertEqual(rc, 0)
        self.assertTrue(model.jprogram and model.configured)
        self.assertIn(f"configuring the XC7A50T from {path}", out)
        self.assertTrue(out.endswith("the bootloader now starts the flashed application\n"))

    def test_fpga_auto_picks_the_bitstream_by_idcode(self):
        model = ArtixModel()
        model.IDCODE = 0x03631093
        path = self.file("external", "u64e2_100t.bit", data=bitstream(0x03631093))
        with self.repo():
            rc, out, model, _ = run_main(["fpga"], model)
        self.assertEqual(rc, 0)
        self.assertIn(f"configuring the XC7A100T from {path}", out)

    def test_fpga_auto_without_the_bitstream(self):
        with self.repo():
            rc, out, model, _ = run_main(["fpga"])
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: no bitstream for the XC7A50T at "
                      + os.path.join(self.dir.name, "external", "u64e2_50t.bit"), out)
        self.assertFalse(model.jprogram)

    def test_run_refuses_an_elf(self):
        path = self.file("ultimate.elf", data=b"\x7fELF" + bytes(60))
        rc, out, model, _ = run_main(["run", "--warm", "--bin", path])
        self.assertEqual(rc, 1)
        self.assertIn(f"ERROR: {path} is an ELF; pass the raw ultimate.bin", out)
        self.assertEqual(model.chain.write_vector, 0)
        self.assertNotIn(jt.APP_ADDRESS, model.memory)

    def test_run_with_a_missing_image_is_an_error(self):
        path = os.path.join(self.dir.name, "absent.bin")
        rc, out, _, _ = run_main(["run", "--warm", "--bin", path])
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: [Errno 2]", out)

    def test_warm_run_keeps_the_fpga_and_shows_the_console(self):
        path = self.file("ultimate.bin", data=b"\x13\x00\x00\x00" * 8)
        model = ArtixModel()
        model.chain.console_fifo.extend(b"*** Ultimate\n")
        sink = io.StringIO()
        with console_sink(sink), mock.patch.object(jt.Board, "run_application",
                                                   autospec=True) as load:
            rc, out, model, _ = run_main(["run", "--warm", "--no-verify", "--console", "0.1",
                                          "--bin", path], model)
        self.assertEqual(rc, 0)
        load.assert_called_once_with(mock.ANY, b"\x13\x00\x00\x00" * 8, verify=False)
        self.assertFalse(model.jprogram)
        self.assertIn("--warm: keeping the running FPGA image", out)
        self.assertEqual(sink.getvalue(), "*** Ultimate\n")

    def test_warm_run_needs_a_running_design(self):
        path = self.file("ultimate.bin", data=bytes(8))
        model = ArtixModel()
        model.configured = False
        rc, out, model, _ = run_main(["run", "--warm", "--bin", path], model)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: the FPGA does not run an Ultimate design", out)
        self.assertEqual(model.chain.write_vector, 0)

    def test_cold_run_configures_then_loads(self):
        bit = self.file("my.bit", data=bitstream(0x0362C093))
        app = self.file("ultimate.bin", data=b"\xaa\xbb\xcc\xdd\xee")
        rc, out, model, _ = run_main(["run", "--fpga", bit, "--bin", app])
        self.assertEqual(rc, 0)
        self.assertTrue(model.jprogram)
        self.assertEqual(model.memory[jt.APP_ADDRESS], b"\xaa\xbb\xcc\xdd")
        self.assertEqual(model.memory[jt.APP_ADDRESS + 4], b"\xee\x00\x00\x00")
        self.assertEqual(model.memory[jt.BOOT_MAGIC_ADDRESS + 4],
                         struct.pack("<L", jt.BOOT_MAGIC_VALUE))
        self.assertIn(mock.call(1.0), self.sleep.call_args_list)
        self.assertIn("with read-back", out)

    def test_recover_needs_the_50t(self):
        model = ArtixModel()
        model.IDCODE = 0x03631093
        rc, out, model, _ = run_main(["recover"], model)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: the recovery kit carries an XC7A50T bitstream only", out)
        self.assertFalse(model.jprogram)

    def test_recover_loads_the_kit(self):
        bit = self.file("recovery", "u64ii", "u64_mk2_artix.bit", data=bitstream(0x0362C093))
        self.file("recovery", "u64ii", "ultimate.bin", data=b"KIT!" * 4)
        with self.repo():
            rc, out, model, _ = run_main(["recover"])
        self.assertEqual(rc, 0)
        self.assertIn(f"configuring the XC7A50T from {bit}", out)
        self.assertEqual(model.memory[jt.APP_ADDRESS + 12], b"KIT!")
        self.assertIn("recovery application (3.14c) runs from RAM", out)
        self.assertIn("use 'fpga', not 'reset'", out)

    def test_reset_requests_a_boot_through_the_flush(self):
        rc, out, model, _ = run_main(["reset"])
        self.assertEqual(rc, 0)
        self.assertEqual(model.memory[jt.BOOT_MAGIC_ADDRESS],
                         struct.pack("<L", jt.TRAMPOLINE_ADDRESS))
        self.assertEqual(model.chain.write_vector, 0)
        self.assertIn("CPU restarted through the cache flush", out)

    def test_reset_needs_a_running_design(self):
        model = ArtixModel()
        model.configured = False
        rc, out, model, _ = run_main(["reset"], model)
        self.assertEqual(rc, 1)
        self.assertNotIn(jt.BOOT_MAGIC_ADDRESS, model.memory)

    def test_console_command_stops_quietly_on_ctrl_c(self):
        model = ArtixModel()
        model.chain.console_fifo.extend(b"READY.\n")
        sink = io.StringIO()
        self.sleep.side_effect = KeyboardInterrupt
        with console_sink(sink):
            rc, out, _, _ = run_main(["console"], model)
        self.assertEqual(rc, 0)
        self.assertEqual(sink.getvalue(), "READY.\n")
        self.assertEqual(out, "")

    def test_console_command_needs_a_running_design(self):
        model = ArtixModel()
        model.configured = False
        rc, out, _, _ = run_main(["console", "--secs", "1"], model)
        self.assertEqual(rc, 1)
        self.assertIn("does not run an Ultimate design", out)

    def test_dump_hexdump(self):
        model = ArtixModel()
        for i in range(8):
            model.memory[0x1000 + 4 * i] = bytes(range(0x40 + 4 * i, 0x44 + 4 * i))
        model.memory[0x1000] = b"\x00\x41\x7f\x80"
        rc, out, _, _ = run_main(["dump", "0x1001", "20"], model)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "00001001  41 7f 80 44 45 46 47 48 49 4a 4b 4c 4d 4e 4f 50  A..DEFGHIJKLMNOP",
            "00001011  51 52 53 54                                      QRST",
        ])

    def test_dump_rejects_bad_ranges(self):
        for address, length in (("-4", "4"), ("0", "0"), ("0", "-1")):
            rc, out, _, _ = run_main(["dump", address, length])
            self.assertEqual(rc, 1)
            self.assertIn("dump needs a non-negative address and a positive length", out)

    def test_dump_to_a_file_says_where(self):
        model = ArtixModel()
        model.memory[0x2000] = b"WXYZ"
        path = os.path.join(self.dir.name, "out.bin")
        rc, out, _, _ = run_main(["dump", "0x2000", "3", "--output", path], model)
        self.assertEqual(rc, 0)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"WXY")
        self.assertIn(f"wrote 3 bytes to {path}", out)

    def test_script_entry_point_exits_with_the_command_status(self):
        class PyFtdi(FakeFtdi):
            def __init__(self):
                super().__init__(ArtixModel())

            def open_mpsse_from_url(self, url, **kwargs):
                self.url = url

        package, module = types.ModuleType("pyftdi"), types.ModuleType("pyftdi.ftdi")
        module.Ftdi, package.ftdi = PyFtdi, module
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"pyftdi": package, "pyftdi.ftdi": module}), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys, "argv", ["u64ii_jtag.py", "probe"]), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            runpy.run_path(jt.__file__, run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("an Ultimate design is running", out.getvalue())


# ---------------------------------------------------------------------------
# The same tap, chain and flow tests through a USB-Blaster
# ---------------------------------------------------------------------------
class OnBlaster:
    def setUp(self):
        global CABLE
        self.cable, CABLE = CABLE, "blaster"

    def tearDown(self):
        global CABLE
        CABLE = self.cable


class BlasterTapTest(OnBlaster, TapTest):
    def test_release_leaves_pins_inputs(self):
        b, _, ftdi = board()
        b.close()
        self.assertEqual(ftdi.pins, 0)          # outputs disabled
        self.assertTrue(ftdi.closed)


class BlasterUserChainTest(OnBlaster, UserChainTest):
    pass


class BlasterFlowTest(OnBlaster, FlowTest):
    pass


class BlasterTest(unittest.TestCase):
    def test_release_on_a_dead_link_keeps_the_original_error(self):
        # As Mpsse.release(): a cable that is gone must not replace the error
        # that ended the session, and the port must still be closed.
        b, _, ftdi = board_on_blaster()
        b.identify()
        with self.assertRaises(jt.JtagError):
            with b:
                ftdi.fail_in_reset = True
                ftdi.model.chain.write_vector = jt.OUTPUT_CPU_RESET
                raise jt.JtagError("the session failed")
        self.assertTrue(ftdi.closed)

    def test_packets_fit_one_usb_packet(self):
        b, model, ftdi = board_on_blaster()
        b.identify()
        b.run_application(bytes(range(256)) * 40)
        self.assertLessEqual(max(ftdi.packets), jt.BL_PACKET)

    def open_with(self, attached, *urls):
        """Open each URL with these (serial, bus, address) Blasters attached."""
        opened = []
        saved = jt.Blaster.open, jt.Mpsse.open, jt.find_blasters
        try:
            jt.Blaster.open = classmethod(lambda cls, url, f: opened.append(("blaster", url)))
            jt.Mpsse.open = classmethod(lambda cls, url, f: opened.append(("ft232h", url)))
            jt.find_blasters = lambda: list(attached)
            for url in urls:
                jt.open_cable(url, 3e6)
        finally:
            jt.Blaster.open, jt.Mpsse.open, jt.find_blasters = saved
        return opened

    def test_url_selects_the_cable(self):
        opened = self.open_with([("8aB75VK4", 1, 5)], "blaster",
                                "ftdi://altera:usbblaster/2", "ftdi://ftdi:232h/1")
        self.assertEqual(opened, [("blaster", "ftdi://altera:usbblaster:8aB75VK4/1"),
                                  ("blaster", "ftdi://altera:usbblaster/2"),
                                  ("ft232h", "ftdi://ftdi:232h/1")])

    def test_plain_blaster_refuses_when_several_are_attached(self):
        with self.assertRaises(jt.JtagError) as ctx:
            self.open_with([("A1", 1, 4), ("B2", 1, 7)], "blaster")
        self.assertIn("blaster:A1, blaster:B2", str(ctx.exception))

    def test_plain_blaster_refuses_when_none_is_attached(self):
        with self.assertRaises(jt.JtagError):
            self.open_with([], "blaster")

    def test_serial_picks_one_of_several(self):
        opened = self.open_with([("A1", 1, 4), ("B2", 1, 7)], "blaster:B2")
        self.assertEqual(opened, [("blaster", "ftdi://altera:usbblaster:B2/1")])

    def test_unknown_serial_lists_the_attached_ones(self):
        with self.assertRaises(jt.JtagError) as ctx:
            self.open_with([("A1", 1, 4)], "blaster:B2")
        self.assertIn("blaster:A1", str(ctx.exception))

    def test_clashing_serials_need_bus_and_address(self):
        with self.assertRaises(jt.JtagError) as ctx:
            self.open_with([("SAME", 1, 4), ("SAME", 2, 26)], "blaster:SAME")
        # pyftdi reads bus and address as hex
        self.assertIn("blaster:1:4, blaster:2:1a", str(ctx.exception))
        opened = self.open_with([("SAME", 1, 4), ("SAME", 2, 26)], "blaster:2:1a")
        self.assertEqual(opened, [("blaster", "ftdi://altera:usbblaster:2:1a/1")])

    def test_bypass_hint_names_frequency_only_for_the_ft232h(self):
        for cable in ("ft232h", "blaster"):
            model = ArtixModel()
            b = (jt.Board(mpsse=jt.Blaster(FakeBlaster(model))) if cable == "blaster"
                 else jt.Board(mpsse=jt.Mpsse(FakeFtdi(model))))
            b.tap.bypass_delay = lambda: 0
            with self.assertRaises(jt.JtagError) as ctx:
                b.identify()
            self.assertEqual("--frequency" in str(ctx.exception), cable == "ft232h")


try:
    from pyftdi.ftdi import Ftdi as PyFtdiConstants
except ImportError:                     # the host tests themselves need no pyftdi
    PyFtdiConstants = None


@unittest.skipIf(PyFtdiConstants is None, "pyftdi is not installed")
class PyftdiOpcodeTest(unittest.TestCase):
    """The MPSSE opcodes against pyftdi's own table, an independent source.

    FakeFtdi decodes what this file's authors believe the opcodes mean, so a
    wrong opcode could pass every other test. pyftdi names each opcode by its
    clock edges: data out on the falling edge (NVE) and TDO in on the rising
    edge (PVE), as the FT232H code expects.
    """

    EXPECTED = {
        "OP_W_BYTES_LSB": "WRITE_BYTES_NVE_LSB",
        "OP_W_BITS_LSB": "WRITE_BITS_NVE_LSB",
        "OP_RW_BYTES_LSB": "RW_BYTES_PVE_NVE_LSB",
        "OP_RW_BITS_LSB": "RW_BITS_PVE_NVE_LSB",
        "OP_W_BYTES_MSB": "WRITE_BYTES_NVE_MSB",
        "OP_W_BITS_MSB": "WRITE_BITS_NVE_MSB",
        "OP_W_TMS": "WRITE_BITS_TMS_NVE",
        "OP_RW_TMS": "RW_BITS_TMS_PVE_NVE",
        "OP_SET_LOW": "SET_BITS_LOW",
        "OP_LOOPBACK_OFF": "LOOPBACK_END",
        "OP_SEND_IMMEDIATE": "SEND_IMMEDIATE",
        "OP_CLOCK_BITS": "CLK_BITS_NO_DATA",
        "OP_CLOCK_BYTES": "CLK_BYTES_NO_DATA",
    }

    def test_every_opcode_is_the_command_pyftdi_names(self):
        ours = {name for name in dir(jt) if name.startswith("OP_")}
        self.assertEqual(ours, set(self.EXPECTED), "an opcode without a pyftdi counterpart")
        for ours_name, pyftdi_name in self.EXPECTED.items():
            with self.subTest(opcode=ours_name):
                self.assertEqual(getattr(jt, ours_name), getattr(PyFtdiConstants, pyftdi_name))


class BlasterFtdi:
    """pyftdi's Ftdi as Blaster.open uses it: custom ids, open, latency, purge."""

    registered = []
    fail_open = None            # an exception for open_from_url to raise
    short = False               # write_data reports one byte fewer
    reply = b""

    def __init__(self):
        self.writes, self.calls, self.closed = [], [], False

    @classmethod
    def add_custom_vendor(cls, vid, name):
        if ("vendor", vid) in cls.registered:
            raise ValueError("already registered")
        cls.registered.append(("vendor", vid))

    @classmethod
    def add_custom_product(cls, vid, pid, name):
        if ("product", vid, pid) in cls.registered:
            raise ValueError("already registered")
        cls.registered.append(("product", vid, pid))

    def open_from_url(self, url):
        self.calls.append(("open", url))
        if self.fail_open:
            raise self.fail_open

    def set_latency_timer(self, value):
        self.calls.append(("latency", value))

    def purge_buffers(self):
        self.calls.append(("purge",))

    def write_data(self, data):
        self.writes.append(bytes(data))
        return len(data) - 1 if self.short else len(data)

    def read_data_bytes(self, size, attempt=1):
        return self.reply[:size]

    def close(self, freeze=False):
        self.closed = True


def fake_pyftdi(ftdi_class=None, usbtools=None):
    """sys.modules entries for a pyftdi package with the given parts."""
    package = types.ModuleType("pyftdi")
    entries = {"pyftdi": package}
    if ftdi_class is not None:
        module = types.ModuleType("pyftdi.ftdi")
        module.Ftdi, package.ftdi = ftdi_class, module
        entries["pyftdi.ftdi"] = module
    if usbtools is not None:
        module = types.ModuleType("pyftdi.usbtools")
        module.UsbTools, package.usbtools = usbtools, module
        entries["pyftdi.usbtools"] = module
    return entries


class BlasterOpenTest(unittest.TestCase):
    def setUp(self):
        BlasterFtdi.registered = []
        self.made = []
        made = self.made

        class Ftdi(BlasterFtdi):
            def __init__(self):
                super().__init__()
                made.append(self)

        self.Ftdi = Ftdi
        nap = mock.patch.object(jt.time, "sleep")
        nap.start()
        self.addCleanup(nap.stop)

    def open(self, url=jt.BLASTER_URL):
        with mock.patch.dict(sys.modules, fake_pyftdi(self.Ftdi)):
            return jt.Blaster.open(url)

    def test_open_without_pyftdi_says_how_to_get_it(self):
        with mock.patch.dict(sys.modules, {"pyftdi": None, "pyftdi.ftdi": None}), \
                self.assertRaisesRegex(jt.JtagError, "pyftdi is missing.*u64ii_jtag.sh"):
            jt.Blaster.open(jt.BLASTER_URL)

    def test_open_registers_the_altera_ids_once(self):
        self.open()
        self.open()                                     # a second open finds them known
        self.assertEqual(BlasterFtdi.registered,
                         [("vendor", 0x09FB), ("product", 0x09FB, 0x6001)])

    def test_open_resets_the_tap_and_leaves_tms_high(self):
        b = self.open("ftdi://altera:usbblaster:ABC/1")
        ftdi = self.made[-1]
        self.assertEqual(ftdi.calls, [("open", "ftdi://altera:usbblaster:ABC/1"),
                                      ("latency", 2), ("purge",), ("purge",)])
        # More than one shift-mode payload of TMS-high clocks, in USB packets.
        burst = b"".join(ftdi.writes[:-1])
        self.assertEqual(burst, bytes((jt.BL_IDLE | jt.BL_TMS,
                                       jt.BL_IDLE | jt.BL_TMS | jt.BL_TCK)) * 128)
        self.assertGreater(len(burst), 2 * jt.BL_SHIFT_MAX)
        self.assertTrue(all(len(w) <= jt.BL_PACKET for w in ftdi.writes))
        self.assertEqual(ftdi.writes[-1], bytes((jt.BL_IDLE | jt.BL_TMS,)))
        self.assertEqual(b.tms_level, jt.BL_TMS)

    def test_open_failure_names_a_program_that_may_hold_the_cable(self):
        self.Ftdi.fail_open = OSError("Access denied")
        with self.assertRaisesRegex(jt.JtagError, r"cannot open the USB-Blaster at "
                                                  r"ftdi://altera:usbblaster/1: Access denied.*jtagd"):
            self.open()

    def test_short_write_is_an_error(self):
        b = jt.Blaster(BlasterFtdi())
        b.ftdi.short = True
        with self.assertRaisesRegex(jt.JtagError, "short write"):
            b.tms(0b011111, 6)
            b.flush()

    def test_short_read_is_an_error(self):
        b = jt.Blaster(BlasterFtdi())
        b.tms_level = 0
        b.bytes_out(b"\x12\x34", read=True)
        with self.assertRaisesRegex(jt.JtagError, "returned 0 of 2 bytes"):
            b.flush()

    def test_byte_shift_with_tms_high_is_refused(self):
        b = jt.Blaster(BlasterFtdi())                   # TMS starts high
        with self.assertRaisesRegex(jt.JtagError, "byte shift with TMS high"):
            b.bytes_out(b"\x00")


class FindBlastersTest(unittest.TestCase):
    def test_without_pyftdi_says_how_to_get_it(self):
        with mock.patch.dict(sys.modules, {"pyftdi": None, "pyftdi.usbtools": None}), \
                self.assertRaisesRegex(jt.JtagError, "pyftdi is missing.*u64ii_jtag.sh"):
            jt.find_blasters()

    def test_lists_serial_bus_and_address_in_bus_order(self):
        asked = []

        class UsbTools:
            @staticmethod
            def find_all(ids):
                asked.append(ids)
                desc = types.SimpleNamespace
                return [(desc(sn="B", bus=2, address=1), 1),
                        (desc(sn=None, bus=1, address=7), 1),
                        (desc(sn="A", bus=1, address=3), 1)]

        with mock.patch.dict(sys.modules, fake_pyftdi(usbtools=UsbTools)):
            found = jt.find_blasters()
        self.assertEqual(asked, [[(0x09FB, 0x6001)]])
        self.assertEqual(found, [("A", 1, 3), (None, 1, 7), ("B", 2, 1)])

    def test_single_clone_without_serial_is_opened_by_bus_and_address(self):
        with mock.patch.object(jt, "find_blasters", return_value=[(None, 1, 0x1c)]):
            self.assertEqual(jt.blaster_url("blaster"), "ftdi://altera:usbblaster:1:1c/1")


def board_on_blaster():
    model = ArtixModel()
    ftdi = FakeBlaster(model)
    return jt.Board(mpsse=jt.Blaster(ftdi)), model, ftdi


if __name__ == "__main__":
    unittest.main()
