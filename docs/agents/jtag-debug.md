# Debugging a wedged U64 with JTAG gdb

Use this when a U64 (Nios II) stops answering. Do not guess. The U64 build has no JTAG UART, so adding `printf` is wasted effort. JTAG gdb halts the CPU immediately, even on a device that answers neither HTTP nor ICMP. In one investigation it found two root causes within minutes, after six plausible hypotheses had each been built, deployed and refuted by black-box probing over many hours.

For the C64 Ultimate and Ultimate 64 Elite II (RISC-V, no halt possible), use `tooling/u64ii_gdb.sh` instead; it reads memory and task state while the CPU runs. See `docs/c64u-jtag.md` in the tools repository, section "Debugging with gdb".

## Attach

1. Start `nios2-gdb-server --tcpport 2342 --tcppersist` as a real background process. A `setsid` or `nohup` subshell fails silently.
2. Run `nios2-elf-gdb -q -batch -x script.gdb target/u64/nios2/ultimate/result/ultimate.elf`, with `target remote :2342` as the first line of the script. The ELF carries full symbols (about 9 MB).
3. Read the server banner. `Processor is already paused` means the CPU stopped on a fatal exception. `Pausing target processor: OK` means it was running, so a task is blocked and nothing crashed.

## Reading the state

- **Whole device dead.** The backtrace shows `soft_exceptions ()` at `port_asm.S` executing `break` (an unimplemented instruction). Frame 1 and `ra` name the culprit. Causes seen: a virtual call through a stale vtable (an object disposed while still on the UI object stack), and a smashed return address. A return address made of ASCII bytes (`0x0064656d` is "med") means a string overran a stack buffer; look for an unbounded `strcpy` into a fixed array in that frame.
- **REST alive but the menu dead.** `xSchedulerRunning=1`, `xTickCount` advancing, `pxCurrentTCB->pcTaskName` equal to "IDLE" and every task in a normal blocked list mean nothing crashed. A task is stuck in a modal loop. The UI task is named "U-II Main".
- **Enumerate tasks.** Walk `pxReadyTasksLists[]`, `pxDelayedTaskList`, `pxOverflowDelayedTaskList` and `xSuspendedTaskList`. Each item's `pvOwner` is the `TCB_t *`. `configMAX_PRIORITIES` is a macro, not a symbol, so size the array with `sizeof(pxReadyTasksLists)/sizeof(pxReadyTasksLists[0])`.
- **Unwind a blocked task.** Set `$s0` to its `pxTopOfStack`. The trap frame saved by `portSAVE_CONTEXT` (`software/FreeRTOS/Source/portable/nios/port_asm.S`) holds `ra` at +0, the PC at +72, `r16`-`r23` at +76 to +104, `gp` at +108 and `fp` at +112. Load those, set `$sp = $s0 + 116`, then `bt` unwinds the real stack from DWARF.

## Recovery ladder, in order

1. `nios2-download -g target/u64/nios2/ultimate/result/ultimate.elf` (about 35 s) clears most hard wedges.
2. Only if it reports "Pausing target processor: not responding": load the FPGA volatilely with `quartus_pgm -c "USB-Blaster [<n>]" -m JTAG -o "p;external/u64.sof"`, then redeploy. Say so first. This replaces the flashed FPGA core until the next power cycle, and the device's core may carry fixes that the repository's `.sof` does not.
3. Last resort: remove power physically. The soft power button is not enough.

Do not call a device wedged from REST alone. Check ICMP too, and remember that `machine:reset` can make REST briefly unreachable. A genuine halt does not self-recover; 150 s of polling settles it.

Do not reconfigure the FPGA as part of the normal deploy loop. See `docs/u64-jtag-deploy.md` in the tools repository.
