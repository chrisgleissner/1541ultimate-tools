---
name: c64u-jtag-run
description: Run a 1541ultimate firmware build, or an upstream pull request, on a C64 Ultimate or Ultimate 64 Elite II over FT232H JTAG. The code loads into RAM and is verified over REST; flash is never written. Use for "run PR 123 on the C64U", "try this branch on the C64 Ultimate", "JTAG-load the u64ii build", "probe the C64U JTAG adapter", or "get the C64U back to its flashed firmware".
---

# Run a build on a C64 Ultimate over JTAG

Everything loaded here is volatile: a power cycle returns the board to its flashed firmware. The tools take the c64u device lock themselves; never delete or bypass it, and never run another JTAG program on the adapter at the same time. To hold the lock across several commands, start them as `python3 tooling/with_device_locks.py c64u -- COMMAND`; the tools run inside it without waiting. A shutdown, power cycle or flash needs the current task to ask for it.

The procedure, wiring and recovery are in https://github.com/chrisgleissner/1541ultimate-tools/blob/{{TOOLS_REF}}/docs/c64u-jtag.md. Read the section for the step you are on. The host is `$C64U_HOST`, from the environment or `.build-tool.env`.

## Order of steps

1. Ask before loading if another person or another firmware setup may be using the board. `tooling/c64u_mode.py status` shows what runs.
2. Power the FT232H over USB before the C64U is switched on, and never leave it on header P5 unpowered while the machine runs. An unpowered adapter once gave a rolling HDMI picture at boot.
3. Probe first, and again after any wiring change: `tooling/u64ii_jtag.sh probe`. Continue only with IDCODE `0x?362C093` (50T) or `0x?3631093` (100T) and user ID `0xDEAD1541`.
4. Start `tooling/c64u_monitor.py` and watch its ALERT and WARN lines. Stop it with `--stop`, never `pkill -f`.
5. Build and load, with the REST check enabled:
   - Current tree: `U64II_VERIFY_HOST=$C64U_HOST ./build-tool --jtag c64u`
   - Upstream PR: `U64II_VERIFY_HOST=$C64U_HOST ./build-tool --apply-pr N --pr-base <remote>/master --jtag c64u`, where `<remote>` points at GideonZ/1541ultimate. It builds a throwaway worktree beside the checkout; remove it afterwards with `git worktree remove`.
6. A C64U needs the system ROM step from the document before the Ultimate build shows READY. Set the ROM names in RAM only; never save the configuration to flash.
7. Verify with the REST check and the READY screen. A PR worktree reports its base commit in `/v1/info`, so identify the build by the feature under test.

## Rules for the commands

- `run` is volatile. It reloads the FPGA, writes the application to RAM and starts it.
- `reset` restarts the CPU and boots whichever application is in flash. That application must suit the FPGA design currently loaded: after `run` with a recovery or foreign bitstream, `reset` starts code that crashes. Leave that state with `fpga`, or by a power cycle.
- `fpga` loads the bitstream from `external/`, and the flashed application then boots.

## Before any power cycle

Only when the task authorises it:

1. Read the board's `Power On After Power Loss` setting with `tooling/c64u_mode.py check-power`. If it is not On, the board stays off after the power returns; do not cut power.
2. Shut down over REST first (`PUT /v1/machine:poweroff`) when REST answers.
3. Switch only an outlet that feeds this board alone, never one that also powers other devices. `c64u_mode.py` needs `--allow-power-cycle`.
4. Wait for REST to answer again, then run the checks of step 7.
