---
name: u64-flash-unattended
description: Flash firmware onto an Ultimate 64 Elite (MK1, Nios II) without anyone at the keyboard. The updater is loaded over JTAG, its popups are answered by their text, and a power button actuator or a person switches the machine on afterwards. Use for "flash the U64", "install this update.u64 on the Elite", "update the U64 firmware unattended" or "drive the U64 updater safely". Not for the C64 Ultimate or Elite II, or for the U2+L.
---

# Flash an Ultimate 64 Elite unattended

`tooling/flash_u64.py` does the whole flow. How it works, the answer table and the pitfalls: https://github.com/chrisgleissner/1541ultimate-tools/blob/{{TOOLS_REF}}/docs/u64-unattended-flash.md. Flashing is persistent. Do it only when the task asks, on a board nobody else is using. The tool takes the u64 device lock itself; never delete or bypass it. To keep other commands away from the adapter between the build and the flash, run it as `python3 tooling/with_device_locks.py u64 -- python3 tooling/flash_u64.py ...`.

## Rules

- The answers are fixed to keep everything: Reformat Flash Disk NO, Continue YES, Reset Configuration NO, ESP32 popups OK. Never add a YES for Reformat or Reset Configuration unless the person asks. A blind `y` once wiped a flash disk.
- An unknown popup stops the run with exit code 2 and the CPU halted at `popup()`. Read the text. Extend the answer table only when the updater source makes the right answer clear, then run again with `--resume`. Otherwise ask.
- Never send blind keys to the updater. The popup sequence depends on the board's state.
- Prove the flash changed something: flash a build whose commit differs from the `git_commit_hash` that `/v1/info` reports now, and pass `--expect-commit`.
- Afterwards the board runs the flashed firmware. A branch that was loaded over JTAG earlier is gone; load it again if it is still needed.
- Do not reconfigure the FPGA with `quartus_pgm` to rescue a failed run without asking first: it replaces the core until the next power cycle.

## Order of steps

1. Get the `.u64` image and an updater ELF of the same commit. A CI package holds only the `.u64`, so build that commit locally for the symbols: `BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool --repo-dir <worktree> --no-submodule-update --build-tools-dir <dir> u64`. The `u64` target needs Quartus and Nios II from that directory (or `BUILD_TOOL_TOOLS_DIR`); `$HOME` is not mounted. The ELF is `target/u64/nios2/updater/result/update.elf`. For a local build, `update.app` in the same directory is the image.
2. Check the board answers on `$U64_HOST` (`/v1/info`), a USB-Blaster is visible to `jtagconfig`, and nothing listens on gdb port 2342.
3. Run in the background and watch the log. The JTAG load alone takes about 160 s:
   ```bash
   python3 tooling/flash_u64.py --host "$U64_HOST" --u64 <file.u64> \
       --sym-elf <update.elf> --expect-commit <short hash> \
       [--power-button-cmd "python3 tooling/switchbot_press.py --mac <MAC> --hold 0"]
   ```
   Without `--power-button-cmd` the tool asks a person to press the power button and waits.
4. Verify afterwards: `/v1/info` shows the new hash, settings are intact, and `/Flash` still holds `roms`, `html` and `config`.

## If it stops

- Before "About to update": nothing was written. Power cycle, or load the application again with `tooling/build_and_deploy_u64.sh`.
- Checksum error in the updater: it halts without writing; recover the same way.
- A failed write in the middle: ask before trying anything, because the board may need a volatile `external/u64.sof` load first. See `tooling/docs/agents/jtag-debug.md` for the recovery ladder.
- `jtagconfig` reporting "Can't scan JTAG chain. Error code 36": stop `jtagd` by its PID and try again.
