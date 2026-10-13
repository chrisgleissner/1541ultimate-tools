#!/usr/bin/env python3
"""Recover an Ultimate II+L by programming its ECP5-connected SPI flash.

A U2+L ``.u2l`` file is an updater application, not an SPI flash image.  It
contains the runtime ECP5 bitstream and the Ultimate application as embedded
objects.  This utility extracts those two objects and uses the repository's
``ecpprog`` driver to write them to their documented U2+L flash regions.

With no mode flag it is deliberately read-only: it validates the firmware and
prints the exact recovery plan.  This is the intended mode before the
FT232H/JTAG lead is connected.

Every address, size limit and header layout used here is taken from the
1541ultimate sources; see ADDRESS_PROVENANCE below.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import shlex
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from u64ii_jtag import DeviceLock


# --- U2+L flash layout -------------------------------------------------------
#
# ADDRESS_PROVENANCE
#   software/io/flash/w25q_flash.cc, flash_addresses_u2pl[]:
#       FLASH_ID_BOOTFPGA   has_header=0  start=0x000000
#       FLASH_ID_APPL       has_header=0  start=0x0A0000  max_length=0x160000
#       FLASH_ID_FLASHDRIVE has_header=0  start=0x200000  max_length=0x5F0000
#       FLASH_ID_CONFIG     has_header=0  start=0x7F0000  max_length=0x010000
#   has_header=0 means prog_flash.cc writes the buffer raw, with no 16-byte
#   version header in front of it, so the images below are written verbatim.
#   software/portable/riscv/bootloader.c reads the 12-byte application record
#   (load address, length, entry address) from 0xA0000 and refuses lengths that
#   are not below MAX_APPL_SIZE (0x140000).
#   software/io/flash/w25q_flash.cc read_config_page() places the 16 config
#   sectors at the very top of the device, which only lands on 0x7F0000 for an
#   8 MiB part; that is why FLASH_SIZE is fixed rather than probed.

U2L_EXTENSION = ".u2l"
OUTER_LOAD_ADDRESS = 0x03000000
RUNTIME_LOAD_ADDRESS = 0x00030000
BOOT_FPGA_OFFSET = 0x000000
APPLICATION_OFFSET = 0x0A0000
APPLICATION_SLOT_SIZE = 0x160000  # flash_addresses_u2pl[] max_length for APPL
BOOTLOADER_MAX_APPL_SIZE = 0x140000  # bootloader.c MAX_APPL_SIZE
FLASH_DRIVE_OFFSET = 0x200000
FLASH_DRIVE_LIMIT = 0x7F0000
FLASH_DRIVE_SIZE = FLASH_DRIVE_LIMIT - FLASH_DRIVE_OFFSET
CONFIG_OFFSET = 0x7F0000
CONFIG_SIZE = 0x010000
FLASH_SIZE = 0x800000

# The updater links both objects into .rodata with `.align 4`, which on RISC-V
# means 2**4, so both records start on a 16-byte boundary and at most 15 zero
# bytes separate them (software/application/update_u2pl/update_binaries.s).
OBJECT_ALIGNMENT = 16

ECP5_BIT_MARKER = b"\xff\x00Lattice Semiconductor Corporation Bitstream\x00"
ECP5_PART_MARKER = b"Part: LFE5U-25F-6CABGA256\x00"
ECP5_TRAILER = b"\xff\xff\xff\xff"

# --- ecpprog behaviour -------------------------------------------------------
#
# tools/ecpprog/ecpprog/ecpprog.c ecp_prog_flash() rounds every erase outwards
# to whole erase blocks, default 64 KiB.  Collateral erasure of a neighbouring
# region is the one mistake that would turn a recoverable cartridge into a
# harder case, so every planned write is checked against this rounding.
ERASE_BLOCK_SIZE = 0x10000

# ecp5 IDCODE and JEDEC IDs the U2+L firmware itself accepts.
EXPECTED_IDCODE = 0x41111043
EXPECTED_IDCODE_NAME = "LFE5U-25"
# software/io/flash/w25q_flash.cc W25Q_Flash::tester()   -> 0xEF 0x40, cap 0x17
# software/io/flash/s25fl_flash.cc S25FL_Flash::tester() -> 0x01 0x40, cap 0x17
SUPPORTED_FLASH_IDS = {
    (0xEF, 0x40): "Winbond W25Q64",
    (0x01, 0x40): "Spansion/Cypress S25FL164K",
}
CAPACITY_8MIB = 0x17

DEFAULT_VID_PID = "i:0x0403:0x6014:0"  # Adafruit FT232H; index avoids ambiguity.
FT232H_VID = 0x0403
FT232H_PID = 0x6014

# ecpprog clocks JTAG at 6 MHz / divider (mpsse.c mpsse_init).
JTAG_BASE_CLOCK_HZ = 6_000_000


class FirmwareFormatError(ValueError):
    """The supplied file is not a U2+L updater that this tool can prove safe."""


class LayoutError(RuntimeError):
    """A planned write would not stay inside its documented flash region."""


@dataclass(frozen=True)
class AppRecord:
    load_address: int
    length: int
    entry_address: int
    data_offset: int


@dataclass(frozen=True)
class RecoveryImage:
    firmware: Path
    firmware_sha256: str
    outer: AppRecord
    bitstream: bytes
    application: bytes
    application_record: AppRecord


@dataclass(frozen=True)
class ProbeResult:
    idcode: int
    device_name: str
    jedec: tuple[int, int, int]
    flash_name: str
    flash_size: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def format_size(value: int) -> str:
    return f"{value:,} bytes (0x{value:x})"


# --- firmware container parsing ---------------------------------------------


def parse_record(data: bytes, offset: int, *, context: str) -> AppRecord:
    if offset < 0 or offset + 12 > len(data):
        raise FirmwareFormatError(f"{context}: truncated 12-byte application header")
    load_address, length, entry_address = struct.unpack_from("<III", data, offset)
    padded_length = (length + 3) & ~3
    if length == 0 or offset + 12 + padded_length > len(data):
        raise FirmwareFormatError(f"{context}: invalid payload length 0x{length:x}")
    return AppRecord(load_address, length, entry_address, offset + 12)


def looks_like_application_header(payload: bytes, offset: int) -> bool:
    """Check the three header words the U2+L bootloader itself consumes.

    bootloader.c reads a load address, a length and an entry address from
    0xA0000 and only starts the application when the length is below
    MAX_APPL_SIZE.  Applying the same three tests here means a record this
    function accepts is a record the cartridge would actually run.
    """
    if offset + 12 > len(payload):
        return False
    load_address, length, entry_address = struct.unpack_from("<III", payload, offset)
    if load_address != RUNTIME_LOAD_ADDRESS or entry_address != RUNTIME_LOAD_ADDRESS:
        return False
    if length == 0 or length >= BOOTLOADER_MAX_APPL_SIZE:
        return False
    if 12 + ((length + 3) & ~3) > APPLICATION_SLOT_SIZE:
        return False
    return offset + 12 + ((length + 3) & ~3) <= len(payload)


def preceded_by_bitstream_padding(payload: bytes, offset: int, bitstream_start: int) -> bool:
    """Require the linker layout: ECP5 trailer, then 0..15 zero pad bytes."""
    for pad in range(0, OBJECT_ALIGNMENT):
        end = offset - pad
        if end - len(ECP5_TRAILER) < bitstream_start:
            break
        if payload[end - len(ECP5_TRAILER) : end] != ECP5_TRAILER:
            continue
        if payload[end:offset] != b"\x00" * pad:
            continue
        return True
    return False


def find_application_record(payload: bytes, bitstream_start: int) -> AppRecord:
    """Locate the single embedded Ultimate application record.

    Candidates must satisfy both the bootloader's header test and the linker's
    placement rule (16-byte aligned, directly after the bitstream trailer and
    its zero padding).  Requiring both means an incidental header-shaped word
    inside executable data cannot create a false "ambiguous" abort, while a
    genuine change to the packaging format still fails closed.
    """
    first_possible = bitstream_start + len(ECP5_BIT_MARKER)
    start = (first_possible + OBJECT_ALIGNMENT - 1) & ~(OBJECT_ALIGNMENT - 1)
    candidates: list[int] = []
    for offset in range(start, len(payload) - 12, OBJECT_ALIGNMENT):
        if not looks_like_application_header(payload, offset):
            continue
        if not preceded_by_bitstream_padding(payload, offset, bitstream_start):
            continue
        candidates.append(offset)
    if len(candidates) != 1:
        raise FirmwareFormatError(
            "could not identify one embedded Ultimate application "
            f"(found {len(candidates)} plausible records)"
        )
    return parse_record(payload, candidates[0], context="embedded Ultimate application")


def extract_recovery_image(firmware: Path) -> RecoveryImage:
    firmware = firmware.resolve()
    if not firmware.is_file():
        raise FirmwareFormatError(f"firmware does not exist: {firmware}")
    if firmware.suffix.lower() != U2L_EXTENSION:
        raise FirmwareFormatError(f"expected a {U2L_EXTENSION} firmware, got: {firmware.name}")

    blob = firmware.read_bytes()
    outer = parse_record(blob, 0, context="U2+L updater")
    if outer.load_address != OUTER_LOAD_ADDRESS or outer.entry_address != OUTER_LOAD_ADDRESS:
        raise FirmwareFormatError(
            "unexpected U2+L updater load/entry address "
            f"(0x{outer.load_address:08x}/0x{outer.entry_address:08x})"
        )
    if 12 + ((outer.length + 3) & ~3) != len(blob):
        raise FirmwareFormatError("U2+L updater has trailing data or multiple outer records")

    payload = blob[outer.data_offset : outer.data_offset + outer.length]
    bitstream_start = payload.find(ECP5_BIT_MARKER)
    if bitstream_start < 0:
        raise FirmwareFormatError("embedded Lattice ECP5 bitstream was not found")
    if payload.find(ECP5_BIT_MARKER, bitstream_start + 1) >= 0:
        raise FirmwareFormatError("more than one embedded Lattice bitstream was found")
    if ECP5_PART_MARKER not in payload[bitstream_start : bitstream_start + 0x400]:
        raise FirmwareFormatError("embedded bitstream is not for LFE5U-25F-6CABGA256")
    if bitstream_start % OBJECT_ALIGNMENT:
        raise FirmwareFormatError(
            f"embedded bitstream starts at 0x{bitstream_start:x}, which is not "
            f"{OBJECT_ALIGNMENT}-byte aligned as the updater linker requires"
        )

    application_record = find_application_record(payload, bitstream_start)
    application_start = application_record.data_offset - 12
    if application_start % OBJECT_ALIGNMENT:
        raise FirmwareFormatError(
            f"embedded application starts at 0x{application_start:x}, which is not "
            f"{OBJECT_ALIGNMENT}-byte aligned as the updater linker requires"
        )

    # Locate the bitstream's final ECP5 trailer rather than guessing from
    # alignment: a Diamond .bit file can itself end on a four-byte boundary,
    # after which the binary-object linker still adds zero padding.
    trailer = payload.rfind(ECP5_TRAILER, bitstream_start, application_start)
    if trailer < bitstream_start:
        raise FirmwareFormatError("embedded bitstream has no ECP5 trailer")
    bitstream_end = trailer + len(ECP5_TRAILER)
    padding_size = application_start - bitstream_end
    if padding_size >= OBJECT_ALIGNMENT:
        raise FirmwareFormatError(
            f"0x{padding_size:x} bytes between the bitstream and the application "
            f"exceed the {OBJECT_ALIGNMENT}-byte linker alignment"
        )
    if payload[bitstream_end:application_start] != b"\x00" * padding_size:
        raise FirmwareFormatError("non-zero bytes found in padding before embedded Ultimate application")

    bitstream = payload[bitstream_start:bitstream_end]
    if len(bitstream) == 0 or len(bitstream) > APPLICATION_OFFSET:
        raise FirmwareFormatError(
            f"embedded bitstream size 0x{len(bitstream):x} would overlap the U2+L application slot"
        )

    application_end = application_record.data_offset + ((application_record.length + 3) & ~3)
    application = payload[application_start:application_end]
    if len(application) > APPLICATION_SLOT_SIZE:
        raise FirmwareFormatError("embedded Ultimate application exceeds its U2+L flash slot")

    return RecoveryImage(
        firmware=firmware,
        firmware_sha256=sha256_file(firmware),
        outer=outer,
        bitstream=bitstream,
        application=application,
        application_record=application_record,
    )


# --- erase-span arithmetic ---------------------------------------------------


def erase_span(offset: int, length: int) -> tuple[int, int]:
    """Return the range ecpprog will actually erase for this write.

    ecp_prog_flash() erases from ``offset`` rounded down to an erase block up
    to ``offset + length`` rounded up, so a write can destroy data before and
    after the bytes it programs.
    """
    mask = ERASE_BLOCK_SIZE - 1
    return offset & ~mask, (offset + length + mask) & ~mask


def check_write_stays_in_region(
    *, name: str, offset: int, length: int, region_start: int, region_end: int
) -> None:
    start, end = erase_span(offset, length)
    if start < region_start or end > region_end:
        raise LayoutError(
            f"{name}: writing {format_size(length)} at 0x{offset:06x} erases "
            f"0x{start:06x}-0x{end - 1:06x}, outside its region "
            f"0x{region_start:06x}-0x{region_end - 1:06x}"
        )
    if end > FLASH_SIZE:
        raise LayoutError(f"{name}: erase would run past the end of the {FLASH_SIZE // 1024 // 1024} MiB device")


def check_layout(image: RecoveryImage) -> None:
    """Prove every planned write stays inside its own documented region."""
    check_write_stays_in_region(
        name="ECP5 bitstream",
        offset=BOOT_FPGA_OFFSET,
        length=len(image.bitstream),
        region_start=BOOT_FPGA_OFFSET,
        region_end=APPLICATION_OFFSET,
    )
    check_write_stays_in_region(
        name="Ultimate application",
        offset=APPLICATION_OFFSET,
        length=len(image.application),
        region_start=APPLICATION_OFFSET,
        region_end=FLASH_DRIVE_OFFSET,
    )
    check_write_stays_in_region(
        name="flash drive erase",
        offset=FLASH_DRIVE_OFFSET,
        length=FLASH_DRIVE_SIZE,
        region_start=FLASH_DRIVE_OFFSET,
        region_end=FLASH_DRIVE_LIMIT,
    )
    check_write_stays_in_region(
        name="configuration erase",
        offset=CONFIG_OFFSET,
        length=CONFIG_SIZE,
        region_start=CONFIG_OFFSET,
        region_end=CONFIG_OFFSET + CONFIG_SIZE,
    )
    record = image.application_record
    if record.length >= BOOTLOADER_MAX_APPL_SIZE:
        raise LayoutError(
            f"application payload 0x{record.length:x} is not below the bootloader's "
            f"MAX_APPL_SIZE 0x{BOOTLOADER_MAX_APPL_SIZE:x}; the cartridge would not start it"
        )


# --- host-side files ---------------------------------------------------------


def write_extracted_files(image: RecoveryImage, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    bitstream = directory / "u2l-runtime.bit"
    application = directory / "u2l-ultimate.app"
    for path, data in ((bitstream, image.bitstream), (application, image.application)):
        if path.exists():
            if path.read_bytes() != data:
                raise RuntimeError(f"output already contains a different image: {path}; use a new --output-dir")
        else:
            with path.open("xb") as stream:
                stream.write(data)
    return bitstream, application


def write_erased_fill(path: Path, size: int) -> Path:
    """Create a file of erased-flash bytes for ecpprog's erase-and-verify path."""
    chunk = b"\xff" * (1024 * 1024)
    with path.open("xb") as stream:
        remaining = size
        while remaining:
            amount = min(remaining, len(chunk))
            stream.write(chunk[:amount])
            remaining -= amount
    return path


