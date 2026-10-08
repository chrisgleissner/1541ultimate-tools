#!/usr/bin/env python3
"""Host tests for the parts of flash_u64.py that need no device or toolchain:
the .u64 header, the answer table, symbol lookup, the code check, and the
parsing of gdb/MI values.

    python3 tooling/test_flash_u64.py
"""

import os
import struct
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
