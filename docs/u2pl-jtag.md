# UII+L backup, deployment and recovery over FT232H JTAG

`tooling/u2pl_jtag.py` programs the Ultimate II+L's Lattice ECP5 through a
normal Adafruit FT232H. It extracts the FPGA and application from a `.u2l`
updater, validates their layout, backs up the complete 8 MiB SPI flash, then
programs and verifies the two boot regions. Python 3.10 or later and `ecpprog`
are required; pyftdi and Diamond are not required for this tool.

This is the recovered `u2l_jtag_flash.py` workflow used locally in August and
September 2026, with its original parser tests and additional host tests. Saved
recovery logs and an 8 MiB dump establish previous use on the cartridge. This
integration has been tested offline; the cartridge was disconnected, so it has
not been tested again on hardware. No firmware or personal backups are shipped.

Use the [existing UII+L wiring guide](../README.md#uiil-ft232h-jtag-wiring).
The C64U/Elite II Artix tools and the Ultimate 64 Cyclone tools address different
FPGAs. `build-tool --jtag u2pl` is not implemented: use this explicit standalone
workflow after building or obtaining the intended updater.

## Important behavior

Even `--probe` and `--dump` **clear the running FPGA configuration**: `ecpprog`
uses `ISC_ENABLE`, `ISC_ERASE` and `ISC_DISABLE` to access SPI flash through
JTAG. They leave SPI contents unchanged, but interrupt the cartridge. If the
C64 screen comes back after a probe, that does not demonstrate a repair: the
unconfigured cartridge has stopped holding the host in reset. Cold boot to
reload the flashed FPGA and restart the cartridge and its WiFi module.

The final flash write refreshes the ECP5 from SPI. This restarts its boot chain
but does not power-cycle the ESP32. Verify recovery after a cold boot; network
availability immediately after a JTAG refresh is not a reliable boot test.

Try button-held safe mode before JTAG recovery: hold the cartridge's left
(reset) button at power-on for several seconds. On firmware with the checks
in `software/components/config.cc` and `software/filesystem/blockdev_flash.cc`,
this uses default settings and skips mounting the flash disk. It helps only
if the application starts. The cartridge has no separate golden FPGA boot
partition: an unusable boot bitstream requires external recovery access.

The tool takes the existing `c64u.lock` under `DEVICE_LOCK_DIR` (default
`/tmp/1541ultimate-device-locks`) across every hardware sequence, because the
same adapter can be moved between host and cartridge. This serializes it with
the repository's C64U tools. Raw `ecpprog` and other programs do not take this
lock; stop those before running the tool. The inherited `U64II_JTAG_LOCK=off`
setting disables it and should only be used by a coordinating caller.

## Install the programmer

The installed binary used successfully on this machine matches the local
[gregdavill/ecpprog](https://github.com/gregdavill/ecpprog) checkout at
`1931c3e121f682536a9d80ca1ce1b651c11aef76`. Use that version to reproduce the
workflow. The firmware repository's `GideonZ/ecpprog` submodule is a different
fork: it lacks the `-z` IDCODE guard required by this wrapper.

```bash
sudo apt-get install build-essential pkg-config libftdi1-dev
git clone https://github.com/gregdavill/ecpprog.git ecpprog
git -C ecpprog checkout 1931c3e121f682536a9d80ca1ce1b651c11aef76
make -C ecpprog/ecpprog
sudo make -C ecpprog/ecpprog install
```

Use a fresh clone directory. `--ecpprog /absolute/path/to/ecpprog` selects a
binary without installing it. `--preflight` examines linkage, sysfs enumeration
and USB-node access without opening the adapter:

```bash
tooling/u2pl_jtag.py known-good.u2l --preflight
```

The firmware argument is a placeholder in preflight mode and need not exist.
For Linux USB access, use the same FT232H permissions setup documented in
[the C64U wiring/setup section](c64u-jtag.md#wiring). The tool also prints a udev-rule
suggestion if the USB node is not writable. Preflight does not establish that
JTAG is wired correctly or that the cartridge can be recovered.

The default selector is `i:0x0403:0x6014:0`, interface A. With several adapters,
select the intended FT232H explicitly, for example
`--device s:0x0403:0x6014:SERIAL`. This is libftdi syntax, not a pyftdi URL.

## Inspect, probe and back up

First validate a known-good `.u2l` offline. Default mode opens no USB device:

```bash
tooling/u2pl_jtag.py known-good.u2l --output-dir recovery-plan
```

It extracts `u2l-runtime.bit` and `u2l-ultimate.app`, prints their SHA-256
digests, checks their target and container layout, and prints the commands that
`--flash` would run. Existing extracted files are reused only when identical;
use a new output directory for another package. A `.u2l` is an executable
updater containing several objects, not a raw SPI image.

With the wiring checked and cartridge powered, probe and make a standalone
backup. These commands interrupt the running FPGA as described above:

```bash
tooling/u2pl_jtag.py known-good.u2l --probe --output-dir recovery-plan
tooling/u2pl_jtag.py known-good.u2l --dump --output-dir first-backup
```

Expected IDCODE: `0x41111043` (`LFE5U-25`). Supported flash IDs are
`EF 40 17` (Winbond W25Q64) and `01 40 17` (Spansion/Cypress S25FL164K).
Other targets or capacities abort before persistent writes. `ecpprog -z`
rejects unsupported FPGA families before SPI access; the wrapper then checks
the exact UII+L ID and capacity. It is still essential to connect to the
intended cartridge.

Backups use timestamped filenames, refuse overwrite, and must be exactly
8 MiB. Copy the backup and its printed SHA-256 off the working machine.
`--backup /path/to/new.bin` chooses the backup filename. An all-zero readback
aborts recovery; an all-erased readback is reported and can be recovered if
the hardware connection is otherwise sound.

At the default divider 30 (200 kHz), a saved run measured approximately
**61 minutes for an 8 MiB backup**. Earlier local documentation's estimate of
12 minutes was incorrect. After a clean probe and full backup, divider 6
(1 MHz) was used successfully on flying leads; measured write-and-verify
times were about 32 s for the FPGA and 54 s for the application. These are
historical measurements, not guarantees for another wiring setup. Pass
`--clock-divider 6` explicitly to use that speed. Every persistent run still
takes a fresh full backup; the tool provides no skip-backup switch.

Analyse a saved dump without USB:

```bash
tooling/u2pl_jtag.py known-good.u2l --analyse first-backup/u2l-spi-backup-TIMESTAMP.bin
```

Analysis checks headers and compares the boot images against the reference.
Valid headers alone do not prove timing closure, a functional application, or
successful cartridge startup.

## Volatile experiment or persistent recovery

To try the extracted FPGA without writing SPI flash:

```bash
tooling/u2pl_jtag.py candidate.u2l --sram --output-dir sram-experiment
```

This probes the cartridge and runs `ecpprog -S` on the extracted FPGA. It
does not load the candidate application into RAM: the FPGA bootloader reads
the application already in SPI flash. Select a compatible FPGA/application
pair. No backup is taken because no SPI contents are written. A cold boot
returns to the flashed FPGA. This mode is supported by the recovered programmer;
the new wrapper path is covered by host tests and has not been retested on hardware.
The wrapper requires the final ECP5 status to report `DONE` without configuration
errors; the programmer's zero exit status alone does not establish this.

To deploy the two boot images persistently, use a new output directory:

```bash
tooling/u2pl_jtag.py known-good.u2l --flash --output-dir recovery-run
```

The sequence is probe, full backup, FPGA write and verification, application
write and verification, then FPGA refresh. Every write uses `-p` to clear the
lower 2 MiB write protection normally set by the updater. Failed commands,
protection warnings, verification mismatches or missing `VERIFY OK` abort the
sequence. Do not disconnect power, USB or ground while it runs.

Normal recovery preserves flash-disk files and settings. Only after testing
the core recovery should you consider `--flash --reset-configuration` or
`--flash --erase-flash-drive`: these explicitly erase settings or files,
respectively, and still take a full backup first. Erasing the disk does not
reinstall its ROMs and web assets. Use a new output directory for each attempt.

To restore an entire saved 8 MiB image, including its files and settings:

```bash
tooling/u2pl_jtag.py known-good.u2l --flash \
  --restore-backup /path/to/old-backup.bin --output-dir restore-run
```

Restoration first backs up the current cartridge, then writes and verifies
the entire supplied image. The reference `.u2l` must still validate. A full
restore cannot be combined with either optional erase.

After a successful recovery, power down before removing leads and cold boot
the cartridge. Confirm normal menu and application operation. Run the normal
`.u2l` updater afterwards to install the package's flash-disk assets and WiFi
firmware and reapply SPI write protection. JTAG recovery alone does not update
the ESP32.

## Flash layout and tests

The addresses come from `flash_addresses_u2pl[]` in
`software/io/flash/w25q_flash.cc`. All boot objects are written raw, without a
16-byte flash version header:

| Region | Start | Limit (exclusive) |
| --- | --- | --- |
| ECP5 boot image | `0x000000` | `0x0A0000` (application start) |
| Application slot | `0x0A0000` | `0x200000` |
| Flash disk | `0x200000` | `0x7F0000` |
| Configuration | `0x7F0000` | `0x800000` |

The firmware's BOOTFPGA table entry nominally allows `0x0C0000` bytes, which
overlaps the application start; this tool deliberately limits FPGA writes
to `0x0A0000`. It checks the actual 64 KiB rounded erase spans. The application
has a 12-byte load/length/entry header, with payload length strictly below
`0x140000` as required by `software/portable/riscv/bootloader.c`. Embedded
objects must obey the 16-byte alignment in `update_u2pl/update_binaries.s`.

`tooling/test_u2pl_jtag.py` covers container validation, flash boundaries,
target checks, backup gates, programmer failures and command sequencing without
USB. The repository's installation tests exercise the installed entry point;
upstream contract tests check flash-map facts against a firmware checkout.
See [the build guide](u2pl-build.md) for FPGA synthesis and artifact validation.

## Diagnose startup and bisect packages

The local `unbrick-u2` skill also preserved a boundary-scan diagnostic, now
`tooling/u2pl_jtag_sample.py`. It reuses this repository's tested MPSSE/TAP
implementation with ECP5's eight-bit instruction framing. Unlike `ecpprog -t`,
normal sampling does not clear the FPGA. `--refresh` explicitly reloads it
from SPI, interrupts the cartridge, and waits 12 seconds before sampling.

The sampler needs pyftdi 0.57.2, the same version as the C64U tools. Use an
interpreter that already provides it, or create a virtual environment:

```bash
python3 -m venv .u2pl-jtag-venv
.u2pl-jtag-venv/bin/pip install pyftdi==0.57.2
.u2pl-jtag-venv/bin/python tooling/u2pl_jtag_sample.py --refresh --label known-good
```

`--url` takes a pyftdi URL; `--frequency` is Hz. These differ from the recovery
tool's libftdi `--device` and integer `--clock-divider`. Both tools hold the
same adapter lock, and the sampler checks the exact IDCODE before issuing any
ECP5 instruction. It releases the adapter pins as inputs when finished.

The diagnostic observes `SLOT_RSTn`, ECP5 ball R3, through cell 352 of the
409-bit boundary register, using `SAMPLE` (`0x1c`) and returning to `IDCODE`
(`0xe0`). It never issues `EXTEST` or `PRELOAD`, which could drive the pins.
Exit 0 means reset released, 1 means reset held, and 2 means a tool/connection
error. Calibrate before trusting a bisect: an unconfigured FPGA should release
reset, while a known startup failure with a configured FPGA should hold it.
Reset released alone does not prove application startup: an absent or
unconfigured cartridge also releases reset. Check a known-good boot and the
visible cartridge behavior as controls.

The boot chain is SPI FPGA configuration → embedded RISC-V boot ROM → DDR2
calibration → application record at `0x0A0000` → application initialization
and C64 reset release. IDCODE, flash JEDEC ID and FPGA `DONE` identify earlier
stages; they do not prove application initialization completed. Repeatable LED
activity suggests configuration loaded, but is not sufficient to identify the
failing stage. Release images need not provide the development User-JTAG
`0xDEAD1541` endpoint, so do not expect the Artix application tools to work.

To bisect a startup regression:

1. Keep a known-good package and full SPI backup. Establish that the known-good
   package cold-boots on the actual cartridge/host pair.
2. Fetch the firmware branch and list its successful Actions runs with
   `gh run list --repo GideonZ/1541ultimate --branch test-merge --limit 100`.
   Select installable `update_package_*` artifacts. Branch-filtered run lists
   can include PR heads that never landed: use
   `git merge-base --is-ancestor CANDIDATE_SHA BAD_SHA` and order candidates
   with `git log --first-parent`. Version counts and filenames can collide;
   record full source SHA, Actions run ID and package SHA-256.
3. Inspect each `.u2l` into a separate output directory. Group candidates by
   extracted FPGA SHA-256. Match the original dump's FPGA bytes and the
   `12 + length` application bytes at `0x0A0000` against the extracted objects
   to identify the flashed build exactly. Cached bitstreams are untracked;
   a source commit alone does not establish the FPGA bytes in a package.
4. Use `--sram` to test candidate FPGA compatibility with the current flashed
   application. A result applies to that pair; it does not establish that
   every package with that FPGA and a different application has the same fault.
   Sample reset without `--refresh`, which would undo the SRAM experiment.
5. For a full-package test use `--flash`, including its mandatory backup and
   verification. Sample after startup and cold boot for network tests. Record
   pass, fail, or tool error separately; a failed scan is not a failing firmware.
   Repeat the final red/green boundary before concluding, then restore the
   known-good package and confirm a cold boot.

The local kit also had `bisect_test.sh` and `flash_app.sh`. They bypassed the
fresh-backup gate and used machine-specific paths and cached state, so their
raw write shortcuts are not shipped. Use the validated wrapper for writes.
The diagnostic and method above retain the useful observations.

An older recovery investigation found an application hang caused by RISC-V
`FENCE` instructions emitted by `std::atomic`: older rvlite FPGA decoders did
not implement that opcode. This is a **version-dependent historical failure**,
not a claim that every UII+L rejects `FENCE`; the firmware source inspected for
this integration includes `when 3 => -- FENCE` handling. For an old package,
inspect the actual bitstream's source provenance and disassemble its application
with `riscv32-unknown-elf-objdump -d ultimate.elf`. A new decoder in source does
not change an older cached bitstream embedded in the updater.

For deeper startup diagnosis, the historical kit instrumented application
initializers to blink ordering markers on the reset line and sampled them
through boundary scan. That requires deliberately modified firmware and is
not a normal recovery operation. If markers continue toggling, interrupts
are still running; do not infer an interrupts-disabled trap from a dark menu
alone. Reproduce a known-good and known-bad control before interpreting such
instrumentation.