def optional_erase_files(args: argparse.Namespace, directory: Path) -> tuple[Path | None, Path | None]:
    flash_drive = None
    configuration = None
    if args.erase_flash_drive:
        directory.mkdir(parents=True, exist_ok=True)
        flash_drive = write_erased_fill(directory / "u2l-flash-drive-erased.ff", FLASH_DRIVE_SIZE)
    if args.reset_configuration:
        directory.mkdir(parents=True, exist_ok=True)
        configuration = write_erased_fill(directory / "u2l-configuration-erased.ff", CONFIG_SIZE)
    return flash_drive, configuration


# --- ecpprog command construction -------------------------------------------


def ecpprog_command(args: argparse.Namespace, *extra: str) -> list[str]:
    return [args.ecpprog, "-z", "-d", args.device, "-I", "A", "-k", str(args.clock_divider), *extra]


def planned_commands(
    args: argparse.Namespace,
    bitstream: Path | None,
    application: Path | None,
    flash_drive_erase: Path | None,
    config_erase: Path | None,
    backup: Path,
) -> list[list[str]]:
    """Build the hardware command sequence, probe first and refresh last.

    ecpprog erases the ECP5's SRAM configuration at the start of every flash
    operation (ecp_init_flash_mode), so a ``-a`` refresh placed anywhere but on
    the final command is undone by the command that follows it.
    """
    commands: list[list[str]] = [ecpprog_command(args, "-t")]
    if args.sram:
        return [commands[0], ecpprog_command(args, "-S", str(bitstream))]
    commands.append(ecpprog_command(args, "-R", hex(FLASH_SIZE), "-o", "0", str(backup)))

    writes: list[list[str]] = []
    if args.restore_backup:
        writes.append(
            ecpprog_command(args, "-p", "-i", "64", "-o", hex(0), str(Path(args.restore_backup).expanduser().resolve()))
        )
    else:
        if bitstream is not None:
            writes.append(ecpprog_command(args, "-p", "-i", "64", "-o", hex(BOOT_FPGA_OFFSET), str(bitstream)))
        if application is not None:
            writes.append(ecpprog_command(args, "-p", "-i", "64", "-o", hex(APPLICATION_OFFSET), str(application)))
        # ecpprog's -e mode is unusable here: it passes a NULL FILE* into
        # ecp_prog_flash, which immediately fseek()s it.  Programming an FF
        # fill file instead erases exact 64 KiB-aligned ranges and then
        # readback-verifies every erased byte.
        if flash_drive_erase is not None:
            writes.append(
                ecpprog_command(args, "-p", "-i", "64", "-o", hex(FLASH_DRIVE_OFFSET), str(flash_drive_erase))
            )
        if config_erase is not None:
            writes.append(ecpprog_command(args, "-p", "-i", "64", "-o", hex(CONFIG_OFFSET), str(config_erase)))

    if writes:
        # Every write command ends with "-o <offset> <file>", so the refresh
        # flag goes three places from the end, in front of "-o".
        last = writes[-1]
        last.insert(len(last) - 3, "-a")
    commands.extend(writes)
    return commands


