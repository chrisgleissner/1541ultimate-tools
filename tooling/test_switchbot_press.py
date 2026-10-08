#!/usr/bin/env python3
"""Host tests for switchbot_press.py against fake bleak and switchbot modules.

The fakes record the scan and the Bot commands and answer with the results a
test chooses, so no Bluetooth controller or pySwitchbot install is needed.

    python3 tooling/test_switchbot_press.py
"""

import contextlib
import io
import os
import runpy
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import switchbot_press as sp  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"


class FakeBluetooth:
    """BleakScanner.find_device_by_address and a Switchbot Bot."""

    def __init__(self):
        self.device = object()
        self.long_press_ack = True
        self.press_ack = True
        self.scans, self.bots, self.calls = [], [], []

        bluetooth = self

        class BleakScanner:
            @staticmethod
            async def find_device_by_address(mac, timeout):
                bluetooth.scans.append((mac, timeout))
                return bluetooth.device

        class Switchbot:
            def __init__(self, device):
                bluetooth.bots.append(device)

            async def set_long_press(self, hold):
                bluetooth.calls.append(("set_long_press", hold))
                return bluetooth.long_press_ack

            async def press(self):
                bluetooth.calls.append(("press",))
                return bluetooth.press_ack

        self.modules = {"bleak": types.ModuleType("bleak"), "switchbot": types.ModuleType("switchbot")}
        self.modules["bleak"].BleakScanner = BleakScanner
        self.modules["switchbot"].Switchbot = Switchbot


class Press(unittest.TestCase):
    def setUp(self):
        self.bt = FakeBluetooth()
        self.out, self.err = io.StringIO(), io.StringIO()
        self.enterContext(mock.patch.dict(sys.modules, self.bt.modules))
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(self.err))

    def main(self, *argv):
        with mock.patch.object(sys, "argv", ["switchbot_press.py", *argv]):
            return sp.main()

    def test_press(self):
        self.assertEqual(self.main("--mac", MAC), 0)
        self.assertEqual(self.bt.scans, [(MAC, 20.0)])
        self.assertEqual(self.bt.bots, [self.bt.device])
        self.assertEqual(self.bt.calls, [("press",)])
        self.assertEqual(self.out.getvalue(), f"[switchbot] pressed {MAC}\n")

    def test_hold_is_set_before_the_press(self):
        self.assertEqual(self.main("--mac", MAC, "--hold", "5"), 0)
        self.assertEqual(self.bt.calls, [("set_long_press", 5), ("press",)])

    def test_hold_zero_is_set(self):
        # 0 is a real hold time, not "leave it as it is".
        self.assertEqual(self.main("--mac", MAC, "--hold", "0"), 0)
        self.assertEqual(self.bt.calls, [("set_long_press", 0), ("press",)])

    def test_not_found(self):
        self.bt.device = None
        self.assertEqual(self.main("--mac", MAC, "--timeout", "3.5"), 2)
        self.assertEqual(self.bt.scans, [(MAC, 3.5)])
        self.assertEqual(self.bt.calls, [])
        self.assertIn(f"{MAC} not found within 3.5 s", self.err.getvalue())

    def test_hold_not_acknowledged(self):
        self.bt.long_press_ack = False
        self.assertEqual(self.main("--mac", MAC, "--hold", "0"), 3)
        self.assertEqual(self.bt.calls, [("set_long_press", 0)])
        self.assertIn("set_long_press was not acknowledged", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_press_not_acknowledged(self):
        self.bt.press_ack = False
        self.assertEqual(self.main("--mac", MAC), 4)
        self.assertIn("press was not acknowledged", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_mac_is_required(self):
        with self.assertRaises(SystemExit) as exit:
            self.main()
        self.assertEqual(exit.exception.code, 2)
        self.assertIn("--mac", self.err.getvalue())
        self.assertEqual(self.bt.scans, [])

    def test_hold_must_be_whole_seconds(self):
        with self.assertRaises(SystemExit) as exit:
            self.main("--mac", MAC, "--hold", "0.5")
        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(self.bt.scans, [])

    def test_run_as_a_script(self):
        with mock.patch.object(sys, "argv", ["switchbot_press.py", "--mac", MAC]):
            with self.assertRaises(SystemExit) as exit:
                runpy.run_path(sp.__file__, run_name="__main__")
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(self.bt.calls, [("press",)])


if __name__ == "__main__":
    unittest.main()
