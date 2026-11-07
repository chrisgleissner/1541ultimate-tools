---
name: vhdl-analysis
description: Read the 1541ultimate VHDL to answer what the hardware does for firmware, with file and process evidence. Covers interrupt delivery, stop and stall behaviour, register decodes and clock-domain crossings. Use for "what does the FPGA do when...", "is this NMI or IRQ lost in hardware", "check the RTL before changing the firmware", or when a firmware bug theory depends on hardware behaviour.
---

# Analyse VHDL for behaviour the firmware can see

The result is a mechanism with evidence, not a plausible story. Cite the file, the entity or process and its enable, by name; line numbers drift. Say when you infer instead of read. The sources are under `fpga/`; for a shipped bitstream, check that you read the source it was built from.

## Method

1. **Find the right source and its generics.** `target/fpga/ultimate_logic.inc` and the `ultimate_logic*.inc` files beside it list the sources of a build. Read generics at the instantiation. In `fpga/cart_slot/vhdl_source/slot_server_v4.vhd`, `g_direct_dma` selects the generate branch `r_master` or `r_no_master`, with different stop logic. A trace through the wrong branch proves nothing.
2. **Separate three ways a synchronous element stands still:** the clock stops, a clock enable is off, or a qualifier inside the clocked process holds the state. Find what drives the enable and whether that driver has an enable too. In `fpga/6502/vhdl_source/proc_interrupt.vhd`, the edge detector and state machine sit under `clock_en`.
3. **Audit which ports an architecture uses.** A declared port proves nothing until the architecture reads it.
4. **For an edge-triggered interrupt, find the latch and every writer of it.** Follow pad, synchroniser registers (note each enable), edge comparison, latch. In `proc_interrupt`, `nmi_act` is set on a rising edge of the registered NMI input and cleared in the `do_nmi` state, or by `reset`. Check which wins when a set and a clear coincide.
5. **Treat a shared open-drain line as wired-OR, and list every driver.** In `slot_server_v4`, `nmin_o` is low when `control.c64_nmi`, `nmi_n` or `slot_resp.nmi` is asserted. A driver holding the line low hides another driver's edge.
6. **Check release paths as carefully as assert paths.** A stop mechanism may need the same condition to release as to stop. In `fpga/cart_slot/vhdl_source/slot_master_v4.vhd`, the `stopped` state is left only when `c64_stop` is low and the `stop` condition derived from `stop_cond` is true again. Writing the release register does not move the machine at once.
7. **Take register meaning from the decode, not the C header.** Read the cases in `fpga/cart_slot/vhdl_source/cart_slot_registers.vhd` for bits, side effects and reset values, and look for status bits the firmware never reads. The read of `c_cart_c64_clock_detect` returns `status.nmi` in bit 5.
8. **Name clock domains at every crossing:** sending domain, receiving domain and synchroniser. `fpga/ip/synchroniser/vhdl_source/level_synchronizer.vhd` is a two-flip-flop level synchroniser. Bus program order holds within one domain only; work out each path's latency before claiming a race.
9. **Finish with a falsification test.** State the measurement that would show the analysis wrong, for example a failure rate that changes with a hold time the RTL says is irrelevant. Prefer one status read at the moment of failure to a rate over many runs.

## Cross-checks

- Check firmware comments about hardware against the RTL.
- Read the decoder before assuming an instruction does something: in `fpga/cpu_unit/rvlite/vhdl_source/decode_comb.vhd` the `FENCE` case is `null`.
- Two losses in a row that a fresh run clears point to persistent state.