# --- probing -----------------------------------------------------------------

IDCODE_PATTERN = re.compile(r"IDCODE: 0x([0-9a-fA-F]{8})(?: \(([^)]+)\))?")
FLASH_ID_PATTERN = re.compile(r"flash ID:((?: 0x[0-9A-Fa-f]{2})+)")


def parse_probe_output(transcript: str) -> ProbeResult:
    idcode_match = IDCODE_PATTERN.search(transcript)
    if not idcode_match:
        raise RuntimeError("JTAG probe printed no IDCODE; check wiring, ground and power")
    idcode = int(idcode_match.group(1), 16)
    device_name = idcode_match.group(2) or "unknown"
    if idcode != EXPECTED_IDCODE:
        raise RuntimeError(
            f"JTAG probe read IDCODE 0x{idcode:08x} ({device_name}); "
            f"expected 0x{EXPECTED_IDCODE:08x} ({EXPECTED_IDCODE_NAME}). "
            "An IDCODE of 0x00000000 or 0xffffffff usually means TDO, ground or power is wrong."
        )

    flash_match = FLASH_ID_PATTERN.search(transcript)
    if not flash_match:
        raise RuntimeError("JTAG probe printed no SPI flash JEDEC ID")
    values = [int(token, 16) for token in flash_match.group(1).split()]
    if len(values) < 3:
        raise RuntimeError(f"JTAG probe returned a short JEDEC ID: {flash_match.group(0)!r}")
    jedec = (values[0], values[1], values[2])
    flash_name = SUPPORTED_FLASH_IDS.get((jedec[0], jedec[1]))
    if flash_name is None:
        raise RuntimeError(
            "JTAG probe read JEDEC ID "
            f"0x{jedec[0]:02X} 0x{jedec[1]:02X} 0x{jedec[2]:02X}, which no U2+L flash "
            "driver in the 1541ultimate sources accepts. Refusing to write."
        )
    if jedec[2] != CAPACITY_8MIB:
        raise RuntimeError(
            f"{flash_name} reports capacity byte 0x{jedec[2]:02X}; this layout requires "
            f"the 8 MiB part (0x{CAPACITY_8MIB:02X}). Refusing to write."
        )
    return ProbeResult(idcode, device_name, jedec, flash_name, FLASH_SIZE)


