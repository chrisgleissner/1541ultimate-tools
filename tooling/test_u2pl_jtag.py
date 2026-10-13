#!/usr/bin/env python3
import hashlib
import contextlib
import io
import subprocess
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import u2pl_jtag as tool


def pad_to_alignment(data: bytes) -> bytes:
    return data + b"\x00" * ((-len(data)) % tool.OBJECT_ALIGNMENT)


def make_application(payload: bytes = b"\x73\x70\x04\x30" + b"application") -> bytes:
    return (
        struct.pack("<III", tool.RUNTIME_LOAD_ADDRESS, len(payload), tool.RUNTIME_LOAD_ADDRESS)
        + payload
        + b"\x00" * ((-len(payload)) % 4)
    )


def make_u2l(
    *,
    duplicate_app: bool = False,
    corrupt_padding: bool = False,
    trailing_decoy: bool = False,
    app_payload: bytes | None = None,
) -> bytes:
    """Build a .u2l with the layout update_binaries.s produces.

    The updater links the bitstream and ultimate.app into .rodata with
    `.align 4`, which on RISC-V means 16-byte alignment, so both objects start
    on a 16-byte boundary and the bitstream's ECP5 trailer is followed by zero
    padding up to that boundary.
    """
    bitstream = (
        tool.ECP5_BIT_MARKER
        + b"Version: test\x00"
        + tool.ECP5_PART_MARKER
        + b"payload"
        + tool.ECP5_TRAILER
    )
    app = make_application() if app_payload is None else make_application(app_payload)

    payload = pad_to_alignment(b"prefix")
    payload += bitstream
    if corrupt_padding:
        payload += b"\x01"
    payload = pad_to_alignment(payload)
    payload += app
    if duplicate_app:
        # A second, structurally identical object after the first.
        payload = pad_to_alignment(payload)
        payload += tool.ECP5_TRAILER
        payload = pad_to_alignment(payload)
        payload += app
    if trailing_decoy:
        # A header-shaped word inside later executable data. It is not preceded
        # by an ECP5 trailer, so it must not be treated as a candidate.
        payload = pad_to_alignment(payload)
        payload += make_application(b"decoy payload data")
    outer_padding = b"\x00" * ((-len(payload)) % 4)
    return struct.pack("<III", tool.OUTER_LOAD_ADDRESS, len(payload), tool.OUTER_LOAD_ADDRESS) + payload + outer_padding


def make_flash_image(*, bitstream: bytes, application: bytes) -> bytearray:
    raw = bytearray(b"\xff" * tool.FLASH_SIZE)
    raw[tool.BOOT_FPGA_OFFSET : tool.BOOT_FPGA_OFFSET + len(bitstream)] = bitstream
    raw[tool.APPLICATION_OFFSET : tool.APPLICATION_OFFSET + len(application)] = application
    return raw


