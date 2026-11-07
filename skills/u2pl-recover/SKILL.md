---
name: u2pl-recover
description: Recover an Ultimate II+L cartridge that does not boot over ECP5 JTAG with an FT232H, and find the last good and first bad firmware build. Use for "the U2+L is bricked", "the cartridge holds the C64 in reset", "black screen with the U2+L fitted", "a firmware update broke the U2+L", "unbrick the U2+L" or "bisect U2+L builds". Not for a routine update of a cartridge that works; that is the U2+L deploy procedure.
---

# Recover an Ultimate II+L that does not boot

The U2+L has no golden or fallback FPGA partition. A bad image in the boot regions can only be repaired over JTAG. The tool is `tooling/u2pl_jtag.py`; its read-only diagnosis is `tooling/u2pl_jtag_sample.py`. Procedure, wiring, flash map and bisect method: https://github.com/chrisgleissner/1541ultimate-tools/blob/{{TOOLS_REF}}/docs/u2pl-jtag.md. A working cartridge is updated with the `u2pl-flash` skill, as in `tooling/docs/agents/u2pl-deploy.md`.

## Rules

- **A probe is not a repair.** Probing, dumping and every flash write clear the FPGA configuration. The unconfigured cartridge stops holding the C64 in reset, so the screen comes back. A power cycle brings the fault back. Say this to the person as soon as they report a working screen.
- **A JTAG refresh is not a power cycle.** It restarts the boot chain but not the WiFi module, so the network stays down until a cold boot.
- **Never write without a fresh full backup.** The tool takes one first and aborts without it; use a new output directory per attempt.
- Nothing else may use the FT232H at the same time: not `ecpprog` by hand, not another JTAG tool. The tool takes the c64u device lock itself; never delete it. Raw `ecpprog` does not take it.
- Never interrupt an erase, write or verify. Accept a write only after `VERIFY OK` for both regions.
- Use the `ecpprog` revision the document names; the firmware repository's fork lacks the IDCODE guard.
- Flash only when the task asks for it.

## Order of steps

1. Try safe mode first: hold the cartridge's left (reset) button at power-on for several seconds. It helps only if the application starts.
2. Run the tool with no mode flag. It is read-only and prints the plan for the `.u2l`. Add `--preflight` to check the host tools and the adapter.
3. Connect the wires as the document says, with the cartridge powered through the C64 first and the FT232H plugged in after it. Use `--probe`. Expect IDCODE `0x41111043` and JEDEC `EF 40 17`. Stop on anything else or on an unstable chain.
4. Take the full backup with `--dump`. At the default clock an 8 MiB read takes about an hour; run it in the background.
5. After one probe and one backup with no retry, add `--clock-divider 6` for the rest.
6. Diagnose with `u2pl_jtag_sample.py` (exit 0 reset released, 1 held, 2 error). Calibrate it first on an unconfigured FPGA and on a known-bad image. Released alone does not show the application started.
7. To find a breaking build, bisect installable `update_package_*` artifacts of the `test-merge` runs, not commits. Drop builds whose head never landed. Repeat the final good and bad pair once before reporting it.
8. Recover with `--flash` and the known-good `.u2l`. The tool writes with `-p` so the protected lower 2 MiB takes the write.
9. Power the cartridge down before removing the leads, then cold boot. Run the normal `.u2l` updater afterwards to restore the flash-disk assets, WiFi firmware and write protection.
