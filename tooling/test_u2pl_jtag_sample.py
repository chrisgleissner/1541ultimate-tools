"""Clock the real sampler through a mocked FT232H and an eight-bit ECP5 TAP."""

import contextlib
import io
import runpy
import sys
import unittest
from unittest.mock import patch

import u2pl_jtag_sample as tool
from u64ii_jtag import Mpsse
from test_u64ii_jtag import (FakeFtdi, NEXT, TLR, RTI, CAP_IR, SH_IR, UPD_IR,
                             CAP_DR, SH_DR)


class Ecp5Model:
    def __init__(self, released=True, idcode=tool.EXPECTED_IDCODE):
        self.state = TLR
        self.ir = tool.IR_IDCODE
        self.ir_shift = self.dr = 0
        self.dr_len = 32
        self.idcode = idcode
        # Exercise every byte and the final bit, not just the reset cell.
        self.boundary = sum(1 << bit for bit in range(0, tool.BOUNDARY_BITS, 3))
        if released:
            self.boundary |= 1 << tool.SLOT_RSTN_CELL
        self.instructions = []
        self.refreshes = 0
        self.presented = 1

    def rising(self, tms, tdi):
        sampled = self.presented
        state = self.state
        if state == CAP_IR:
            self.ir_shift = 1
        elif state == SH_IR:
            self.ir_shift = (tdi << 7) | (self.ir_shift >> 1)
        elif state == UPD_IR:
            self.ir = self.ir_shift & 0xFF
            self.instructions.append(self.ir)
            if self.ir == tool.IR_REFRESH:
                self.refreshes += 1
        elif state == CAP_DR:
            self.dr, self.dr_len = ((self.boundary, tool.BOUNDARY_BITS)
                                   if self.ir == tool.IR_SAMPLE else (self.idcode, 32))
        elif state == SH_DR:
            self.dr = (tdi << (self.dr_len - 1)) | (self.dr >> 1)
        self.state = NEXT[state][tms]
        if self.state == TLR:
            self.ir = tool.IR_IDCODE
        self.presented = (self.ir_shift & 1 if self.state == SH_IR else
                          self.dr & 1 if self.state == SH_DR else 1)
        return sampled


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.model = Ecp5Model()
        self.ftdi = FakeFtdi(self.model)
        self.cable = Mpsse(self.ftdi)

    def test_full_boundary_scan_is_exact_and_returns_to_idcode(self):
        tap = tool.Ecp5Tap(self.cable)
        self.assertEqual(tap.idcode(), tool.EXPECTED_IDCODE)
        value, boundary = tap.sample_reset()
        self.assertEqual(boundary, self.model.boundary)
        self.assertEqual(value, 1)
        self.assertEqual(self.model.state, RTI)
        self.assertEqual(self.model.instructions, [tool.IR_SAMPLE, tool.IR_IDCODE])

    def test_entry_point_uses_the_mocked_cable(self):
        with patch.object(tool.Mpsse, "open", return_value=self.cable), \
             patch("u64ii_jtag.DeviceLock", return_value=contextlib.nullcontext()), \
             patch.object(sys, "argv", ["u2pl_jtag_sample.py"]), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as status:
            runpy.run_path(tool.__file__, run_name="__main__")
        self.assertEqual(status.exception.code, 0)
        self.assertTrue(self.ftdi.closed)

    def main(self, *args):
        with patch.object(tool.Mpsse, "open", return_value=self.cable), \
             patch.object(tool, "DeviceLock", return_value=contextlib.nullcontext()), \
             patch.object(tool.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()) as output:
            status = tool.main(list(args))
        return status, output.getvalue(), sleep

    def test_normal_sample_never_refreshes_or_drives_boundary_pins(self):
        status, output, sleep = self.main()
        self.assertEqual(status, 0)
        self.assertIn("SLOT_RSTn=1", output)
        sleep.assert_not_called()
        self.assertEqual(self.model.refreshes, 0)
        self.assertEqual(self.model.instructions, [tool.IR_SAMPLE, tool.IR_IDCODE])
        self.assertTrue(self.ftdi.closed)
        self.assertEqual(self.ftdi.pins, (0, 0))

    def test_held_reset_returns_one(self):
        self.model.boundary &= ~(1 << tool.SLOT_RSTN_CELL)
        status, output, _ = self.main("--label", "candidate")
        self.assertEqual(status, 1)
        self.assertIn("candidate:", output)
        self.assertIn("held in reset", output)

    def test_refresh_is_explicit_and_waits_before_sampling(self):
        status, _, sleep = self.main("--refresh")
        self.assertEqual(status, 0)
        self.assertEqual(self.model.refreshes, 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.1, 12])
        self.assertEqual(self.model.instructions, [tool.IR_REFRESH, tool.IR_SAMPLE, tool.IR_IDCODE])

    def test_wrong_target_never_gets_ecp5_instructions(self):
        self.model.idcode = 0x1362C093
        with contextlib.redirect_stderr(io.StringIO()):
            status, _, _ = self.main("--refresh")
        self.assertEqual(status, 2)
        self.assertEqual(self.model.instructions, [])
        self.assertTrue(self.ftdi.closed)

    def test_short_usb_read_restores_idcode_and_releases_adapter(self):
        original = self.ftdi.read_data_bytes
        def fail_sample(size, attempt=1):
            return b"" if self.model.ir == tool.IR_SAMPLE else original(size, attempt)
        with patch.object(self.ftdi, "read_data_bytes", side_effect=fail_sample), \
             contextlib.redirect_stderr(io.StringIO()):
            status, _, _ = self.main()
        self.assertEqual(status, 2)
        self.assertEqual(self.model.instructions[-1], tool.IR_IDCODE)
        self.assertTrue(self.ftdi.closed)

    def test_open_failure_returns_two(self):
        with patch.object(tool.Mpsse, "open", side_effect=OSError("disconnected")), \
             patch.object(tool, "DeviceLock", return_value=contextlib.nullcontext()), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main([]), 2)

    def test_bad_options_do_not_open_usb(self):
        for args in (("--wait", "-1"), ("--frequency", "0"), ("--wait", "nan")):
            with self.subTest(args=args), patch.object(tool.Mpsse, "open") as open_cable, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tool.main(list(args))
            open_cable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
