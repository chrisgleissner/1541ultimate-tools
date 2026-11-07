---
name: u2pl-flash
description: Update the firmware of a working Ultimate II+L cartridge that is plugged into a C64 Ultimate, by uploading a .u2l and driving the cartridge's updater menu over REST with keys sent through the C64 Ultimate. Use for "update the U2+L", "flash this .u2l onto the cartridge", "install the new U2+L firmware" or "deploy U2+L firmware". Only for a cartridge that boots and answers REST; one that does not boot needs the u2pl-recover skill.
---

# Update a working Ultimate II+L through the C64 Ultimate

`./build-tool --u2-flash` runs `tooling/u2_flash.py`. The procedure, the flags, the exit codes and the checks afterwards are in `tooling/docs/agents/u2pl-deploy.md`; read it before the first run. A cartridge that does not boot, holds the C64 in reset or shows a black screen is not a case for this skill: use the `u2pl-recover` skill.

## Rules

- A flash is persistent, and the last step restarts the C64 Ultimate. Run it only when the task asks, and not while another person or tool uses either device. Pass `--confirm-flash` and `--confirm-power-cycle` only when the task authorises the flash and the restart.
- The tool takes the c64u device lock itself and refuses when another tool holds it. Never delete or bypass the lock. Use `--wait SECONDS` to wait for it, or run the whole command under `python3 tooling/with_device_locks.py c64u -- ...`.
- There are no default hosts. Take the cartridge and C64 Ultimate hosts from the person running the bench, and pass them as `--host` and `--c64-host` or in `U2_FLASH_HOST` and `U2_FLASH_C64_HOST`. Never write them into a tracked file.
- Leave the Reformat Flash Disk, Reset Configuration and WiFi module answers at No unless the person asks for Yes. The tool answers by the popup's text.
- Exit code 2 means a flash may be running or its state is unknown. Do not retry and do not power-cycle. Look at the devices by hand and report.
- Never send keys to the cartridge by hand. They must go through the C64 Ultimate's `machine:input`, and only while its own menu is closed.
- Every upload needs a new `--git-id`. The tool never replaces a file on the cartridge.

## Order of steps

1. Get the `.u2l`. The `update_package_*` artifact of a CI run for the wanted commit carries the current FPGA bitstream. A software-only local build reuses whichever bitstream is checked in, which can be older.
2. Check the artifact without touching a device: `./build-tool --u2-flash --dry-run --artifact <file>`.
3. List the cartridge's FTP root to find its USB volume, and pass `--dest-root /<volume>/Firmware/latest`. Use a short `--branch` name and a unique `--git-id`.
4. Check that the C64 Ultimate is running its normal firmware, with its own menu closed.
5. Run the flash:
   ```bash
   ./build-tool --u2-flash --host "$U2_HOST" --c64-host "$C64U_HOST" \
     --dest-root /<volume>/Firmware/latest --branch <short-branch> --git-id <unique-id> \
     --artifact <file> --confirm-flash --confirm-power-cycle
   ```
   Without `--confirm-flash` the tool navigates up to `Run Update`, backs out, and exits with 1. That run also consumes the `--git-id`.
6. After the restart, allow up to two minutes. `/v1/info` of the cartridge must show the new `git_commit_hash`, and the jiffy clock at `$00A2` of the C64 Ultimate must change between two reads. `fpga_version` alone is not a health signal.
7. If the tool reports that it could not drive the power cycle, the flash itself is complete. Power-cycle the C64 Ultimate by hand when the task authorises it.
