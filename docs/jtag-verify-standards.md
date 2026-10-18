# JTAG verifier: standards, sources of truth and coverage

This is the requirements and evidence map for `tooling/jtag_verify.py`.
[Usage and safety preconditions](jtag-verify.md) are maintained separately.
The methods below describe what is established, without duplicating transport
encodings or the pattern schedule in the implementation.

There is no single certificate covering USB-Blaster compatibility, FT232H command
processing, target JTAG implementation, board electrical quality and debugger
reliability. The verifier reports evidence for a limited, safe subset. It never
labels a cable fully standards compliant.

## Source hierarchy

The references were reviewed on 2026-10-18. Manufacturer documents take precedence
for their products. IEEE defines the TAP architecture; device manuals define the
instruction width, opcodes and side effects for a particular FPGA. Open-source
cable drivers are compatibility evidence, not normative specifications.

| Ref | Source of truth | Scope and limits |
| --- | --- | --- |
| IEEE | [IEEE 1149.1-2013, Test Access Port and Boundary-Scan Architecture](https://standards.ieee.org/ieee/1149.1/4484/) | Architectural source for TAP transitions, IR capture and BYPASS. The public catalogue is not the full standard; device manuals and existing independently modelled transports support the concrete checks here. No claim of a complete clause-by-clause audit. |
| UB | [Altera USB-Blaster Download Cable User Guide, 15.1](https://www.intel.com/content/dam/support/us/en/programmable/support-resources/bulk-container/pdfs/literature/ug/archives/ug-usb-blstr-15.1.pdf) | Original USB-Blaster pinout, supported modes, electrical and installation requirements. Not USB-Blaster II or III. The guide does not supply a public normative USB byte-stream specification. |
| OCD | [OpenOCD USB-Blaster driver](https://www.openocd.org/doc/doxygen/html/usb__blaster_8c_source.html) | De facto pin-level and byte-shift protocol; packet handling and readback conventions. Pinned source analysis is recorded in the investigation document. |
| OFL | [openFPGALoader USB-Blaster implementation](https://github.com/trabucayre/openFPGALoader/blob/bf2b52916e7b6216ba1cc0b30933827d7735b05d/src/usbBlaster.cpp) | Independent original-cable protocol implementation, including byte shifts. Its programming paths are reference material only and are not called. |
| FTDS | [FT232H datasheet, FT_000288, MPSSE interface section](https://ftdichip.com/wp-content/uploads/2020/07/DS_FT232H.pdf) | Manufacturer capabilities, pin functions, clock and I/O timing. Advertised speed does not prove an assembled breakout meets those timings. |
| FT108 | [FTDI AN_108, FT_000109, version 1.5](https://ftdichip.com/Support/Documents/AppNotes/AN_108_Command_Processor_for_MPSSE_and_MCU_Host_Bus_Emulation_Modes.pdf) | MPSSE bit/byte shifts, TMS commands, edge selection, readback, immediate delivery, clock divisors. This is the command processor source of truth. |
| FT129 | [FTDI AN_129, Interfacing FTDI USB Hi-Speed Devices to a JTAG TAP](https://www.ftdichip.cn/Support/Documents/AppNotes/AN_129_FTDI_Hi_Speed_USB_To_JTAG_Example.pdf) | Manufacturer JTAG usage and signal sequencing; not a specification for every breakout's wiring. |
| PY | [PyFtdi low-level API](https://eblot.github.io/pyftdi/api/ftdi.html), [pinned 0.57.2 implementation](https://github.com/eblot/pyftdi/blob/v0.57.2/pyftdi/ftdi.py) | Host backend semantics: location URLs, MPSSE initialization, clock selection, transfer handling and close/freeze behavior. Software-calculated clock readback is not a frequency measurement. |
| CV | [Cyclone V Handbook, JTAG instruction table](https://docs.altera.com/r/docs/683375/current/cyclone-v-device-handbook-volume-1-device-interfaces-and-integration/supported-jtag-instruction) | Ten-bit IR, IDCODE `0000000110`, BYPASS `1111111111`; IDCODE selected in TAP reset. Normal-operation BYPASS does not isolate FPGA I/O: that is the distinct, prohibited HIGHZ instruction. IDCODE requires nCONFIG/nSTATUS high. |
| A7 | [AMD/Xilinx UG470, 7 Series FPGAs Configuration User Guide](https://docs.amd.com/v/u/en-US/ug470_7Series_Config) | Six-bit IR, IDCODE and BYPASS; configuration instructions have very different effects and are forbidden. Applies to the reviewed Artix-7 50T/100T profiles. |
| NIOS | [Intel Nios II Software Developer Handbook](https://www.intel.com/programmable/technical-pdfs/683525.pdf), sections on nios2-gdb-server and debugging ISRs; [Nios II instruction reference](https://cdrdv2-public.intel.com/666887/n2cpu-nii5v1gen2-683836-666887.pdf) | Native debug transport and R1 instruction semantics. Intel warns that stepping ISRs can invalidate drivers and require reset. Live stepping is disabled because the reviewed backend cannot verify the ISR-exclusion precondition; the small instruction oracle is host-tested only. |
| RSP | [GNU GDB remote packets](https://www.sourceware.org/gdb/current/onlinedocs/gdb.html/Packets.html), [detach behavior](https://www.sourceware.org/gdb/current/onlinedocs/gdb.html/Connecting.html) | Stop status, general registers, memory read, hardware-step request, continue and interrupt. Detach normally resumes but is stub-dependent; the suite also verifies firmware health after cleanup. No write, breakpoint, monitor or image-loading packet is exposed. |
| SLD | [Intel Virtual JTAG IP guide](https://www.intel.com/programmable/technical-pdfs/683705.pdf) | Native Nios debug uses the vendor's SLD/OCI infrastructure through USER0/USER1. Only the separate opted-in native backend may do this. These are not permitted by the default raw-scan policy or on C64U. |
| LOCAL | `tooling/u64ii_jtag.py`, `tooling/test_u64ii_jtag.py`, `tooling/u64ii_gdbstub.py`, `tooling/read_u64_jtag_terminal.sh`, [C64U guide](c64u-jtag.md), [U64 guide](u64-jtag-deploy.md) | Reusable transports, scan sequencing, physical wiring and operational context. The USER-chain implementation shows memory commands, output controls and FIFO consumption. A terminal reader consumes data; a debugger can stop a CPU. Neither is a safe verifier primitive. |

The local Intel Quartus 18.1 `quartus/linux64/pgm_parts.txt` was also checked for
part identity. It lists `02B050DD` as Cyclone V `5CEBA4`/`5CEFA4`, including
`5CEBA4F23`; this is the ID actually observed on the U64 Elite I. An older
repository test fixture named `02D020DD` as A4, but the vendor table identifies
that as `5CSEBA6`; that fixture was not used as authority for this profile.
This vendor database establishes a part mapping,
not adapter authenticity or the identity of the running FPGA design. C64U's
50T/100T IDs reuse the existing repository mapping. Firmware and board identity
must be confirmed independently before selecting a profile.

Official PDF download servers can deny automated clients; links are supplied for
manual review. No unpublished transport detail is promoted to an official
USB-Blaster requirement. USB-Blaster II variable-clock/firmware-loading behavior
must not be used to fail an original USB-Blaster I clone.

## Requirement classes

- **N**: published JTAG architectural requirements, exercised through documented
  device implementations; only a subset is checked.
- **M**: documented manufacturer command behavior or advertised capability.
- **C**: de facto compatibility established by maintained implementations.
- **T**: target-specific functionality and safe-access preconditions.
- **E**: engineering reliability or quality assessment, without a standards threshold.

Safety is **SAFE**, **DISRUPTIVE** or **PROHIBITED**. SAFE in this matrix is
conditional on the reviewed, powered, configured single-FPGA wiring profile.
Unmapped devices receive enumeration only. Automated verification cannot prove
physical preconditions such as I/O voltage or absence of extra reset wiring.

## Requirements matrix

IDs in `JTAG-SCAN.covers` identify the grouped scan checks. Others are top-level
report requirements or explicitly excluded requirements below.

| Requirement ID / class | Adapter | Source | Expected behavior | High-level verification method | Required hardware | Safety | Automated coverage |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USB-ID / E | Both | PY, USB descriptors | Enumerate every matching interface; distinguish physical attachments without trusting serial uniqueness | Read USB descriptors and topology, sort deterministically, preserve descriptor errors | Adapter attached to host | SAFE | Yes; no interface claim or target clocks. Identity strings are untrusted metadata. |
| ADAPTER-OPEN / C,M | Both | PY, OCD, FT108 | Compatible host transport can be initialized | Claim exact bus/address interface under target lock; operational failure stops scans | Adapter and confirmed powered target | SAFE | Yes when mapped; error is not automatically an adapter conformance defect. |
| TAP-ID / N,T | Both | IEEE, CV, A7 | Documented IDCODE, stable part identity, single-device serial pipeline | TAP reset selects IDCODE; read identity and shifted-through sentinel, then compare IDCODE between pattern groups | Reviewed configured FPGA | SAFE | Yes; wrong part/chain blocks explicit IR scans. Does not discover arbitrary chains. |
| TAP-IR / N,T | Both | IEEE, CV, A7 | IR capture ends in `01`; documented IR width | Capture IR while selecting only IDCODE or all-ones BYPASS | Reviewed configured FPGA | SAFE | Yes; unused implementation-defined capture bits are not scored. |
| TAP-BYPASS / N,T | Both | IEEE, CV, A7 | Capture zero and exact one-bit serial delay | Compare all returned bits with deterministic zero/one/random patterns | Reviewed configured FPGA | SAFE | Yes; diagnoses wrong delay, stuck or corrupted samples without boundary-scan I/O control. |
| SHIFT-BITS / C,M | Blaster / FT232H | OCD, OFL / FT108, FT129 | Correct partial-byte LSB-first scan and readback | Vary scan length around byte boundaries; compare BYPASS oracle | Reviewed configured FPGA | SAFE | Yes, pin-level Blaster or MPSSE bit commands. |
| SHIFT-BYTES / C,M | Blaster / FT232H | OCD, OFL / FT108 | Correct bulk LSB-first shifts and packet framing | Mix bulk readback with small scans, including lengths crossing the Blaster 63-byte payload and USB packet boundaries | Reviewed configured FPGA | SAFE | Yes, supported JTAG subset; not every possible MPSSE command or maximum-length encoding. |
| SHIFT-TMS / C,M,N | Both | IEEE, OCD, FT108, FT129 | Correct TAP transitions and last-bit readback | Repeated IR/DR selection, scan exit, update and idle; final data bit participates in oracle | Reviewed configured FPGA | SAFE | Yes; no Pause-state exhaustive TAP traversal, TRST or system reset. |
| FT-SYNC / M | FT232H | FT108, PY | MPSSE parser returns the documented bad-command echo | Send the documented synchronization probe and compare its response before any target clocks | Powered adapter and reviewed wiring | SAFE | Yes; command mismatch blocks all target scans. This is a documented adapter command response, not an unknown JTAG instruction. |
| FT-CLOCK / M | FT232H | FTDS, FT108, PY | Accept selected clock divisors and preserve scan correctness | Repeat deterministic scans at conservative configured frequencies | Reviewed configured FPGA | SAFE | Partial: host configuration and functional operation; physical frequency/duty cycle unverified. |
| TRANSPORT-SOAK / E | Both | Engineering workload, not a normative limit | Complete all scheduled mixed short/bulk scans without mismatches or retries | Repeat fixed-seed cycles; report compared bits, digest, elapsed time, throughput and latency | Reviewed configured FPGA | SAFE | Yes, bounded sample only; no universal pass threshold for latency or infinite reliability claim. |
| DEBUG-SESSION / T,E | USB-Blaster on U64 | NIOS, RSP, SLD, LOCAL | Stable halted register reads, actual predicted instruction steps, resume and health preservation | Repeat native-server attach/read/guarded hardware-step/continue/interrupt/resume cycles; compare available general-register slots and next PC, repeat code-window reads and verify health after each resume | Confirmed idle U64, Intel tools and matching read-only health endpoint | DISRUPTIVE | Automated behind opt-in and preconditions. Live stepping is disabled: Intel 18.1's unavailable status slot cannot prove the ISR-exclusion precondition. The five-opcode oracle is host-tested only. Minimum actual steps remain unmet/UNVERIFIED; opt-in cannot override qualification. |
| DEBUG-SESSION / T | FT232H on C64U | LOCAL | Distinguish unavailable processor control from a cable defect | Inspect the existing GDB stub and core interface; enforce target exclusion before backend invocation | C64U | DISRUPTIVE, forbidden on this target | Reported UNSUPPORTED: no CPU halt/step support in the reviewed tooling. No live halt/step attempt. |
| ELECTRICAL / M,E | Both | UB, FTDS, FPGA electrical manuals | Appropriate levels, edge timing, margins and signal integrity | External voltage/timing measurements | Oscilloscope/logic analyzer and suitable fixture | SAFE with approved fixture | No: reported UNTESTABLE, no electrical fault injection or arbitrary GPIO drive. |
| USER-READ / T | C64U USER chain | LOCAL | A putative read must not consume or change state | Source review finds output registers, memory commands and FIFO pops behind apparently readable interfaces | Running FPGA design | PROHIBITED in verifier | Host safety-policy tests verify USER instructions are rejected; no live C64U USER access. Native U64 OCI is separately gated. |
| PROGRAM / T | Both | CV, A7, LOCAL | Never configure, erase, reset configuration, write firmware, RAM, peripherals or adapter EEPROM | Default-deny instruction boundary and fixed scan/RSP policies without programming APIs | No live hardware needed for enforcement tests | PROHIBITED | Host tests exhaust prohibited instructions across each profile's IR space and reject bitstreams; opt-in does not widen policy. |
| FAILURE-CLEANUP / E | Both | PY, LOCAL | Do not finish unknown commands or emit recovery clocks after partial transfers | Fault injection verifies further scan calls are rejected and transport closes without additional command writes | Host transport simulator; live fault injection excluded | SAFE | Yes in host tests; healthy completion selects IDCODE and disables outputs. |

## Evidence boundaries

`test_jtag_verify.py` drives the actual existing transport encoders against an
edge-level TAP oracle for both IR widths. It also checks deterministic patterns,
wrong identities, chain tails, corrupted readback, deadlines, discovery,
duplicate serials, locks, CLI/report behavior and fault cleanup. Existing transport
tests provide independent opcode and framing checks. These host models establish
software behavior; hardware scans provide separate empirical evidence.

Neither electrical requirements nor a processor debugging session follow
from an IDCODE or BYPASS pass. The opted-in U64 debug suite provides additional narrowly scoped evidence;
source-level stepping, breakpoint behavior and other processor designs remain
unverified. A programming success or FIFO read cannot substitute for those tests. Extending coverage requires new reviewed semantics
and restoration evidence below the test cases, not a new unrestricted CLI option.

The [code-driven investigation](jtag-verify-investigation.md) pins public driver
and firmware sources, separates possible mechanisms from observed failures, and
records why configuration-loss claims require additional evidence. The
[hardware-run account](jtag-verify.md#native-debug-qualification-and-failed-soak)
includes failed measurements; the native U64 soak did not qualify this setup.
