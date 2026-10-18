# USB-Blaster debugging investigation

This account uses public source code and recorded measurements. It separates
known mechanisms from hypotheses about a particular cable. Neither USB identity
nor a successful FPGA configuration establishes debugging reliability. Conversely,
a debugger failure alone does not establish that an FPGA was cleared.

## What the public implementations establish

| Public source, pinned revision | Code observation | Consequence for verification |
| --- | --- | --- |
| [OpenOCD USB-Blaster driver, bb5a52d](https://github.com/openocd-org/openocd/blob/bb5a52da599c3dac61ddc51d2fa8c1166ee795d0/src/jtag/drivers/usb_blaster/usb_blaster.c#L201) | Pin commands and byte-shift commands implement generic JTAG operations. Mixed scans use bulk shifting followed by bit commands for the final TMS transition, with explicit attention to TCK level. | There is no separate cable debugging capability bit here. Workload shape, edge alignment and short/bulk boundaries matter, so the verifier mixes these paths. This does not describe proprietary Quartus internals. |
| [Same driver, readback paths](https://github.com/openocd-org/openocd/blob/bb5a52da599c3dac61ddc51d2fa8c1166ee795d0/src/jtag/drivers/usb_blaster/usb_blaster.c#L527) | The byte-read loop reduces the remaining count without advancing the destination pointer. The bit-read path uses its requested count without validating the returned count. | Host read handling can affect results; a failure must not automatically be attributed to cable firmware. These observations concern this pinned implementation, not every driver or release. |
| [CH55x replacement firmware, ed30b6f](https://github.com/dougg3/CH55x-USB-Blaster/blob/ed30b6f0a7c7d3a86cda35384f036687970abcd2/main.c#L785) | Shift payload state persists across received packets. Reply producers wrap a 128-byte ring without a full-buffer admission check; draining is conditional on endpoint readiness. The FTDI reset request handler has no parser-reset action. | Sustained outstanding reads and interrupted commands warrant investigation. The source does **not** establish that the tested cable uses this controller or firmware. Buffer pressure and parser recovery are hypotheses for this unit, not diagnosed causes. |
| [openFPGALoader TAP reset, bf2b529](https://github.com/trabucayre/openFPGALoader/blob/bf2b52916e7b6216ba1cc0b30933827d7735b05d/src/jtag.cpp#L416) and [Altera reset](https://github.com/trabucayre/openFPGALoader/blob/bf2b52916e7b6216ba1cc0b30933827d7735b05d/src/altera.cpp#L89) | TAP reset emits TMS-high clocks. The Altera device reset path separately scans ten-bit instruction `0x001`, identified as PULSE_NCONFIG. | TAP reset and FPGA configuration reset are distinct operations. The verifier permits the former under reviewed preconditions and rejects the latter. |
| [openFPGALoader USB-Blaster passive-serial path](https://github.com/trabucayre/openFPGALoader/blob/bf2b52916e7b6216ba1cc0b30933827d7735b05d/src/usbBlaster.cpp#L382) | Passive-serial wiring repurposes a cable control output as nCONFIG and drives it low during its reset sequence. | Pin effects depend on wiring/mode. This is a possible configuration-reset mechanism in that mode, not evidence that ordinary JTAG TMS cleared this U64. The verifier has no passive-serial path. |
| [Public U64 reference design, 5d42e1d](https://github.com/GideonZ/1541ultimate/blob/5d42e1db8c8e608dcf267a2f329fba18bab84708/fpga/nios_c5/nios.qsys#L509) | Debugging is enabled; configured hardware breakpoint and data-trigger counts are zero. | These are reference-design parameters. The installed bitstream's resource counts remain unverified. Do not claim live hardware breakpoint/watchpoint coverage from this source alone. |
| [Repository GDB stub](../tooling/u64ii_gdbstub.py) and [USER-chain implementation](../tooling/u64ii_jtag.py) | The C64U stub cannot halt/step its CPU. USER accesses include memory commands, output controls and FIFO consumption. | Neither a nominal read nor a working console demonstrates safe CPU debug control. C64U CPU testing and USER scans remain excluded. |

The [standards matrix](jtag-verify-standards.md) records manufacturer references
and the high-level methods used to check the permitted subset. Open-source
behavior is compatibility evidence, not a published USB-Blaster byte-protocol
standard. USB-Blaster II/III requirements are not applied to the original cable.

## Native Intel backend: observed and static evidence

The tested installation is Quartus 18.1.0.625. Its executable and debug library
were inspected locally with `nm`, `readelf` and `objdump`. REA's native analysis
providers were unavailable; no decompiler-based completeness claim is made.
Artifact SHA-256 values are:

- `nios2-gdb-server`: `f50a8e9c857e89b95b56a5fced970581fdf870dca4b0d4049b52507db9d80054`
- `libpgm_gdb.so`: `1f8f9a45b21fceded8199b0a6f657f39978098e3bd05dfa9d61c12b0c413c585`

The library has distinct normal-entry and reset-entry methods. In the examined
instructions, `enter_debug_mode` calls `request_debug_mode(false)`, whereas
`reset_and_enter_debug_mode` passes true; the reset-processor call in the latter
request path is conditional on that argument. `single_step` calls the restart
method with its step argument set. These are bounded static observations, not
proof of every vendor-library path, electrical effect or failure outcome. Vendor
code is not reproduced here.

The server's memory-read dispatch invokes `NIOS2DEBUG::read_memory` through its
virtual table. A distinct cached-memory helper exists elsewhere, so its symbol
alone would not prove that ordinary RSP memory requests are cached. Conversely,
the examined `read_register` returns values from the server's saved register
context. Repeating `g` therefore tests API/snapshot consistency, **not independent
hardware-register acquisition on every request**. New attach, step and interrupt
stops provide separate context acquisitions; counts must not be reported as an
equivalent number of USB/JTAG register reads.

The verifier's own backend exposes only fixed read/step/continue/interrupt
operations, with no reset, breakpoint, write, image or monitor API. Live testing
found that acknowledged `D` did not resume this server's target. Explicit `c`
and a one-shot server did; persistent relistening could halt it again. The
implementation and regression tests reflect these observations. Server
acknowledgement alone still does not establish restored firmware health.

## What happened on the connected hardware

The [recorded-run account](jtag-verify.md#recorded-hardware-run-2026-10-18)
links both successes and failures. The clone passed 63,677,100 compared raw-scan
bits, but the later native debug soak failed after 116 completed cycles and 91
checked hardware steps. The memory-read timeout was followed by a failed cleanup
continue and failed network health. A new one-shot server accepted continue and
reported execution running; the network service did not recover during the
subsequent checks. No reset/reconfiguration/reload was attempted.

Subsequent static analysis found a verifier defect: the library's `s_regid`
table maps GDB slot 35 to `0xff` (unavailable), and the server emits an all-ones
sentinel for unavailable slots. The old guard mistook its low bit for a valid
pre-debug PIE flag. It therefore failed to prove ISR exclusion. Intel's documented
ISR-stepping hazard is a possible contributor to the observed health failure;
the exact cause is still unresolved. Current code has a separate qualification
gate disabling live stepping, including for apparently valid status replies.

The 91 instruction-oracle matches establish observed PC/register-slot results,
not safe stepping contexts or a valid adapter soak. Moreover, GDB slots 25 and 30
are unavailable in the examined mapping; comparing their sentinel values does not
verify the corresponding physical registers. The hardware evidence **does not
qualify this setup for dependable debugging**, and cannot fairly condemn the
adapter. The target, firmware, cable, host,
vendor backend and their interactions remain possible contributors. The separate
FT232H initialization/parser failures likewise do not establish that the Adafruit
hardware is defective, and an FT232H is not a protocol-equivalent Blaster control.

To establish a claim of FPGA configuration loss would require independent
configuration-state evidence, such as suitable nCONFIG/CONF_DONE measurements or
captured and decoded configuration-reset instruction traffic. Network loss,
IDCODE availability, a paused processor or unchanged version strings do not
provide that proof. Attribution to a particular clone firmware additionally
requires identifying that firmware and capturing the failing transactions;
VID/PID and marketing names are insufficient. Such measurements were not
available in this run. No blanket claim about all cheap clones follows.

Private FPGA repositories are not sources for this public account. No private
source, design detail or derived implementation description is included.