class ExtractionTests(unittest.TestCase):
    def write_firmware(self, content: bytes) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".u2l", delete=False)
        tmp.write(content)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return Path(tmp.name)

    def test_extracts_runtime_images(self) -> None:
        firmware = self.write_firmware(make_u2l())
        image = tool.extract_recovery_image(firmware)
        self.assertTrue(image.bitstream.startswith(tool.ECP5_BIT_MARKER))
        self.assertTrue(image.bitstream.endswith(tool.ECP5_TRAILER))
        self.assertEqual(image.application_record.load_address, tool.RUNTIME_LOAD_ADDRESS)
        self.assertEqual(image.application[12:16], b"\x73\x70\x04\x30")
        self.assertEqual(image.firmware_sha256, hashlib.sha256(firmware.read_bytes()).hexdigest())

    def test_rejects_ambiguous_embedded_application(self) -> None:
        firmware = self.write_firmware(make_u2l(duplicate_app=True))
        with self.assertRaisesRegex(tool.FirmwareFormatError, "found 2 plausible records"):
            tool.extract_recovery_image(firmware)

    def test_ignores_header_shaped_data_after_the_application(self) -> None:
        """A later header-shaped word must not make extraction fail closed.

        Only records that follow an ECP5 trailer and its zero padding are
        candidates, so executable data that happens to contain the three
        header words is skipped instead of counted as a second record.
        """
        firmware = self.write_firmware(make_u2l(trailing_decoy=True))
        image = tool.extract_recovery_image(firmware)
        self.assertEqual(image.application[12:16], b"\x73\x70\x04\x30")

    def test_rejects_non_zero_padding(self) -> None:
        firmware = self.write_firmware(make_u2l(corrupt_padding=True))
        with self.assertRaises(tool.FirmwareFormatError):
            tool.extract_recovery_image(firmware)

    def test_rejects_application_the_bootloader_would_not_start(self) -> None:
        """bootloader.c refuses lengths that are not below MAX_APPL_SIZE."""
        oversized = b"\x00" * tool.BOOTLOADER_MAX_APPL_SIZE
        firmware = self.write_firmware(make_u2l(app_payload=oversized))
        with self.assertRaisesRegex(tool.FirmwareFormatError, "plausible records"):
            tool.extract_recovery_image(firmware)

    def test_rejects_non_u2l_extension(self) -> None:
        original = self.write_firmware(make_u2l())
        firmware = original.with_suffix(".bin")
        original.rename(firmware)
        self.addCleanup(firmware.unlink, missing_ok=True)
        with self.assertRaisesRegex(tool.FirmwareFormatError, "expected a .u2l"):
            tool.extract_recovery_image(firmware)


class LayoutTests(unittest.TestCase):
    def test_erase_span_rounds_outwards_to_64k(self) -> None:
        self.assertEqual(tool.erase_span(0x0A0000, 1), (0x0A0000, 0x0B0000))
        self.assertEqual(tool.erase_span(0x0A0001, 0x10000), (0x0A0000, 0x0C0000))

    def test_layout_check_accepts_the_real_firmware_sizes(self) -> None:
        image = tool.RecoveryImage(
            firmware=Path("dummy.u2l"),
            firmware_sha256="",
            outer=tool.AppRecord(tool.OUTER_LOAD_ADDRESS, 1, tool.OUTER_LOAD_ADDRESS, 12),
            bitstream=b"\x00" * 589628,
            application=b"\x00" * 987520,
            application_record=tool.AppRecord(tool.RUNTIME_LOAD_ADDRESS, 987508, tool.RUNTIME_LOAD_ADDRESS, 12),
        )
        tool.check_layout(image)

    def test_layout_check_rejects_a_bitstream_that_would_erase_the_application(self) -> None:
        """A bitstream of 0xA0000 bytes fits exactly, but 0xA0001 does not.

        ecpprog rounds the erase up to the next 64 KiB block, so one byte past
        the region boundary destroys the first block of the application slot.
        """
        base = dict(
            firmware=Path("dummy.u2l"),
            firmware_sha256="",
            outer=tool.AppRecord(tool.OUTER_LOAD_ADDRESS, 1, tool.OUTER_LOAD_ADDRESS, 12),
            application=b"\x00" * 1024,
            application_record=tool.AppRecord(tool.RUNTIME_LOAD_ADDRESS, 1012, tool.RUNTIME_LOAD_ADDRESS, 12),
        )
        tool.check_layout(tool.RecoveryImage(bitstream=b"\x00" * tool.APPLICATION_OFFSET, **base))
        with self.assertRaisesRegex(tool.LayoutError, "ECP5 bitstream"):
            tool.check_layout(tool.RecoveryImage(bitstream=b"\x00" * (tool.APPLICATION_OFFSET + 1), **base))