def probe_target(command: list[str]) -> ProbeResult:
    print("+ " + shlex.join(command), flush=True)
    completed = subprocess.run(command, text=True, capture_output=True)
    transcript = completed.stdout + completed.stderr
    if transcript:
        print(transcript, end="" if transcript.endswith("\n") else "\n", flush=True)
    if completed.returncode != 0:
        # ecpprog exits 2 when it cannot claim the FTDI device at all, which is
        # a host access problem rather than a wiring problem.
        hint = " Run --preflight to check the FT232H and its permissions." if completed.returncode == 2 else ""
        raise RuntimeError(f"the JTAG probe failed with exit status {completed.returncode}.{hint}")
    result = parse_probe_output(transcript)
    print(
        f"Probe OK: ECP5 {result.device_name} (0x{result.idcode:08x}), "
        f"{result.flash_name} "
        f"(JEDEC 0x{result.jedec[0]:02X} 0x{result.jedec[1]:02X} 0x{result.jedec[2]:02X})."
    )
    return result


def run_checked(command: list[str]) -> None:
    """Run an ecpprog command, failing on its output as well as its status.

    ecpprog writes its progress counters to stderr and overwrites them with
    carriage returns, so the output is forwarded in raw chunks rather than by
    line. It is also inspected afterwards: flash_disable_protection() only
    prints a warning when the status register will not clear, and the erase and
    program steps then quietly do nothing. The verify pass would catch that,
    but naming the real cause is more useful than reporting a mismatch.
    """
    print("+ " + shlex.join(command), flush=True)
    captured = bytearray()
    with subprocess.Popen(command, stderr=subprocess.PIPE) as process:
        assert process.stderr is not None
        while True:
            chunk = process.stderr.read1(4096)
            if not chunk:
                break
            captured += chunk
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
        returncode = process.wait()
    transcript = captured.decode("utf-8", "replace")
    if not transcript.endswith("\n"):
        sys.stderr.buffer.write(b"\n")
        sys.stderr.buffer.flush()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, command)
    if "failed to disable protection" in transcript:
        raise RuntimeError(
            "ecpprog could not clear the SPI status register, so the flash is still "
            "write protected. Check the JTAG signal integrity and retry with a "
            "larger --clock-divider."
        )
    if "Found difference between flash and file" in transcript:
        raise RuntimeError("ecpprog readback verification failed; the write did not land correctly")
    if "-R" not in command and "-S" not in command and "VERIFY OK" not in transcript:
        raise RuntimeError("ecpprog did not report VERIFY OK; refusing to continue")


