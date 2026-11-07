# Firmware lessons for the monitor, freezer and shared code

Read this before touching the machine code monitor, the freezer, or code compiled into the updater.

## Freezer and monitor memory

- While frozen, treat C64 `$0400-$0FFF` as live UI memory. `C64::backup_io()` installs the freezer screen at `$0400-$07FF` and the firmware charset at `$0800-$0FFF`, so a live write there corrupts the UI at once.
- Frozen monitor reads and writes must use the saved buffers consistently. For U64 monitor access, a write to frozen `$0400-$07FF` goes only to `screen_backup`, and a write to frozen `$0800-$0FFF` goes only to `ram_backup`.
- Persistent charset corruption does not come from popup drawing past the border: `Window` drawing clamps to the monitor window. If corruption survives redraws, inspect the backing memory write path.
- Corruption bytes that look like opcodes mean a commit path aliases into display memory. Compare the damaged bytes with the opcode that was written.
- Add a focused host regression for any freezer aliasing fix: the intended frozen byte changes and the live `$0800` byte stays untouched.

## Monitor `G` (go) on the U64

- Defer execution until after `MachineMonitor::deinit()` and object destruction. Jumping while the monitor or freezer UI still owns the machine leaves the UI half-alive.
- Do not use cartridge or boot-cart jump paths when SYS-like behaviour is needed. They execute code but disturb the VIC state and can black the screen.
- The working handoff is a live-machine NMI trampoline (`software/monitor/monitor_file_io.cc`): release freezer ownership, write a small trampoline into the cassette buffer at `$033C`, point `NMINV` (`$0318/$0319`) at it, pulse `C64_MODE_NMI`, and let the trampoline restore `NMINV` before it jumps to the target.

## Which code the target compiles

- For `RECOVERYAPP` targets (U2+ and U64 updaters), shared C64 or UI code must not reference machine-monitor entry points unless the target also links `run_machine_monitor.cc`, `monitor_init.cc` and a backend. Updater builds omit those, so an unguarded hook fails at link time.
- `./build` does not cover `u2plus` or `u2pl`. For shared code used by the U2+ Nios images, run `make u2plus_swonly`, or at least `make -C target/u2plus/nios/recovery`.
- The firmware builds `software/httpd/c-version/lib`, selected in `target/common/environment.mk`. The `FreeRTOS/lib` port looks like the right one and is never compiled, so edits there are silently discarded by a successful build. Check with `grep -n httpd target/common/environment.mk`, and prove a change was emitted with `nios2-elf-objdump -dr` on the object.

## Waiting on a blocking call

A test wait shorter than the firmware call's own blocking budget reads as a failure of the firmware. Size the wait to the full budget. Do not add reset-and-retry loops to hide intermittent misses; they hide real defects and degrade the device's network stack over repeated reset cycles. Recover a degraded device with a fresh JTAG deploy.
