# Device invariants after a green suite

A suite that reports every check OK can still leave the device broken for the next suite. One debugger suite passed all of its checks while leaving the U64's volatile KERNAL image corrupted at `$E000`. The failure surfaced later as an unrelated blocked ROM cell in another suite.

## Rules

- **Check an invariant after each suite, not only its verdict.** On the U64 the KERNAL entry that the ROM lanes need is bytes `85 56 20 0F BC` at `$E000`. A suite that changes it has a defect, whatever its result.
- **Bisect across suites before within one.** Probe the invariant after each suite in sequence. The failing suite is often only the first one to notice damage that an earlier suite did.
- **Take a guard's baseline from a known-good constant, never from the device.** A baseline sampled at the start compares a corrupt image with itself and stays silent.
- **Leave the machine in a known state.** A suite must not leave a program running. A fixture that keeps toggling banking makes every later ROM read a coin flip. A check that depends on banking re-establishes it before it believes what it read.

## Reading C64 memory through REST or DMA

- `machine:readmem` is served through the live 6510 memory map. A read of `$E000` returns the KERNAL only while the KERNAL is banked in.
- To tell ROM from RAM, write a distinctive byte to the address and read it back. If the write does not stick, the read came from ROM. If it sticks, you read RAM.
- Reads of `$0000` and `$0001` over DMA return a stale mirror. They are not evidence of the live CPU port.
- With the machine at the boot screen, a correct BASIC at `$A000` and a wrong `$E000` means real damage, not banking.

## Tracing writers

"Zero writes traced" only rules out the writers you traced. The writers that can reach a ROM window on the U64 are in `software/u64/u64_machine.cc` (`poke`, `poke_cpu`, `poke_visible`, `write_frozen_byte`, `write_visible_byte`) and the generic `C64::poke`. Instrument all of them. Missing `write_frozen_byte`, the most used path, sent one investigation down a wrong path.

## Which write primitive

The U64 memory primitives do not behave alike. They differ in two settings: `C64_DMA_MEMONLY` (memory only, or the live map) and `SERVE_WHILE_STOPPED` (access is served through the live memory map). When the live map is used, a write into a ROM window while that ROM is banked in reaches the volatile ROM image, not the RAM below it.

- Choose the primitive by intent. A write meaning "put this byte in RAM" (debug patch on RAM under ROM, fixture, restore) must not go through the live map. Otherwise it silently edits the KERNAL.
- Read with the matching primitive, or a patch saves one byte and restores it somewhere else.
- Read `software/u64/u64_machine.cc` for what each primitive sets before using it. Names differ between branches.

## Firmware log without a JTAG UART

The U64 build has no JTAG UART, so firmware `printf` cannot be read over JTAG. Set `Network Settings -> Log to Syslog Server` to `<dev-host>:5514` and bind a UDP listener, or run `./run-tests -o DIR --syslog <target>`. This is much cheaper than a flash cycle per hypothesis. Put the setting back when finished.