# --- on-device image analysis ------------------------------------------------


def region_state(raw: bytes, offset: int, size: int) -> str:
    chunk = raw[offset : offset + size]
    if not chunk:
        return "missing"
    if chunk.count(0xFF) == len(chunk):
        return "erased"
    if chunk.count(0x00) == len(chunk):
        return "all zero"
    return "programmed"


def analyse_flash_image(raw: bytes, reference: RecoveryImage | None) -> tuple[list[str], list[str]]:
    """Describe what is actually on the cartridge, and list the faults found."""
    notes: list[str] = []
    faults: list[str] = []

    if len(raw) != FLASH_SIZE:
        faults.append(f"image is {len(raw)} bytes, expected {FLASH_SIZE}")
        return notes, faults

    if raw.count(0xFF) == len(raw):
        faults.append("the whole device reads as erased; either it really is blank or TDO is not being read")
        return notes, faults
    if raw.count(0x00) == len(raw):
        faults.append("the whole device reads as zero, which means the readback is not working; do not write")
        return notes, faults

    # ECP5 configuration region.
    if raw[BOOT_FPGA_OFFSET : BOOT_FPGA_OFFSET + len(ECP5_BIT_MARKER)] != ECP5_BIT_MARKER:
        faults.append(f"0x{BOOT_FPGA_OFFSET:06x}: no Lattice bitstream preamble; the ECP5 has nothing valid to load")
    else:
        header = raw[BOOT_FPGA_OFFSET : BOOT_FPGA_OFFSET + 0x400]
        if ECP5_PART_MARKER not in header:
            faults.append(f"0x{BOOT_FPGA_OFFSET:06x}: bitstream is not built for LFE5U-25F-6CABGA256")
        else:
            notes.append(f"0x{BOOT_FPGA_OFFSET:06x}: Lattice LFE5U-25F bitstream present")
        if reference is not None:
            on_device = raw[BOOT_FPGA_OFFSET : BOOT_FPGA_OFFSET + len(reference.bitstream)]
            if on_device == reference.bitstream:
                notes.append("             bitstream is byte-identical to the supplied firmware")
            else:
                notes.append("             bitstream differs from the supplied firmware (expected after a bad update)")

    # Ultimate application region.
    load_address, length, entry_address = struct.unpack_from("<III", raw, APPLICATION_OFFSET)
    notes.append(
        f"0x{APPLICATION_OFFSET:06x}: application header load=0x{load_address:08x} "
        f"length=0x{length:08x} entry=0x{entry_address:08x}"
    )
    if load_address == 0xFFFFFFFF and length == 0xFFFFFFFF:
        faults.append(f"0x{APPLICATION_OFFSET:06x}: application slot is erased; the bootloader stops at 'Empty'")
    elif load_address != RUNTIME_LOAD_ADDRESS or entry_address != RUNTIME_LOAD_ADDRESS:
        faults.append(
            f"0x{APPLICATION_OFFSET:06x}: load/entry address is not 0x{RUNTIME_LOAD_ADDRESS:08x}; "
            "the bootloader would copy to and jump into the wrong address"
        )
    elif length == 0 or length >= BOOTLOADER_MAX_APPL_SIZE:
        faults.append(
            f"0x{APPLICATION_OFFSET:06x}: application length 0x{length:x} is outside the bootloader's "
            f"accepted range (1 .. 0x{BOOTLOADER_MAX_APPL_SIZE - 1:x}); the application is never started"
        )
    else:
        notes.append("             header is within the range the bootloader accepts")
        if reference is not None:
            on_device = raw[APPLICATION_OFFSET : APPLICATION_OFFSET + len(reference.application)]
            if on_device == reference.application:
                notes.append("             application is byte-identical to the supplied firmware")
            else:
                notes.append("             application differs from the supplied firmware")

    # Flash drive and configuration are not boot critical, but their state
    # explains a cartridge that starts the application and then stops.
    drive_state = region_state(raw, FLASH_DRIVE_OFFSET, 0x1000)
    fat_signature = raw[FLASH_DRIVE_OFFSET + 0x1FE : FLASH_DRIVE_OFFSET + 0x200]
    notes.append(
        f"0x{FLASH_DRIVE_OFFSET:06x}: flash drive first sector is {drive_state}"
        + (", FAT boot signature present" if fat_signature == b"\x55\xaa" else ", no FAT boot signature")
    )
    programmed_config = sum(
        1 for sector in range(0, CONFIG_SIZE, 0x1000)
        if region_state(raw, CONFIG_OFFSET + sector, 0x1000) != "erased"
    )
    notes.append(
        f"0x{CONFIG_OFFSET:06x}: {programmed_config} of {CONFIG_SIZE // 0x1000} configuration sectors hold data"
    )
    return notes, faults


