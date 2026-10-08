#!/usr/bin/env python3
"""Checks the tools' knowledge of the firmware against a 1541ultimate checkout.

The JTAG tool, the gdb server and the unwinder carry constants transcribed from
the firmware: user chain registers from jtag_client_xilinx.vhd, the boot magic
from the RISC-V bootloader, the instruction cache size, the bitstreams in
external/ and the recovery kit. These tests read the same facts from a real
checkout, so an upstream change that breaks the tools fails here instead of on
a board.

    ULTIMATE_REPO_DIR=/path/to/1541ultimate python3 -m unittest tests/upstream/test_contract.py

Without ULTIMATE_REPO_DIR every test is skipped; with UPSTREAM_REQUIRED=1 a
missing checkout is a failure instead.
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tooling"))
os.environ.setdefault("U64II_JTAG_LOCK", "off")
import u64ii_gdbstub as gs  # noqa: E402
import u64ii_jtag as jt  # noqa: E402

REPO = os.environ.get("ULTIMATE_REPO_DIR", "")
REQUIRED = os.environ.get("UPSTREAM_REQUIRED") == "1"


def setUpModule():
    if not REPO or not os.path.exists(os.path.join(REPO, ".git")):
        if REQUIRED:
            raise RuntimeError(f"ULTIMATE_REPO_DIR={REPO!r} is not a 1541ultimate checkout")
        raise unittest.SkipTest("ULTIMATE_REPO_DIR is not set")


def source(*parts):
    with open(os.path.join(REPO, *parts), encoding="latin-1") as handle:
        return handle.read()


def binary(*parts):
    with open(os.path.join(REPO, *parts), "rb") as handle:
        return handle.read()


def hex_constant(text, pattern):
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"pattern {pattern!r} not found")
    return int(match.group(1), 16)


class Bitstreams(unittest.TestCase):
    def test_each_supported_part_has_its_bitstream(self):
        for idcode, (part, name) in jt.SUPPORTED.items():
            with self.subTest(part=part):
                data = binary("external", name)
                self.assertEqual(jt.bitstream_idcode(data) & 0x0FFFFFFF, idcode,
                                 f"external/{name} is not built for the {part}")

    def test_recovery_kit_is_an_xc7a50t_image(self):
        # cmd_recover refuses anything but an XC7A50T for this reason.
        data = binary("recovery", "u64ii", "u64_mk2_artix.bit")
        self.assertEqual(jt.bitstream_idcode(data) & 0x0FFFFFFF, 0x0362C093)

    def test_recovery_kit_has_its_application(self):
        self.assertGreater(len(binary("recovery", "u64ii", "ultimate.bin")), 64 * 1024)


class UserChain(unittest.TestCase):
    """fpga/io/jtag/vhdl_source/jtag_client_xilinx.vhd"""

    @classmethod
    def setUpClass(cls):
        cls.vhdl = source("fpga", "io", "jtag", "vhdl_source", "jtag_client_xilinx.vhd")

    def tdo_register(self, number):
        """The shift register the TDO multiplexer selects for a register number."""
        match = re.search(rf'when X"{number:X}"\s*=>\s*jtdo\s*<=\s*(\w+)', self.vhdl)
        self.assertIsNotNone(match, f"register {number:#x} has no TDO source")
        return match.group(1)

    def test_identification_word(self):
        self.assertEqual(hex_constant(self.vhdl, r'c_rom\s*:[^:]*:=\s*X"([0-9A-Fa-f]{8})"'),
                         jt.USER_ID_VALUE)
        self.assertEqual(self.tdo_register(jt.USER_ID), "c_rom")

    def test_readable_registers(self):
        self.assertEqual(self.tdo_register(jt.USER_DEBUG), "shiftreg_debug")
        self.assertEqual(self.tdo_register(jt.USER_READ_FIFO), "shiftreg_fifo")
        self.assertEqual(self.tdo_register(jt.USER_CONSOLE), "shiftreg_console")

    def test_outputs_register_is_written_on_update(self):
        match = re.search(rf'when X"{jt.USER_OUTPUTS:X}"\s*=>\s*write_vector_i\s*<=\s*shiftreg_write',
                          self.vhdl)
        self.assertIsNotNone(match)

    def test_memory_command_and_data_registers(self):
        self.assertRegex(self.vhdl, rf'ir_in = X"{jt.USER_MEM_COMMAND:X}"')
        self.assertRegex(self.vhdl, rf'ir_in = X"{jt.USER_MEM_DATA:X}"')

    def test_read_fifo_is_popped_from_its_register(self):
        self.assertRegex(self.vhdl, rf'if ir_in = X"{jt.USER_READ_FIFO:X}" then')


class Bootloader(unittest.TestCase):
    """software/portable/riscv/bootloader_u64ii.c and its linker script"""

    @classmethod
    def setUpClass(cls):
        cls.c = source("software", "portable", "riscv", "bootloader_u64ii.c")

    def test_boot_magic(self):
        self.assertEqual(hex_constant(self.c, r"BOOT_MAGIC_VALUE\s+\(0x([0-9A-Fa-f]+)\)"),
                         jt.BOOT_MAGIC_VALUE)

    def test_jump_address_and_magic_locations(self):
        # The tool writes the jump address at BOOT_MAGIC_ADDRESS and the magic
        # in the word after it.
        jump = hex_constant(self.c, r"BOOT_MAGIC_JUMPADDR\s+\*\(\(volatile uint32_t\*\)0x([0-9A-Fa-f]+)\)")
        magic = hex_constant(self.c, r"BOOT_MAGIC_LOCATION\s+\*\(\(volatile uint32_t\*\)0x([0-9A-Fa-f]+)\)")
        self.assertEqual(jump, jt.BOOT_MAGIC_ADDRESS)
        self.assertEqual(magic, jt.BOOT_MAGIC_ADDRESS + 4)

    def test_bootloader_runs_from_the_boot_rom(self):
        linker = source("target", "u64ii", "riscv", "bootloader", "linker.x")
        self.assertEqual(hex_constant(linker, r"bootrom\s*:\s*ORIGIN\s*=\s*0x([0-9A-Fa-f]+)"),
                         jt.BOOTLOADER_ADDRESS)


class InstructionCache(unittest.TestCase):
    def test_cache_size(self):
        vhdl = source("fpga", "cpu_unit", "rvlite", "vhdl_source", "icache.vhd")
        bits = int(re.search(r"c_cache_size_bits\s*:\s*natural\s*:=\s*(\d+)", vhdl).group(1))
        self.assertEqual(1 << bits, jt.ICACHE_BYTES)


class TrapFrame(unittest.TestCase):
    """software/FreeRTOS/Source/portable/risc-v/port_asm.S, read by the gdb server."""

    @classmethod
    def setUpClass(cls):
        cls.asm = source("software", "FreeRTOS", "Source", "portable", "risc-v", "port_asm.S")

    def slot(self, register):
        match = re.search(rf"store_x\s+{register},\s*(\d+)\s*\*\s*portWORD_SIZE", self.asm)
        self.assertIsNotNone(match, f"{register} is not saved in the trap frame")
        return int(match.group(1))

    def test_context_size(self):
        words = int(re.search(r"#define\s+portCONTEXT_SIZE\s+\(\s*(\d+)\s*\*\s*portWORD_SIZE\s*\)",
                              self.asm).group(1))
        self.assertEqual(words, gs.FRAME_WORDS)

    def test_register_slots(self):
        self.assertEqual(self.slot("x1"), gs.FRAME_RA)
        self.assertEqual(self.slot("x5"), gs.FRAME_X5)

    def test_mstatus_slot(self):
        match = re.search(r"csrr\s+t0,\s*mstatus[^\n]*\n\s*store_x\s+t0,\s*(\d+)\s*\*\s*portWORD_SIZE",
                          self.asm)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), gs.FRAME_MSTATUS)

    def test_pc_slot(self):
        # The task's resume address goes to word 0 of the frame.
        self.assertRegex(self.asm, rf"store_x\s+\w+,\s*{gs.FRAME_PC}\s*\(\s*sp\s*\)|"
                                   rf"store_x\s+\w+,\s*{gs.FRAME_PC}\s*\*\s*portWORD_SIZE")


if __name__ == "__main__":
    unittest.main()