class ProbeParsingTests(unittest.TestCase):
    TRANSCRIPT = (
        "init..\n"
        "IDCODE: 0x41111043 (LFE5U-25)\n"
        "ECP5 Status Register: 0x00200000\n"
        "reset..\n"
        "flash ID: 0xEF 0x40 0x17\n"
    )

    def test_accepts_the_documented_hardware(self) -> None:
        result = tool.parse_probe_output(self.TRANSCRIPT)
        self.assertEqual(result.idcode, tool.EXPECTED_IDCODE)
        self.assertEqual(result.jedec, (0xEF, 0x40, 0x17))
        self.assertEqual(result.flash_name, "Winbond W25Q64")

    def test_accepts_the_spansion_alternative(self) -> None:
        """s25fl_flash.cc is compiled into the U2+L application as well."""
        result = tool.parse_probe_output(self.TRANSCRIPT.replace("0xEF 0x40", "0x01 0x40"))
        self.assertEqual(result.flash_name, "Spansion/Cypress S25FL164K")

    def test_rejects_a_wrong_capacity(self) -> None:
        """The layout puts configuration at 0x7F0000, which only suits 8 MiB."""
        with self.assertRaisesRegex(RuntimeError, "capacity byte 0x18"):
            tool.parse_probe_output(self.TRANSCRIPT.replace("0x17", "0x18"))

    def test_rejects_a_wrong_idcode(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "0xffffffff"):
            tool.parse_probe_output("IDCODE: 0xffffffff does not match :(\nflash ID: 0xEF 0x40 0x17\n")

    def test_rejects_an_unknown_flash(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no U2\\+L flash"):
            tool.parse_probe_output(self.TRANSCRIPT.replace("0xEF 0x40", "0xC2 0x20"))


class CommandTests(unittest.TestCase):
    def commands_for(self, argv: list[str], *, drive: Path | None = None, config: Path | None = None):
        args = tool.parse_args(argv)
        return args, tool.planned_commands(
            args, Path("bit"), Path("app"), drive, config, Path("backup.bin")
        )

    def test_flash_is_explicit(self) -> None:
        self.assertFalse(tool.parse_args(["firmware.u2l"]).flash)
        self.assertTrue(tool.parse_args(["firmware.u2l", "--flash"]).flash)

    def test_optional_erases_require_flash(self) -> None:
        for flag in ("--erase-flash-drive", "--reset-configuration", "--restore-backup=x.bin"):
            with self.assertRaises(SystemExit):
                tool.parse_args(["firmware.u2l", flag])

    def test_modes_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            tool.parse_args(["firmware.u2l", "--flash", "--dump"])

    def test_probe_runs_first_and_backup_second(self) -> None:
        _, commands = self.commands_for(["firmware.u2l", "--flash"])
        self.assertEqual(commands[0][-1], "-t")
        self.assertEqual(commands[1][-5:], ["-R", hex(tool.FLASH_SIZE), "-o", "0", "backup.bin"])

    def test_refresh_is_only_on_the_last_write(self) -> None:
        """ecp_init_flash_mode erases the ECP5 SRAM at the start of every
        operation, so a refresh on any earlier command is immediately undone."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            args = tool.parse_args(
                ["firmware.u2l", "--flash", "--erase-flash-drive", "--reset-configuration"]
            )
            drive, config = tool.optional_erase_files(args, output)
            commands = tool.planned_commands(args, Path("bit"), Path("app"), drive, config, Path("backup.bin"))
        self.assertEqual([index for index, cmd in enumerate(commands) if "-a" in cmd], [len(commands) - 1])
        self.assertEqual(commands[-1][-3:], ["-o", hex(tool.CONFIG_OFFSET), str(config)])

    def test_refresh_flag_precedes_the_offset_argument(self) -> None:
        """-a must not land between -o and its value, where getopt would eat it."""
        _, commands = self.commands_for(["firmware.u2l", "--flash"])
        last = commands[-1]
        self.assertEqual(last[last.index("-a") + 1], "-o")

    def test_optional_erases_are_exact_u2l_ranges(self) -> None:
        args = tool.parse_args(
            ["firmware.u2l", "--flash", "--erase-flash-drive", "--reset-configuration"]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            drive, config = tool.optional_erase_files(args, output)
            self.assertEqual(drive.stat().st_size, tool.FLASH_DRIVE_SIZE)
            self.assertEqual(config.stat().st_size, tool.CONFIG_SIZE)
            self.assertEqual(config.read_bytes(), b"\xff" * tool.CONFIG_SIZE)
            commands = tool.planned_commands(args, Path("bit"), Path("app"), drive, config, Path("backup.bin"))
            self.assertEqual(commands[4][-3:], ["-o", hex(tool.FLASH_DRIVE_OFFSET), str(drive)])
            self.assertEqual(commands[5][-3:], ["-o", hex(tool.CONFIG_OFFSET), str(config)])

    def test_restore_replaces_the_region_writes(self) -> None:
        args = tool.parse_args(["firmware.u2l", "--flash", "--restore-backup", "old.bin"])
        commands = tool.planned_commands(args, Path("bit"), Path("app"), None, None, Path("backup.bin"))
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[2][-2], hex(0))


class AnalysisTests(unittest.TestCase):
    def reference(self) -> tool.RecoveryImage:
        bitstream = tool.ECP5_BIT_MARKER + tool.ECP5_PART_MARKER + b"body" + tool.ECP5_TRAILER
        application = make_application()
        return tool.RecoveryImage(
            firmware=Path("dummy.u2l"),
            firmware_sha256="",
            outer=tool.AppRecord(tool.OUTER_LOAD_ADDRESS, 1, tool.OUTER_LOAD_ADDRESS, 12),
            bitstream=bitstream,
            application=application,
            application_record=tool.AppRecord(
                tool.RUNTIME_LOAD_ADDRESS, len(application) - 12, tool.RUNTIME_LOAD_ADDRESS, 12
            ),
        )

    def test_healthy_image_reports_no_fault(self) -> None:
        reference = self.reference()
        raw = make_flash_image(bitstream=reference.bitstream, application=reference.application)
        _, faults = tool.analyse_flash_image(bytes(raw), reference)
        self.assertEqual(faults, [])

    def test_erased_application_slot_is_reported(self) -> None:
        reference = self.reference()
        raw = make_flash_image(bitstream=reference.bitstream, application=b"")
        _, faults = tool.analyse_flash_image(bytes(raw), reference)
        self.assertTrue(any("application slot is erased" in fault for fault in faults))

    def test_oversized_application_length_is_reported(self) -> None:
        """The bootloader silently never starts an application this large."""
        reference = self.reference()
        bad = struct.pack(
            "<III", tool.RUNTIME_LOAD_ADDRESS, tool.BOOTLOADER_MAX_APPL_SIZE, tool.RUNTIME_LOAD_ADDRESS
        )
        raw = make_flash_image(bitstream=reference.bitstream, application=bad)
        _, faults = tool.analyse_flash_image(bytes(raw), reference)
        self.assertTrue(any("outside the bootloader's" in fault for fault in faults))

    def test_missing_bitstream_preamble_is_reported(self) -> None:
        reference = self.reference()
        raw = make_flash_image(bitstream=b"\x00" * 64, application=reference.application)
        _, faults = tool.analyse_flash_image(bytes(raw), reference)
        self.assertTrue(any("no Lattice bitstream preamble" in fault for fault in faults))

    def test_all_zero_readback_is_rejected(self) -> None:
        _, faults = tool.analyse_flash_image(b"\x00" * tool.FLASH_SIZE, None)
        self.assertTrue(any("readback is not working" in fault for fault in faults))


class HardwareWorkflowTests(unittest.TestCase):
    """Model the external programmer, including failures at each write gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.firmware = self.root / "good.u2l"
        self.firmware.write_bytes(make_u2l())
        self.image = tool.extract_recovery_image(self.firmware)
        self.raw = bytes(make_flash_image(bitstream=self.image.bitstream, application=self.image.application))
        self.output = self.root / "output with spaces"
        self.addCleanup(patch.stopall)
        self.stdout = io.StringIO()
        self.stderr = SimpleNamespace(buffer=io.BytesIO(), write=lambda text: None, flush=lambda: None)
        patch.object(tool.sys, "stderr", self.stderr).start()
        patch.object(tool, "DeviceLock", return_value=contextlib.nullcontext()).start()
        self.host = patch.object(tool, "require_ecpprog").start()
        self.probe = patch.object(tool, "probe_target", return_value=tool.parse_probe_output(ProbeParsingTests.TRANSCRIPT)).start()
        self.calls = []
        self.fail_at = None
        self.backup_data = self.raw

        def programmer(command):
            self.calls.append(command)
            if len(self.calls) == self.fail_at:
                raise subprocess.CalledProcessError(3, command)
            if "-R" in command:
                Path(command[-1]).write_bytes(self.backup_data)
        self.run = patch.object(tool, "run_checked", side_effect=programmer).start()

    def main(self, *argv):
        with contextlib.redirect_stdout(self.stdout):
            return tool.main([str(self.firmware), "--output-dir", str(self.output), *argv])

    def test_default_inspection_is_offline_and_reusable(self):
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.main(), 0)
        self.host.assert_not_called()
        self.probe.assert_not_called()
        self.run.assert_not_called()
        self.assertIn("'", self.stdout.getvalue())  # shell-safe paths with spaces
        self.assertEqual((self.output / "u2l-runtime.bit").read_bytes(), self.image.bitstream)

    def test_different_package_cannot_overwrite_extracted_images(self):
        self.assertEqual(self.main(), 0)
        self.firmware.write_bytes(make_u2l(app_payload=b"a different application"))
        self.assertEqual(self.main("--flash"), 1)
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_preflight_does_not_need_a_firmware_file(self):
        self.firmware.unlink()
        with patch.object(tool, "preflight", return_value=0) as preflight:
            self.assertEqual(self.main("--preflight"), 0)
            preflight.assert_called_once()
        self.probe.assert_not_called()

    def test_probe_interrupts_fpga_but_never_programs(self):
        self.assertEqual(self.main("--probe"), 0)
        self.probe.assert_called_once()
        self.run.assert_not_called()
        self.assertIn("FPGA left unconfigured", self.stdout.getvalue())

    def test_sram_load_is_explicit_and_has_no_flash_writes(self):
        self.assertEqual(self.main("--sram"), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("-S", self.calls[0])
        self.assertNotIn("-p", self.calls[0])
        self.assertIn("-z", self.calls[0])
        self.assertFalse(list(self.output.glob("*backup*")))

    def test_dump_stops_after_backup(self):
        self.assertEqual(self.main("--dump"), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("-R", self.calls[0])
        self.assertIn("Backup SHA-256", self.stdout.getvalue())

    def test_full_flash_writes_only_after_backup_and_refreshes_last(self):
        self.assertEqual(self.main("--flash"), 0)
        self.assertEqual(len(self.calls), 3)
        self.assertIn("-R", self.calls[0])
        self.assertIn("-p", self.calls[1])
        self.assertIn("-p", self.calls[2])
        self.assertEqual(self.calls[1][-2], "0x0")
        self.assertEqual(self.calls[2][-2], "0xa0000")
        self.assertNotIn("-a", self.calls[1])
        self.assertIn("-a", self.calls[2])
        self.assertIn("Recovery flash completed", self.stdout.getvalue())

    def test_each_programmer_failure_prevents_all_later_writes(self):
        for index in (1, 2, 3):
            with self.subTest(index=index):
                self.output = self.root / f"failed-{index}"
                self.calls.clear()
                self.fail_at = index
                self.assertEqual(self.main("--flash"), 1)
                self.assertEqual(len(self.calls), index)

    def test_probe_failure_prevents_backup(self):
        self.probe.side_effect = RuntimeError("wrong IDCODE")
        self.assertEqual(self.main("--flash"), 1)
        self.run.assert_not_called()

    def test_existing_backup_aborts_before_probe(self):
        backup = self.root / "existing.bin"
        backup.write_bytes(b"precious original")
        self.assertEqual(self.main("--flash", "--backup", str(backup)), 1)
        self.assertEqual(backup.read_bytes(), b"precious original")
        self.probe.assert_not_called()

    def test_short_backup_blocks_writes(self):
        self.backup_data = b"short"
        self.assertEqual(self.main("--flash"), 1)
        self.assertEqual(len(self.calls), 1)

    def test_all_zero_backup_blocks_writes(self):
        self.backup_data = b"\x00" * tool.FLASH_SIZE
        self.assertEqual(self.main("--flash"), 1)
        self.assertEqual(len(self.calls), 1)

    def test_erased_flash_is_recoverable(self):
        self.backup_data = b"\xff" * tool.FLASH_SIZE
        self.assertEqual(self.main("--flash"), 0)
        self.assertEqual(len(self.calls), 3)

    def test_optional_erases_run_after_boot_writes(self):
        self.assertEqual(self.main("--flash", "--erase-flash-drive", "--reset-configuration"), 0)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.calls[-2][-2], "0x200000")
        self.assertEqual(self.calls[-1][-2], "0x7f0000")
        self.assertIn("-a", self.calls[-1])
        self.assertTrue(all("-a" not in cmd for cmd in self.calls[:-1]))

    def test_restore_preserves_input_and_takes_a_new_backup(self):
        restore = self.root / "old.bin"
        restore.write_bytes(self.raw)
        self.assertEqual(self.main("--flash", "--restore-backup", str(restore)), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("-R", self.calls[0])
        self.assertEqual(self.calls[1][-1], str(restore))
        self.assertIn("-a", self.calls[1])
        self.assertEqual(restore.read_bytes(), self.raw)

    def test_invalid_restore_never_opens_usb(self):
        restore = self.root / "old.bin"
        self.assertEqual(self.main("--flash", "--restore-backup", str(restore)), 1)
        restore.write_bytes(b"short")
        self.assertEqual(self.main("--flash", "--restore-backup", str(restore)), 1)
        self.probe.assert_not_called()

    def test_analysis_is_offline(self):
        dump = self.root / "dump.bin"
        dump.write_bytes(self.raw)
        self.assertEqual(self.main("--analyse", str(dump)), 0)
        self.host.assert_not_called()
        self.run.assert_not_called()

    def test_bad_firmware_never_opens_usb(self):
        self.firmware.write_bytes(b"bad")
        self.assertEqual(self.main("--flash"), 1)
        self.probe.assert_not_called()


class ProgrammerTests(unittest.TestCase):
    def test_guard_is_required_before_hardware_access(self):
        args = tool.parse_args(["file.u2l"])
        with patch.object(tool.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                tool.require_ecpprog(args)
        with patch.object(tool.shutil, "which", return_value="/bin/ecpprog"), \
             patch.object(tool.subprocess, "run", return_value=SimpleNamespace(stdout="", stderr="old help")):
            with self.assertRaisesRegex(RuntimeError, "IDCODE guard"):
                tool.require_ecpprog(args)
        with patch.object(tool.shutil, "which", return_value="/bin/ecpprog"), \
             patch.object(tool.subprocess, "run", return_value=SimpleNamespace(stdout="-z guard", stderr="")) as run:
            tool.require_ecpprog(args)
            self.assertEqual(run.call_args.args[0], ["ecpprog", "--help"])

    def test_probe_transcript_and_exit_failures(self):
        for status, transcript in ((0, ProbeParsingTests.TRANSCRIPT), (2, "USB unavailable"), (1, "other error")):
            with self.subTest(status=status), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(tool.subprocess, "run", return_value=SimpleNamespace(
                     returncode=status, stdout="", stderr=transcript)):
                if status:
                    with self.assertRaisesRegex(RuntimeError, "failed with exit status"):
                        tool.probe_target(["ecpprog", "-t"])
                else:
                    self.assertEqual(tool.probe_target(["ecpprog", "-t"]).idcode, tool.EXPECTED_IDCODE)

    def test_streaming_programmer_output_is_checked(self):
        cases = ((0, b"VERIFY OK\n", None), (3, b"failure\n", subprocess.CalledProcessError),
                 (0, b"failed to disable protection\n", RuntimeError),
                 (0, b"Found difference between flash and file!\n", RuntimeError),
                 (0, b"programming complete", RuntimeError))
        for status, transcript, error in cases:
            with self.subTest(transcript=transcript):
                process = Mock()
                process.stderr = io.BytesIO(transcript)
                process.wait.return_value = status
                process.__enter__ = Mock(return_value=process)
                process.__exit__ = Mock(return_value=False)
                with patch.object(tool.subprocess, "Popen", return_value=process), \
                     patch.object(tool.sys, "stderr", SimpleNamespace(buffer=io.BytesIO())), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if error:
                        with self.assertRaises(error):
                            tool.run_checked(["ecpprog", "-p", "image.bit"])
                    else:
                        tool.run_checked(["ecpprog", "-p", "image.bit"])

    def test_reads_and_sram_loads_do_not_require_spi_verify_message(self):
        for flag in ("-R", "-S"):
            process = Mock()
            process.stderr = io.BytesIO(b"Bye.\n")
            process.wait.return_value = 0
            process.__enter__ = Mock(return_value=process)
            process.__exit__ = Mock(return_value=False)
            with patch.object(tool.subprocess, "Popen", return_value=process), \
                 patch.object(tool.sys, "stderr", SimpleNamespace(buffer=io.BytesIO())), \
                 contextlib.redirect_stdout(io.StringIO()):
                tool.run_checked(["ecpprog", flag, "file"])


class HostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.device = self.root / "1-2"
        self.device.mkdir()
        for name, value in (("idVendor", "0403"), ("idProduct", "6014"), ("busnum", "1"), ("devnum", "2")):
            (self.device / name).write_text(value)
        self.args = tool.parse_args(["placeholder.u2l", "--preflight"])

    def test_sysfs_enumeration_filters_invalid_devices(self):
        wrong = self.root / "wrong"
        wrong.mkdir()
        (wrong / "idVendor").write_text("ffff")
        invalid = self.root / "invalid"
        invalid.mkdir()
        (invalid / "idVendor").write_text("invalid hex")
        other = self.root / "other"
        other.mkdir()
        (other / "idVendor").write_text("0403")
        (other / "idProduct").write_text("6010")
        with patch.object(tool, "Path", return_value=self.root):
            self.assertEqual(tool.usb_devices(0x0403, 0x6014), [self.device])
        with patch.object(tool, "Path", return_value=self.root / "missing"):
            self.assertEqual(tool.usb_devices(0x0403, 0x6014), [])

    def preflight(self, linkage, *, executable="/bin/ecpprog", devices=None, writable=True):
        with patch.object(tool.shutil, "which", return_value=executable), \
             patch.object(tool.subprocess, "run", return_value=SimpleNamespace(stdout=linkage)), \
             patch.object(tool, "usb_devices", return_value=[self.device] if devices is None else devices), \
             patch.object(tool.os, "access", return_value=writable), \
             contextlib.redirect_stdout(io.StringIO()):
            return tool.preflight(self.args)

    def test_missing_programmer_and_adapter(self):
        self.assertEqual(self.preflight("", executable=None, devices=[]), 1)

    def test_missing_library_or_wrong_linkage(self):
        self.assertEqual(self.preflight("libftdi.so => not found"), 1)
        self.assertEqual(self.preflight("libc.so => /lib/libc.so"), 1)

    def test_good_library_and_usb_access(self):
        self.assertEqual(self.preflight("libftdi.so => /lib/libftdi.so"), 0)
        self.assertEqual(self.preflight("libftdi.so => /lib/libftdi.so", writable=False), 1)

    def test_missing_linkage_information_is_reported_as_warning(self):
        self.assertEqual(self.preflight(""), 0)
        with patch.object(tool.subprocess, "run", side_effect=OSError), \
             patch.object(tool.shutil, "which", return_value="/bin/ecpprog"), \
             patch.object(tool, "usb_devices", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.preflight(self.args), 1)

    def test_kernel_driver_and_unreadable_device_numbers(self):
        interface = self.device / "1-2:1.0"
        interface.mkdir()
        driver = interface / "driver"
        driver.symlink_to(self.root / "ftdi_sio")
        self.assertEqual(self.preflight("libftdi.so => /lib/libftdi.so"), 0)
        driver.unlink()
        driver.symlink_to(self.root / "other_driver")
        self.assertEqual(self.preflight("libftdi.so => /lib/libftdi.so"), 0)
        (self.device / "busnum").write_text("invalid")
        self.assertEqual(self.preflight("libftdi.so => /lib/libftdi.so"), 0)


class AdditionalValidationTests(unittest.TestCase):
    def test_invalid_headers(self):
        for data, offset in ((b"", 0), (b"x" * 12, -1), (struct.pack("<III", 0, 0, 0), 0),
                             (struct.pack("<III", 0, 9999, 0), 0)):
            with self.subTest(data=data), self.assertRaises(tool.FirmwareFormatError):
                tool.parse_record(data, offset, context="test")
        self.assertFalse(tool.looks_like_application_header(b"", 0))
        self.assertFalse(tool.preceded_by_bitstream_padding(b"", 0, 0))

    def test_invalid_outer_and_bitstream(self):
        base = make_u2l()
        variants = (base + b"extra", base.replace(tool.ECP5_BIT_MARKER, b"x" * len(tool.ECP5_BIT_MARKER)),
                    base.replace(tool.ECP5_PART_MARKER, b"x" * len(tool.ECP5_PART_MARKER)),
                    struct.pack("<I", 0) + base[4:])
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "file.u2l"
            with self.assertRaises(tool.FirmwareFormatError):
                tool.extract_recovery_image(firmware)
            for blob in variants:
                with self.subTest(blob=blob[:12]), self.assertRaises(tool.FirmwareFormatError):
                    firmware.write_bytes(blob)
                    tool.extract_recovery_image(firmware)

    def test_missing_and_short_probe_ids(self):
        for transcript in ("", "IDCODE: 0x41111043", "IDCODE: 0x41111043\nflash ID: 0xEF"):
            with self.subTest(transcript=transcript), self.assertRaises(RuntimeError):
                tool.parse_probe_output(transcript)

    def test_invalid_cli_combinations(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for flags in (("--clock-divider", "0"), ("--clock-divider", "65537"),
                          ("--flash", "--restore-backup", "x", "--reset-configuration"),
                          ("--analyse", "x", "--probe"), ("--sram", "--flash")):
                with self.subTest(flags=flags), self.assertRaises(SystemExit):
                    tool.parse_args(["f.u2l", *flags])

    def test_analysis_reports_structure_without_claiming_correct_behavior(self):
        self.assertEqual(tool.region_state(b"", 0, 1), "missing")
        self.assertEqual(tool.region_state(b"\x00", 0, 1), "all zero")
        self.assertTrue(tool.analyse_flash_image(b"short", None)[1])
        self.assertTrue(tool.analyse_flash_image(b"\xff" * tool.FLASH_SIZE, None)[1])
        reference = AnalysisTests().reference()
        raw = make_flash_image(bitstream=reference.bitstream, application=reference.application)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tool.print_analysis(raw, None), [])
        self.assertIn("does not prove", output.getvalue())
        raw[tool.APPLICATION_OFFSET:tool.APPLICATION_OFFSET + 4] = b"\x00" * 4
        self.assertTrue(tool.analyse_flash_image(raw, reference)[1])
        raw[0:len(tool.ECP5_PART_MARKER)] = tool.ECP5_BIT_MARKER[:len(tool.ECP5_PART_MARKER)]
        self.assertTrue(tool.analyse_flash_image(raw, reference)[1])

    def test_estimate_uses_measured_backup_time(self):
        self.assertGreaterEqual(tool.estimate_seconds(tool.FLASH_SIZE, 30, programming=False), 3600)
        self.assertGreater(tool.estimate_seconds(10000, 30, programming=True), 1)
        self.assertIn("s", tool.format_duration(30))
        self.assertIn("min", tool.format_duration(3600))


if __name__ == "__main__":
    unittest.main()