def print_analysis(raw: bytes, reference: RecoveryImage | None) -> list[str]:
    notes, faults = analyse_flash_image(raw, reference)
    print("\nOn-device flash contents:")
    for note in notes:
        print("  " + note)
    if faults:
        print("\nBoot-critical faults found:")
        for fault in faults:
            print("  * " + fault)
    else:
        print("\nNo structural fault found in the FPGA or application headers.")
        print("  This does not prove that the FPGA timing or application behavior is correct.")
        print("  If the cartridge still does not start, investigate the firmware and saved state.")
    return faults


# --- host preflight ----------------------------------------------------------


def usb_devices(vendor: int, product: int) -> list[Path]:
    """Find matching USB devices through sysfs, with no external tools."""
    found: list[Path] = []
    root = Path("/sys/bus/usb/devices")
    if not root.is_dir():
        return found
    for entry in sorted(root.iterdir()):
        try:
            if int((entry / "idVendor").read_text().strip(), 16) != vendor:
                continue
            if int((entry / "idProduct").read_text().strip(), 16) != product:
                continue
        except (OSError, ValueError):
            continue
        found.append(entry)
    return found


def preflight(args: argparse.Namespace) -> int:
    """Check everything that can be checked before a wire is attached."""
    problems: list[str] = []
    print("Host preflight checks (no USB device is opened):\n")

    ecpprog = shutil.which(args.ecpprog)
    if ecpprog is None:
        print(f"  [FAIL] ecpprog not found on PATH as {args.ecpprog!r}")
        problems.append("build and install tools/ecpprog from the 1541ultimate checkout")
    else:
        print(f"  [ ok ] ecpprog: {ecpprog}")
        try:
            linkage = subprocess.run(["ldd", ecpprog], text=True, capture_output=True, check=False).stdout
        except OSError:
            linkage = ""
        libs = [line.split("=>")[0].strip() for line in linkage.splitlines() if "ftdi" in line]
        if not linkage:
            print("  [warn] could not inspect ecpprog's shared libraries with ldd")
        elif not libs:
            print("  [FAIL] ecpprog is not linked against libftdi")
            problems.append("rebuild ecpprog against libftdi or libftdi1")
        else:
            missing = [line for line in linkage.splitlines() if "ftdi" in line and "not found" in line]
            if missing:
                print(f"  [FAIL] ecpprog needs {', '.join(libs)} but it is not installed")
                problems.append("install the libftdi runtime that ecpprog was built against")
            else:
                print(f"  [ ok ] ecpprog is linked against {', '.join(libs)}")

    devices = usb_devices(FT232H_VID, FT232H_PID)
    if not devices:
        print(f"  [FAIL] no USB device with ID {FT232H_VID:04x}:{FT232H_PID:04x} (FT232H) is connected")
        problems.append("connect the FT232H and re-check the cable, hub and host USB port")
    for device in devices:
        try:
            bus = int((device / "busnum").read_text().strip())
            dev = int((device / "devnum").read_text().strip())
        except (OSError, ValueError):
            print(f"  [warn] {device.name}: FT232H present but its bus/device numbers are unreadable")
            continue
        node = Path(f"/dev/bus/usb/{bus:03d}/{dev:03d}")
        print(f"  [ ok ] FT232H at {node}")
        if os.access(node, os.W_OK):
            print(f"  [ ok ] {node} is writable by this user")
        else:
            print(f"  [FAIL] {node} is not writable by this user; ecpprog cannot claim the device")
            problems.append(
                "add a udev rule, for example write this to /etc/udev/rules.d/99-ft232h.rules and "
                "run 'sudo udevadm control --reload-rules && sudo udevadm trigger':\n"
                '         SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", '
                'MODE="0660", GROUP="plugdev", TAG+="uaccess"'
            )
        for interface in sorted(device.glob(f"{device.name}:*")):
            driver = interface / "driver"
            if driver.is_symlink():
                bound = os.path.basename(os.readlink(driver))
                if bound == "ftdi_sio":
                    print(f"  [warn] {interface.name} is bound to the ftdi_sio kernel driver")
                    print("         libftdi detaches it automatically, but only if it can open the device")
                else:
                    print(f"  [ ok ] {interface.name} is bound to {bound}")

    if problems:
        print("\nFix before wiring anything:")
        for problem in problems:
            print("  - " + problem)
        return 1
    print("\nHost preflight passed. Follow README.md's UII+L wiring instructions before probing.")
    return 0


# --- planning output ---------------------------------------------------------


def backup_path(args: argparse.Namespace, output_dir: Path) -> Path:
    if args.backup:
        return Path(args.backup).expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return output_dir / f"u2l-spi-backup-{stamp}.bin"


def estimate_seconds(byte_count: int, divider: int, *, programming: bool) -> int:
    """Rough wall-clock estimate so a slow run is not mistaken for a hang."""
    clock = JTAG_BASE_CLOCK_HZ / divider
    seconds = byte_count * 8 / clock
    # The saved recovery run measured ~61 minutes for 8 MiB at divider 30.
    # USB/MPSSE overhead dominates the raw serial transfer time.
    seconds *= 11
    if programming:
        seconds *= 1.6
    return int(seconds) + 1


def format_duration(seconds: int) -> str:
    if seconds < 90:
        return f"~{seconds} s"
    return f"~{seconds // 60} min"


