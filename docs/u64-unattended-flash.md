# Unattended flash of an Ultimate 64 Elite (MK1)

`tooling/flash_u64.py` writes a firmware update to the flash of an Ultimate 64
Elite (MK1) with nobody at the keyboard. It loads the updater over JTAG, answers
the updater's questions by reading their text through gdb, waits for the
updater to switch the machine off, and presses the power button through an
actuator so the machine boots the new firmware. Without an actuator it asks a
person to press the button and carries on once REST answers.

Verified on 2026-10-08 against an Ultimate 64 Elite with the CI build of
1541ultimate master `d5686424f`, with the power button pressed by hand. The
actuator path (`tooling/switchbot_press.py`) has not yet run against hardware.

## Why the power button is needed

The Ultimate 64 Elite has one button, the MultiButton. When the machine is off,
a short press switches it on; that power latch is hardware, and nothing that
runs code is powered while the machine is off. The updater always ends by
switching the machine off (`turn_off()` in
`software/application/update_u2p/update_common.h`). From that point neither
JTAG nor a smart outlet can bring the machine back: the FPGA is unpowered, and
after a loss of input power the latch comes up off. Only a press does.

The Ultimate 64 Elite II and the C64 Ultimate are different: an always-powered
controller owns the power rails there, and they can switch on by themselves.

## What the script does

1. **Reads the `.u64` file.** It is a 12-byte header (load address, length and
   entry, little endian) followed by the raw image. The script wraps the image
   unchanged into an executable ELF and checks that converting the ELF back to
   binary reproduces the image byte for byte.
2. **Takes breakpoint addresses from `--sym-elf`,** the updater ELF of a build of
   the same commit (`target/u64/nios2/updater/result/update.elf`). A CI package
   ships only the `.u64`, so build the same commit locally (`build-tool u64`,
   about two minutes). Two builds of one commit can place data differently, so
   the script compares the code of each breakpoint function between the two
   images, allowing call and data-address instructions to differ, and refuses
   to run when the code does not match.
3. **Loads the updater** with `nios2-download`, leaving the CPU paused (about
   160 s for 5.4 MB), and starts `nios2-gdb-server`.
4. **Prepares the jump into the updater** the way `jump_run()` in
   `software/filetypes/filetype_u2p.cc` does: interrupts off, then the PC at the
   image's entry.
5. **Breaks on** `UserInterface::popup`, `flash_buffer_at`, `update_esp32` and
   `turn_off`. At `popup` it reads the message (`r5`) and the buttons offered
   (`r6`), looks the pair up in a fixed table, and returns the answer from the
   function's entry (`r2` = answer, PC = `ra`), so the popup is never drawn.
6. **Stays attached until the updater has switched the machine off,** then checks
   that ping is silent and the FPGA has left the JTAG chain.
7. **Presses the power button** with `--power-button-cmd` (one retry), or asks a
   person, and waits for `/v1/info`. `--expect-commit` fails the run when the
   reported `git_commit_hash` differs.

## The questions and their answers

Settings and the flash disk are always kept:

| Popup | Buttons | Answer |
|---|---|---|
| Reformat Flash Disk? | YES/NO | NO |
| About to update. Continue? | YES/NO | YES |
| Reset Configuration? (Recommended) | YES/NO | NO |
| Flashing ESP32 Success! | OK | OK |
| Flashing ESP32 Failed! | OK | OK |
| Could not set ESP32 to download mode | OK | OK |

"Reformat Flash Disk?" appears whenever `/Flash` is not empty, which is always
the case on a machine in use. The ESP32 popups appear only when the WiFi
module's version differs from the one in the update, or cannot be read. Error
paths add popups such as "Error reading Flash Disk. Format?" and "Problem with
Flash.. Abort!". Because the sequence is not fixed, a blind key sequence is
unsafe; a wrong YES to the first question erases the flash disk.

Any popup not in the table, or offering different buttons, is left unanswered:
the run stops with exit code 2 and the CPU stays halted at the entry of
`popup()`. Nothing destructive has happened at that point if the popup came
before "About to update. Continue?". After extending the table, `--resume`
attaches to the running gdb-server and carries on from there.

## Running it

```bash
# A build of the same commit as the .u64, for the symbols
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool u64

with-device-locks u64 -- python3 tooling/flash_u64.py \
    --host 192.168.1.13 \
    --u64 update_v3.15-339-gd5686424f.u64 \
    --sym-elf target/u64/nios2/updater/result/update.elf \
    --power-button-cmd "python3 tooling/switchbot_press.py --mac AA:BB:CC:DD:EE:FF" \
    --expect-commit d5686424f
```

The script takes the `u64` device lock itself unless an ancestor process
(`with-device-locks`) already holds it; `FLASH_U64_LOCK=off` disables it for
host experiments. The Intel tools are located the same way as in
`build_and_deploy_u64.sh` (`INTEL_FPGA_ROOT`, `QUARTUS_ROOTDIR`, or the usual
install directories).

A verified run, power button pressed by hand:

```
popup 'Reformat Flash Disk?' -> NO
popup 'About to update. Continue?' -> YES
writing flash at 0x0             (FPGA image, about 45 s)
writing flash at 0x290000        (application, about 20 s)
checking the WiFi module         (versions matched, no popup)
popup 'Reset Configuration? (Recommended)' -> NO
updater finished; it switches the machine off in 5 s
```

## Points that cost time

- `nios2-elf-gdb` 18.1 is built without Python, so the script drives it over
  gdb/MI from outside.
- MI returns register values as hexadecimal strings (`0x3032c5c`); a decimal
  parse reads them as 0.
- `nios2-gdb-server` does not support detach. Quitting gdb while the updater is
  inside `turn_off()` could halt the CPU before it switches the machine off, so
  the script stays attached until the machine is off and then stops the server
  by its PID.
- The `-g` path of `nios2-download` starts the image straight away; the script
  needs it paused so that the breakpoints are in place before the first popup.

## The power button actuator

`tooling/switchbot_press.py` presses a SwitchBot Bot over Bluetooth LE with
[pySwitchbot](https://github.com/sblibs/pySwitchbot) (`pip install
PySwitchbot`). Any command that presses the button once and exits works as
`--power-button-cmd`.

- Keep the press short. On a running machine a press under about 0.8 s opens
  the menu, about 1 s resets the C64, and 4 s or more switches the machine off.
  With the machine off, a short press only switches it on.
- `--hold 5` followed by a normal press is a full power cycle while the FPGA is
  configured, because the 4 s power-off is handled by the FPGA rather than the
  Nios application.
- The Bot body is 43 x 37 x 24 mm and presses with up to about 8 N. It mounts
  beside the button on the case. The button has to be pressed flush with the
  case, so the arm tip needs a nub (a 3 to 5 mm adhesive rubber bumper, or a
  printed arm extender) to reach it.
- The Bluetooth controller must be powered (`bluetoothctl power on`; set
  `AutoEnable=true` under `[Policy]` in `/etc/bluetooth/main.conf` to keep it on).
