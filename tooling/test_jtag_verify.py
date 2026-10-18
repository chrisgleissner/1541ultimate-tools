#!/usr/bin/env python3
"""Verifier safety, independent TAP oracles, faults, discovery and CLI tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jtag_verify as v
# Reuse the repository's transport interpreters; this module does not invoke
# their test cases. Import-time lock override is restored immediately.
_lock = os.environ.get("U64II_JTAG_LOCK")
import test_u64ii_jtag as sim
if _lock is None:
    os.environ.pop("U64II_JTAG_LOCK", None)
else:
    os.environ["U64II_JTAG_LOCK"] = _lock


class TapModel:
    """Independent edge-level single-device oracle for both instruction widths."""
    def __init__(self, profile):
        self.width, self.idir, codes = v.PROFILES[profile]
        self.code = codes[0] | 0x10000000
        self.state = sim.TLR
        self.ir = self.idir
        self.presented = 1
        self.shift = 0
        self.dr = 0
        self.dr_length = 32
        self.instructions = []
        self.edges = 0
        self.bad_capture = False

    def rising(self, tms, tdi):
        sampled = self.presented
        self.edges += 1
        if self.state == sim.CAP_IR:
            self.shift = 0 if self.bad_capture else 1
        elif self.state == sim.SH_IR:
            self.shift = (self.shift >> 1) | (tdi << (self.width - 1))
        elif self.state == sim.UPD_IR:
            self.ir = self.shift
            self.instructions.append(self.ir)
            assert self.ir in (self.idir, (1 << self.width) - 1), self.ir
        elif self.state == sim.CAP_DR:
            self.dr = self.code if self.ir == self.idir else 0
            self.dr_length = 32 if self.ir == self.idir else 1
        elif self.state == sim.SH_DR:
            self.dr = (self.dr >> 1) | (tdi << (self.dr_length - 1))
        self.state = sim.NEXT[self.state][tms]
        if self.state == sim.TLR:
            self.ir = self.idir
        self.presented = ((self.shift if self.state == sim.SH_IR else self.dr) & 1
                          if self.state in (sim.SH_IR, sim.SH_DR) else 1)
        return sampled


def adapter(kind="blaster", address=3):
    vid, pid = (0x9fb, 0x6001) if kind == "blaster" else (0x403, 0x6014)
    return v.Adapter(f"{vid:04x}:{pid:04x}@1-{address}", kind, vid, pid, 1, address,
                     [address], "vendor", "product", "duplicate", "full", [])


def transport(profile="c64u", kind="ft232h"):
    model = TapModel(profile)
    raw = sim.FakeBlaster(model) if kind == "blaster" else sim.FakeFtdi(model)
    raw.set_frequency = Mock(side_effect=lambda x: x)
    if kind == "ft232h":
        original_write = raw.write_data
        def write(data):
            if data == b"\xaa\x87":
                raw.out.extend(b"\xfa\xaa")
                return 2
            return original_write(data)
        raw.write_data = write
    cable = v.jt.Blaster(raw) if kind == "blaster" else v.jt.Mpsse(raw)
    return v.SafeTap(cable, profile), model, raw, cable


class SafetyTest(unittest.TestCase):
    def test_all_prohibited_instructions_rejected_before_transmission(self):
        for profile in v.PROFILES:
            tap, model, _, _ = transport(profile)
            with self.assertRaises(v.PolicyError):
                tap.ir(0)
            tap.identify()
            edges = model.edges
            for instruction in range(1 << tap.width):
                if instruction in (tap.id_instruction, (1 << tap.width) - 1):
                    continue
                with self.assertRaises(v.PolicyError):
                    tap.ir(instruction)
            self.assertEqual(edges, model.edges)
            with self.assertRaises(v.PolicyError):
                tap.dr_bitstream(b"\xff")
            self.assertEqual(edges, model.edges)
            self.assertFalse(hasattr(tap, "m"))
            self.assertFalse(hasattr(tap, "_shift"))

    def test_invalid_scans_idle_and_unconfirmed_are_rejected(self):
        tap, model, _, _ = transport()
        with self.assertRaises(v.PolicyError):
            tap.dr(0, 32, True)
        tap.reset()
        for tdi, size, read in [(0, 96, True), (0, 0, True), (0, 65538, True),
                                (-1, 32, True), (1 << 32, 32, True),
                                (0, 32, False), (0, 12, True), (0, 1.5, True)]:
            edges = model.edges
            with self.assertRaises(v.PolicyError):
                tap.dr(tdi, size, read)
            self.assertEqual(model.edges, edges)
        with self.assertRaises(v.PolicyError):
            tap.idle(1)
        tap.identify()
        for count in [-1, 65, 1.5]:
            with self.assertRaises(v.PolicyError):
                tap.idle(count)
        with self.assertRaises(v.PolicyError):
            v.SafeTap(Mock(), "unknown")

    def test_wrong_identity_never_loads_an_instruction(self):
        tap, model, _, _ = transport()
        model.code = 0x41111043
        with self.assertRaisesRegex(v.PolicyError, "unexpected target"):
            tap.identify()
        self.assertEqual(model.instructions, [])
        tap, model, _, _ = transport()
        with patch.object(v.jt.Tap, "dr", return_value=model.code):
            with self.assertRaises(v.PolicyError):
                tap.identify()  # bad chain tail
        self.assertEqual(model.instructions, [])

    def test_transport_errors_disable_all_further_clocks(self):
        for operation in ("reset", "ir", "dr", "idle"):
            tap, _, _, _ = transport()
            tap.identify()
            inner = tap._SafeTap__tap
            with patch.object(inner.m, "flush", side_effect=OSError("partial write")):
                with self.assertRaises(OSError):
                    getattr(tap, operation)(*dict(reset=(), ir=(tap.id_instruction,),
                                                dr=(0, 32, True), idle=(1,))[operation])
            self.assertFalse(tap.healthy)
            with self.assertRaises(v.PolicyError):
                tap.reset()
            with self.assertRaises(v.PolicyError):
                tap.ir(tap.id_instruction)
            with self.assertRaises(v.PolicyError):
                tap.dr(0, 32, True)
            with self.assertRaises(v.PolicyError):
                tap.idle(1)


class ScanTest(unittest.TestCase):
    def test_real_transport_encoding_for_both_profiles_and_adapters(self):
        for profile in v.PROFILES:
            for kind in ("blaster", "ft232h"):
                tap, model, raw, _ = transport(profile, kind)
                outcome = v.exercise(tap, 1, 1541, float("inf"))
                self.assertEqual(outcome["status"], "PASS")
                self.assertEqual(outcome["bypass_scans"], len(v.LENGTHS) * 3)
                self.assertEqual(model.ir, tap.id_instruction)
                if kind == "blaster":
                    self.assertLessEqual(max(raw.packets), 64)

    def test_deterministic_readback_and_fault_reporting(self):
        outcomes = [v.exercise(transport()[0], 1, 14, float("inf")) for _ in range(2)]
        self.assertEqual(outcomes[0]["readback_sha256"], outcomes[1]["readback_sha256"])
        tap, _, _, _ = transport()
        with self.assertRaisesRegex(v.jt.JtagError, "deadline"):
            v.exercise(tap, 1, 0, 0)
        tap, model, _, _ = transport()
        model.bad_capture = True
        with self.assertRaisesRegex(v.jt.JtagError, "IR capture"):
            v.exercise(tap, 1, 0, float("inf"))
        tap, _, _, _ = transport()
        original = tap.dr
        def corrupt(tdi, nbits, read):
            return original(tdi, nbits, read) ^ (1 if nbits == 1 else 0)
        with patch.object(tap, "dr", side_effect=corrupt):
            with self.assertRaisesRegex(v.jt.JtagError, "BYPASS mismatch"):
                v.exercise(tap, 1, 0, float("inf"))
        tap, _, _, _ = transport()
        original = tap.dr
        with patch.object(tap, "dr", side_effect=lambda d, n, r: original(d, n, r) ^ (n == 32)):
            with self.assertRaisesRegex(v.jt.JtagError, "IDCODE changed"):
                v.exercise(tap, 1, 0, float("inf"))


class DiscoveryTest(unittest.TestCase):
    def test_locations_not_serials_select_devices_and_metadata_errors_survive(self):
        def dev(address, ports, vid=0x9fb, pid=0x6001):
            return types.SimpleNamespace(idVendor=vid, idProduct=pid, bus=2,
                address=address, port_numbers=ports, iManufacturer=1, iProduct=2,
                iSerialNumber=3, speed=2)
        import usb.core
        import usb.util
        devices = [dev(9, [4]), dev(2, [1]), dev(7, [], 0x403, 0x6014), dev(8, [], 1, 2)]
        with patch.object(usb.core, "find", return_value=devices), \
                patch.object(usb.util, "get_string", return_value="same"):
            found = v.discover()
        self.assertEqual(len(found), 3)
        self.assertEqual(len({a.identifier for a in found}), 3)
        self.assertEqual(found[-1].identifier, "09fb:6001@2-4")
        self.assertIn(":2:9/1", found[-1].url)
        devices[0].iProduct = 0
        devices[0].speed = 99
        with patch.object(usb.core, "find", return_value=devices), \
                patch.object(usb.util, "get_string", side_effect=OSError("denied")):
            found = v.discover({(1, 2): "blaster"})
        self.assertEqual(len(found), 4)
        self.assertIsNone(found[-1].product)
        self.assertEqual(found[-1].speed, "unknown")
        self.assertTrue(found[-1].descriptor_errors)


class OpenTest(unittest.TestCase):
    def test_safe_blaster_open_never_uses_recovery_or_clocks(self):
        raw = Mock()
        raw.write_data.return_value = 1
        with patch("pyftdi.ftdi.Ftdi", return_value=raw):
            with v.safe_cable(adapter(), 1e6) as cable:
                self.assertIsInstance(cable, v.jt.Blaster)
                raw.write_data.assert_not_called()
        raw.write_data.assert_called_once_with(b"\x00")  # output disable only
        raw.close.assert_called_once_with()
        raw = Mock()
        raw.open_from_url.side_effect = OSError("busy")
        with patch("pyftdi.ftdi.Ftdi", return_value=raw):
            with self.assertRaises(OSError):
                with v.safe_cable(adapter(), 1e6):
                    self.fail()
        raw.close.assert_called_once()
        raw = Mock()
        with patch("pyftdi.ftdi.Ftdi", return_value=raw):
            with self.assertRaises(OSError):
                with v.safe_cable(adapter(), 1e6):
                    raise OSError("partial")
        raw.write_data.assert_not_called()
        raw.close.assert_called_once_with(freeze=True)

    def test_ft232h_and_custom_registration(self):
        cable = Mock()
        with patch("pyftdi.ftdi.Ftdi.add_custom_vendor", side_effect=ValueError), \
                patch("pyftdi.ftdi.Ftdi.add_custom_product", side_effect=ValueError), \
                patch.object(v.jt.Mpsse, "open", return_value=cable):
            with v.safe_cable(adapter("ft232h"), 1e6):
                pass
        cable.release.assert_called_once()

    def test_failed_output_disable_is_reported(self):
        adapter = types.SimpleNamespace(kind="ft232h", vid=0x0403, pid=0x6014, url="test")
        cable = Mock()
        cable.release.return_value = False
        with patch("pyftdi.ftdi.Ftdi.add_custom_vendor"), patch("pyftdi.ftdi.Ftdi.add_custom_product"), \
                patch.object(v.jt.Mpsse, "open", return_value=cable):
            with self.assertRaisesRegex(RuntimeError, "output-disable"):
                with v.safe_cable(adapter, 100000):
                    pass

    def test_mpsse_failed_open_retains_pins_and_releases_interface(self):
        for step in ("open_mpsse_from_url", "write_data"):
            raw = Mock()
            getattr(raw, step).side_effect = OSError("initialization failed")
            with patch("pyftdi.ftdi.Ftdi", return_value=raw):
                with self.assertRaisesRegex(OSError, "initialization failed"):
                    v.jt.Mpsse.open("ftdi://x/1", 100000)
            raw.close.assert_called_once_with(freeze=True)

    def test_mpsse_sync_echo_and_failures_emit_no_clocks(self):
        raw = Mock()
        raw.write_data.return_value = 2
        raw.read_data_bytes.return_value = b"\xfa\xaa"
        v.mpsse_sync(types.SimpleNamespace(ftdi=raw))
        raw.write_data.assert_called_once_with(b"\xaa\x87")
        raw.read_data_bytes.return_value = b"\xfa\x00"
        with self.assertRaisesRegex(v.jt.JtagError, "echo mismatch"):
            v.mpsse_sync(types.SimpleNamespace(ftdi=raw))
        raw.write_data.return_value = 1
        with self.assertRaisesRegex(v.jt.JtagError, "short MPSSE"):
            v.mpsse_sync(types.SimpleNamespace(ftdi=raw))

    def test_target_locks(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"DEVICE_LOCK_DIR": directory}):
            with v.target_lock("u64", adapter()):
                with self.assertRaises(v.PolicyError):
                    with v.target_lock("u64", adapter()):
                        self.fail()
        with patch.dict(os.environ, {"U64II_JTAG_LOCK": "off"}):
            with self.assertRaises(v.PolicyError):
                with v.target_lock("c64u", adapter()):
                    self.fail()
        with patch.dict(os.environ, {"U64II_JTAG_LOCK": "on"}), \
                patch.object(v.jt, "DeviceLock") as lock:
            with v.target_lock("c64u", adapter()):
                pass
            lock.assert_called_once_with(wait=0)


class ReportTest(unittest.TestCase):
    def args(self, *argv):
        return v.parser().parse_args(list(argv))

    def test_missing_profile_never_opens_even_with_opt_in(self):
        with patch.object(v, "safe_cable") as op:
            for opts in ((), ("--allow-disruptive",), ("--list",)):
                report = v.verify(adapter(), None, self.args(*opts))
                self.assertEqual(report["debugging_suitability"], "UNVERIFIED")
                self.assertEqual(report["results"][1]["status"], "UNTESTABLE")
            op.assert_not_called()
        a = adapter()
        a.descriptor_errors = ["permission denied"]
        self.assertEqual(v.verify(a, None, self.args())["results"][0]["status"], "UNVERIFIED")

    def test_success_sweep_opt_in_still_cannot_enable_unsafe_calls(self):
        for profile, kind in (("u64", "blaster"), ("c64u", "ft232h")):
            _, _, _, cable = transport(profile, kind)
            with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                    patch.object(v, "safe_cable", return_value=contextlib.nullcontext(cable)), \
                    patch.object(v, "LENGTHS", (1, 8, 64)):
                report = v.verify(adapter(kind), profile, self.args("--cycles", "1", "--allow-disruptive"))
            self.assertEqual(report["debugging_suitability"], "TRANSPORT_PASSED_DEBUG_UNVERIFIED")
            if kind == "ft232h":
                self.assertEqual(cable.ftdi.set_frequency.call_count, 3)
            self.assertEqual(report["results"][-2]["status"], "UNSUPPORTED" if profile == "c64u" else "UNTESTABLE")

    def test_failure_and_no_reset_after_partial_transfer(self):
        _, _, _, cable = transport()
        with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                patch.object(v, "safe_cable", return_value=contextlib.nullcontext(cable)), \
                patch.object(cable, "flush", side_effect=OSError("disconnected")) as flush:
            report = v.verify(adapter(), "c64u", self.args())
        self.assertEqual(report["debugging_suitability"], "TRANSPORT_UNTESTABLE")
        self.assertEqual(flush.call_count, 1)

    def test_clock_failure_does_not_send_cleanup_clocks(self):
        _, model, raw, cable = transport()
        raw.set_frequency.side_effect = OSError("clock command interrupted")
        with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                patch.object(v, "safe_cable", return_value=contextlib.nullcontext(cable)):
            report = v.verify(adapter("ft232h"), "c64u", self.args())
        self.assertEqual(model.edges, 0)
        self.assertEqual(report["debugging_suitability"], "TRANSPORT_UNTESTABLE")

    def test_failed_debug_soak_keeps_evidence_and_distinct_verdict(self):
        import jtag_verify_debug as debug
        _, _, _, cable = transport("u64", "blaster")
        opts = self.args("--cycles", "1", "--allow-disruptive", "--debug-idle",
                         "--intel-root", "/intel", "--debug-health-host", "u64")
        failure = debug.DebugRunError("memory timeout", {"failed_cycle": 116, "steps": [{"pc_before": "0x2588"}]})
        with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                patch.object(v, "safe_cable", return_value=contextlib.nullcontext(cable)), \
                patch.object(v, "LENGTHS", (1, 8)), \
                patch.object(debug, "run", side_effect=failure):
            report = v.verify(adapter(), "u64", opts)
        self.assertEqual(report["debugging_suitability"], "DEBUG_SESSION_FAILED")
        result = report["results"][-2]
        self.assertEqual(result["failed_cycle"], 116)
        self.assertEqual(len(result["steps"]), 1)
        self.assertTrue(any(c["requirement"] == "TRANSPORT-SOAK" and c["status"] == "PASS"
                            for c in report["results"]))

    def test_debug_backend_requires_every_gate_and_never_runs_on_c64u(self):
        debug_result = dict(requirement="DEBUG-SESSION", safety="DISRUPTIVE", status="PASS", detail="checked")
        for kind, profile in (("blaster", "u64"), ("ft232h", "c64u"), ("ft232h", "u64")):
            _, _, _, cable = transport(profile, kind)
            opts = self.args("--cycles", "1", "--allow-disruptive", "--debug-idle",
                             "--intel-root", "/intel", "--debug-health-host", "u64")
            with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                    patch.object(v, "safe_cable", return_value=contextlib.nullcontext(cable)), \
                    patch.object(v, "LENGTHS", (1, 8)), \
                    patch("jtag_verify_debug.run", return_value=debug_result) as backend:
                report = v.verify(adapter(kind), profile, opts)
            if profile == "u64" and kind == "blaster":
                backend.assert_called_once()
                self.assertEqual(report["debugging_suitability"], "DEBUG_SESSION_PASSED")
            else:
                backend.assert_not_called()
                self.assertEqual(report["results"][-2]["status"], "UNSUPPORTED")
        opts = self.args("--allow-disruptive", "--debug-idle", "--intel-root", "/intel", "--debug-health-host", "u64")
        with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                patch.object(v, "safe_cable", side_effect=OSError("USB busy")), \
                patch("jtag_verify_debug.run") as backend:
            report = v.verify(adapter(), "u64", opts)
            self.assertEqual(report["results"][-2]["status"], "UNTESTABLE")
            backend.assert_not_called()
        _, _, _, cable = transport("u64", "blaster")
        with patch.object(v, "target_lock", return_value=contextlib.nullcontext()), \
                patch.object(v, "safe_cable", return_value=contextlib.nullcontext(cable)), \
                patch.object(v, "LENGTHS", (1,)), \
                patch("jtag_verify_debug.run", side_effect=RuntimeError("resume failed")):
            report = v.verify(adapter(), "u64", opts)
            self.assertEqual(report["results"][-2]["status"], "ERROR")

    def test_cli_selection_output_and_exit_status(self):
        a, b = adapter(), adapter("ft232h", 4)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(v, "discover", return_value=[a, b]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            path = Path(directory) / "report.json"
            self.assertEqual(v.main(["--list", "--json", str(path)]), 2)
            self.assertEqual(len(json.loads(path.read_text())["adapters"]), 2)
            self.assertEqual(v.main(["--type", "blaster", "--adapter", a.identifier,
                                     "--target", a.identifier + "=u64", "--list"]), 2)
            for argv in (["--debug-cycles", "0"], ["--cycles", "0"], ["--timeout", "nan"], ["--frequency", "inf"]):
                with self.assertRaises(SystemExit):
                    v.main(argv)
            for argv in (["--adapter", "missing"], ["--target", "bad"],
                         ["--target", a.identifier + "=bad"],
                         ["--target", a.identifier + "=u64", "--target", a.identifier + "=u64"],
                         ["--usb-id", "bad"], ["--usb-id", "1:2=bad"],
                         ["--usb-id", "09fb:6001=ft232h"]):
                self.assertEqual(v.main(argv), 2)
            self.assertEqual(v.main(["--usb-id", "1:2=blaster", "--list"]), 2)
            with patch.object(v, "verify", return_value={"results": [], "debugging_suitability": "TRANSPORT_PASSED_DEBUG_UNVERIFIED"}):
                self.assertEqual(v.main([]), 0)
            with patch.object(v, "verify", return_value={"results": [{"status": "FAIL", "requirement": "x", "detail": "x"}], "debugging_suitability": "TRANSPORT_FAILED"}):
                self.assertEqual(v.main([]), 1)
        with patch.object(v, "discover", return_value=[]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(v.main([]), 2)


if __name__ == "__main__":
    unittest.main()