def print_plan(
    image: RecoveryImage,
    bitstream: Path,
    application: Path,
    flash_drive_erase: Path | None,
    config_erase: Path | None,
    args: argparse.Namespace,
    commands: list[list[str]],
) -> None:
    print(f"Firmware:    {image.firmware}")
    print(f"SHA-256:     {image.firmware_sha256}")
    print(f"ECP5 image:  {format_size(len(image.bitstream))}  sha256={sha256_bytes(image.bitstream)}")
    print(f"U2+L app:    {format_size(len(image.application))}  sha256={sha256_bytes(image.application)}")
    record = image.application_record
    print(
        f"App header:  load=0x{record.load_address:08x} length=0x{record.length:08x} "
        f"entry=0x{record.entry_address:08x} (bootloader limit 0x{BOOTLOADER_MAX_APPL_SIZE:x})"
    )
    print(f"Extracted:   {bitstream}\n             {application}")

    if args.sram:
        print("\nVolatile ECP5 SRAM load; the application is read from the existing SPI flash.")
        print("No flash backup or flash write. A cold boot restores the flashed FPGA image.")
        for command in commands:
            print("  " + shlex.join(command))
        return

    if args.flash:
        heading = "Planned persistent writes (each is erased, programmed and readback-verified):"
    else:
        heading = "Persistent writes that --flash would perform (nothing is written in this mode):"
    print("\n" + heading)
    if args.restore_backup:
        print(f"  0x000000-0x{FLASH_SIZE - 1:06x}: restore the complete SPI image from {args.restore_backup}")
    else:
        bit_span = erase_span(BOOT_FPGA_OFFSET, len(image.bitstream))
        app_span = erase_span(APPLICATION_OFFSET, len(image.application))
        print(f"  0x{BOOT_FPGA_OFFSET:06x}: ECP5 configuration bitstream (erases 0x{bit_span[0]:06x}-0x{bit_span[1] - 1:06x})")
        print(f"  0x{APPLICATION_OFFSET:06x}: Ultimate application container (erases 0x{app_span[0]:06x}-0x{app_span[1] - 1:06x})")
        if flash_drive_erase:
            print(f"  0x{FLASH_DRIVE_OFFSET:06x}-0x{FLASH_DRIVE_LIMIT - 1:06x}: erase ALL flash-drive files")
        if config_erase:
            print(f"  0x{CONFIG_OFFSET:06x}-0x{CONFIG_OFFSET + CONFIG_SIZE - 1:06x}: reset ALL saved configuration")

    clock_khz = JTAG_BASE_CLOCK_HZ / args.clock_divider / 1000
    print(
        f"\nJTAG clock:  {clock_khz:.0f} kHz (6 MHz / {args.clock_divider}); "
        f"allow at least {format_duration(estimate_seconds(FLASH_SIZE, args.clock_divider, programming=False))} "
        "for the mandatory backup alone; write/verify time is additional."
    )

    print("\nBefore connecting the FT232H:")
    print("  D0 -> TCK, D1 -> TDI, D2 <- TDO, D3 -> TMS, GND -> a measured ground pin.")
    print("  See README.md#uiil-ft232h-jtag-wiring for the WiFi-header pin mapping.")
    print("  Confirm ground with a multimeter before applying power.")
    print("  Set FT232H I2C MODE to OFF. Adafruit FT232H D0-D3 outputs are fixed at 3.3 V.")
    print("  Do not connect the FT232H 3V power-output pin; power the U2+L normally through the C64.")
    print("  Power the U2+L first, then plug in the FT232H; unplug in the reverse order.")
    print("  Connect to the ECP5 JTAG header only; do not use the separate 1.8 V DEBUG header.")

    print("\nHardware commands that will run:")
    for command in commands:
        print("  " + shlex.join(command))


# --- hardware operations -----------------------------------------------------


def require_ecpprog(args: argparse.Namespace) -> None:
    if not shutil.which(args.ecpprog):
        raise RuntimeError(
            f"ecpprog not found: {args.ecpprog}. Build/install the bundled "
            "ecpprog program first; see docs/u2pl-jtag.md or run --preflight."
        )
    result = subprocess.run([args.ecpprog, "--help"], text=True, capture_output=True, check=False)
    if "-z" not in result.stdout + result.stderr:
        raise RuntimeError("ecpprog must support the -z IDCODE guard; see docs/u2pl-jtag.md for the tested version")


def take_backup(commands: list[list[str]], backup: Path) -> bytes:
    backup.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the name exclusively before ecpprog opens it for writing.
    with backup.open("xb"):
        pass
    print(f"Backing up the complete {FLASH_SIZE // 1024 // 1024} MiB SPI flash to {backup}...")
    run_checked(commands[1])
    if backup.stat().st_size != FLASH_SIZE:
        raise RuntimeError(f"backup size is {backup.stat().st_size}, expected {FLASH_SIZE}; refusing to write")
    print(f"Backup SHA-256: {sha256_file(backup)}")
    return backup.read_bytes()


def perform_hardware(
    args: argparse.Namespace,
    image: RecoveryImage,
    commands: list[list[str]],
    backup: Path,
    *,
    write: bool,
) -> None:
    require_ecpprog(args)
    print("Probing the ECP5 and SPI flash...")
    print("(the probe clears the ECP5's SRAM configuration, so the cartridge goes dark until")
    print(" it is refreshed or power-cycled; that is expected and is not a new fault)")
    probe_target(commands[0])

    raw = take_backup(commands, backup)
    faults = print_analysis(raw, image)

    # A device that reads back as uniformly zero is not a flash fault; it is a
    # JTAG fault. Writing on top of that would destroy a recoverable cartridge.
    if raw.count(0x00) == len(raw):
        raise RuntimeError(
            "the whole device read back as 0x00, which a working SPI flash never does. "
            "TDO is not being sampled correctly. Refusing to write."
        )

    if not write:
        print("\nFlash backup complete; no SPI bytes were written. The FPGA remains unconfigured.")
        return

    if args.restore_backup:
        print(f"\nRestoring the complete SPI image from {args.restore_backup}...")
        run_checked(commands[2])
        print("Restore complete. Remove JTAG and power-cycle the C64/U2+L.")
        return

    print("\nWriting and verifying the ECP5 boot bitstream...")
    run_checked(commands[2])
    print("Writing and verifying the Ultimate application...")
    run_checked(commands[3])
    next_command = 4
    if args.erase_flash_drive:
        print("Erasing and verifying every byte of the flash drive; all files on it will be lost...")
        run_checked(commands[next_command])
        next_command += 1
    if args.reset_configuration:
        print("Erasing and verifying all saved configuration; the next boot will use defaults...")
        run_checked(commands[next_command])
    print("\nRecovery flash completed and the ECP5 was refreshed.")
    if not faults:
        print("Note: the pre-recovery image had no boot-critical fault, so if the cartridge")
        print("      still does not start, retry with --reset-configuration.")
    print("Remove JTAG, power-cycle the C64/U2+L, and verify the normal boot screen.")


# --- command line ------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("firmware", help="U2+L .u2l updater to recover from")
    mode = parser.add_argument_group("modes (default: inspect the firmware and print the plan)")
    mode.add_argument("--preflight", action="store_true", help="check the host tooling and FT232H access, then stop")
    mode.add_argument("--probe", action="store_true", help="probe FPGA/flash IDs (clears running FPGA configuration), then stop")
    mode.add_argument("--dump", action="store_true", help="probe, back up the whole SPI flash and analyse it; no writes")
    mode.add_argument("--analyse", metavar="IMAGE", help="analyse an existing 8 MiB dump offline; no USB, no writes")
    mode.add_argument("--flash", action="store_true", help="perform the persistent JTAG recovery")
    mode.add_argument("--sram", action="store_true", help="load only the extracted FPGA into volatile SRAM; runs the currently flashed application")
    parser.add_argument("--erase-flash-drive", action="store_true", help="erase all U2+L flash-drive files after core recovery (requires --flash)")
    parser.add_argument("--reset-configuration", action="store_true", help="erase all saved U2+L configuration after core recovery (requires --flash)")
    parser.add_argument("--restore-backup", metavar="IMAGE", help=f"write a previously saved {FLASH_SIZE // 1024 // 1024} MiB image back over the whole device (requires --flash)")
    parser.add_argument("--output-dir", default="jtag-output", help="directory for extracted files and the default flash backup")
    parser.add_argument("--backup", help="path for the mandatory full-SPI backup; must not already exist")
    parser.add_argument("--ecpprog", default="ecpprog", help="ecpprog executable (default: %(default)s)")
    parser.add_argument("--device", default=DEFAULT_VID_PID, help="libftdi device selector (default: Adafruit FT232H %(default)s)")
    parser.add_argument("--clock-divider", type=int, default=30, help="ecpprog JTAG divider; clock is 6 MHz/divider, so 30 is 200 kHz (default: %(default)s)")
    args = parser.parse_args(argv)

    chosen = [name for name in ("preflight", "probe", "dump", "flash", "sram") if getattr(args, name)]
    if args.analyse:
        chosen.append("analyse")
    if len(chosen) > 1:
        parser.error("--preflight, --probe, --dump, --analyse, --sram and --flash are mutually exclusive")
    if not 1 <= args.clock_divider <= 65536:
        parser.error("--clock-divider must be between 1 and 65536 (ecpprog's accepted range)")
    for flag in ("erase_flash_drive", "reset_configuration", "restore_backup"):
        if getattr(args, flag) and not args.flash:
            parser.error(f"--{flag.replace('_', '-')} requires --flash")
    if args.restore_backup and (args.erase_flash_drive or args.reset_configuration):
        parser.error("--restore-backup rewrites the whole device, so it cannot be combined with the erase options")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.preflight:
            return preflight(args)

        image = extract_recovery_image(Path(args.firmware).expanduser())
        check_layout(image)

        if args.analyse:
            path = Path(args.analyse).expanduser().resolve()
            raw = path.read_bytes()
            print(f"Image:       {path}")
            print(f"SHA-256:     {sha256_file(path)}")
            print(f"Reference:   {image.firmware}")
            print_analysis(raw, image)
            return 0

        output_dir = Path(args.output_dir).expanduser().resolve()
        backup = backup_path(args, output_dir)
        if (args.dump or args.flash) and backup.exists():
            raise RuntimeError(f"refusing to overwrite an existing backup: {backup}")
        bitstream, application = write_extracted_files(image, output_dir)
        flash_drive_erase, config_erase = optional_erase_files(args, output_dir)

        if args.restore_backup:
            restore = Path(args.restore_backup).expanduser().resolve()
            if not restore.is_file():
                raise RuntimeError(f"restore image does not exist: {restore}")
            if restore.stat().st_size != FLASH_SIZE:
                raise RuntimeError(
                    f"restore image is {restore.stat().st_size} bytes, expected exactly {FLASH_SIZE}"
                )

        commands = planned_commands(args, bitstream, application, flash_drive_erase, config_erase, backup)
        print_plan(image, bitstream, application, flash_drive_erase, config_erase, args, commands)

        if args.probe or args.sram or args.dump or args.flash:
            # The same adapter is moved between the C64U and cartridge; reuse
            # the existing lock for the whole probe/backup/program sequence.
            with DeviceLock():
                print("\nWARNING: probing clears the running FPGA configuration. A C64 that")
                print("starts afterwards does not prove recovery. Cold boot to restore operation.")
                if args.probe or args.sram:
                    require_ecpprog(args)
                    probe_target(commands[0])
                    if args.sram:
                        run_checked(commands[1])
                        print("SRAM load completed. Confirm cartridge operation; this is not a flash update.")
                    else:
                        print("Probe complete; SPI flash unchanged, FPGA left unconfigured.")
                else:
                    perform_hardware(args, image, commands, backup, write=args.flash)
        else:
            print("\nInspection complete; no USB device was opened and no firmware was written.")
        return 0
    except (FirmwareFormatError, LayoutError, RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
