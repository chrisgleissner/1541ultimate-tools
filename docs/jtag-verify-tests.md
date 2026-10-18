# JTAG verifier: tests and recorded hardware results

This guide explains what the verifier checks, why each check matters, and what
happened when it was run against our USB-Blaster clone and Adafruit FT232H.
It covers the measurements recorded on **2026-10-18**. Device health statements
below describe those recorded runs, rather than a continuously monitored status.

**The USB-Blaster passed the raw JTAG transport soak. Neither adapter has been
qualified for reliable processor debugging by this work.** The FT232H completed
some adapter checks, but did not complete a successful target-scan suite.

| Connected setup | What passed | What remains unresolved |
| --- | --- | --- |
| No-name USB-Blaster I compatible cable → U64 Elite I | USB enumeration; FPGA identity; instruction capture; deterministic BYPASS scans; sustained and short-transfer readback | The native CPU debug soak failed. A verifier defect invalidated its interrupt-context guard. Live stepping is now disabled. |
| Adafruit FT232H breakout → C64 Ultimate | USB enumeration; documented MPSSE parser response on some attempts | Other attempts returned a wrong parser response or timed out. FPGA scans, clock sweeps and sustained target readback were not established. CPU debug is excluded. |

## Where to start

- [Running the tool and checking its preconditions](jtag-verify.md)
- [Transport tests](#transport-tests-what-is-checked-and-why)
- [Optional U64 processor tests](#optional-u64-processor-tests)
- [USB-Blaster results](#usb-blaster-results-on-the-u64)
- [FT232H results](#ft232h-results-on-the-c64-ultimate)
- [Host tests and CI](#host-tests-and-ci)
- [Reading the reports](#reading-the-reports)
- [Standards and requirement IDs](jtag-verify-standards.md)
- [Code-driven investigation and limits on attribution](jtag-verify-investigation.md)

## What a successful test establishes

There are three different things to verify:

1. **Adapter communication:** can the host open the correct USB device and exchange
   the expected adapter commands and replies?
2. **JTAG transport:** can the adapter reliably move known bits through the FPGA's
   JTAG interface, including short transactions and large transfers?
3. **Processor debugging:** can a debugger safely halt, inspect, step, continue and
   interrupt the CPU, then leave the firmware working normally?

Success at one level does not establish the next. Reading an FPGA's ID is a
small transport test. Passing many BYPASS scans provides stronger transport
evidence, but does not exercise a complete processor debug session. A software
simulator passing a test does not establish that physical hardware passed it.

The tool reports individual results rather than issuing a full standards or
debugger-compliance certificate. It does not treat an incomplete test as a pass.

## Preconditions and safety

Without a target mapping, the verifier enumerates adapters without opening their
USB interfaces or clocking the target. Mapping an adapter to `u64` or `c64u`
asserts the powered board, correct wiring, a configured single-device FPGA chain,
an idle adapter at a complete USB command boundary, and a JTAG TAP known to be in
Idle or Test-Logic-Reset. The [usage guide](jtag-verify.md#running) explains these
physical and operational preconditions.

A complete USB command and a known TAP state are separate requirements. Clocking
a reset sequence from an unknown partial scan could commit a previous client's
instruction or data. The verifier does not attempt automatic recovery from that
situation.

The default target-facing suite permits only the reviewed **IDCODE** and
**BYPASS** instructions. IDCODE reads the FPGA identity. BYPASS provides a tiny
serial register through which known test bits can be shifted without controlling
the board's normal I/O. Neither operation is an FPGA programming operation.

Unknown instructions, configuration/programming operations, arbitrary writes,
breakpoints and C64U USER-chain accesses are rejected. USER-chain reads are
excluded because apparently readable interfaces can issue memory commands or
consume FIFO data. The disruptive flag does not relax these restrictions.

## Transport tests: what is checked and why

These checks are implemented using the repository's existing `Blaster`, `Mpsse`
and `Tap` transports. The requirement IDs link the results to the
[standards matrix](jtag-verify-standards.md#requirements-matrix).

| Check | What the verifier does | What a successful result means |
| --- | --- | --- |
| USB discovery and identity (`USB-ID`) | Enumerates supported devices and records VID/PID, descriptors, serial, USB topology and speed. Uses physical location to distinguish devices with duplicated serial numbers. | The adapter was found and its metadata could be read. Descriptors do not prove authenticity or electrical quality. |
| Open the selected adapter (`ADAPTER-OPEN`) | Opens the exact bus/address under the appropriate device lock and initializes the existing transport. | The host/backend could initialize that interface. A timeout here is an operational error, not a diagnosed hardware defect. |
| FT232H command synchronization (`FT-SYNC`) | Requests FTDI's documented invalid-command echo before starting target scans. | The MPSSE command parser returned the expected response on that attempt. The command itself does not clock JTAG. |
| FPGA identity and chain (`TAP-ID`) | Reads the reset-selected IDCODE and checks the expected identity plus the shifted-through tail of a single-device chain. | The observed chain matches the reviewed profile. A mismatch blocks explicit instruction scans. This is not arbitrary chain discovery. |
| Instruction capture (`TAP-IR`) | Checks the defined low capture bits while selecting an allowed instruction. | Instruction scanning and capture agree with the expected TAP behavior. Other implementation-defined capture bits are not scored. |
| BYPASS readback (`TAP-BYPASS`) | Shifts known patterns through the one-bit BYPASS register and compares every returned bit. | Capture starts with zero and the returned pattern has exactly the expected one-bit delay. Stuck bits, lost bits and wrong alignment can be detected. |
| Bit, byte and state transitions (`SHIFT-BITS`, `SHIFT-BYTES`, `SHIFT-TMS`) | Mixes small and large scans, partial bytes, adapter packet boundaries, instruction/data selection and scan exits. | The exercised transport paths preserve the expected bit sequence and TAP transitions. This is not an exhaustive traversal of all TAP states. |
| FT232H clock selection (`FT-CLOCK`) | Repeats the functional scan workload at conservative requested clock settings: 100 kHz, 1 MHz and the selected maximum, omitting settings above that maximum. | The driver accepted those settings and scans worked at them. Physical clock frequency, duty cycle and voltage margins remain unmeasured. |
| Repeated workload (`TRANSPORT-SOAK`) | Repeats the mixed scan schedule with a fixed seed and no automatic retries. | Every scheduled scan completed correctly for that particular duration, wiring, host and target. It does not prove indefinite reliability. |

### How known data makes readback testable

BYPASS is useful because its expected output does not depend on firmware data.
After the initial captured zero, each returned bit should be the preceding input
bit. The verifier can therefore calculate the complete expected reply and compare
it with the adapter's actual reply.

Each cycle uses all-zero, all-one and seeded random patterns. Scan lengths cross
important boundaries, such as 7/8/9 bits, the USB-Blaster's 63-byte shift payload,
and larger transfers around 65,536 bits. The schedule alternates short and bulk
operations rather than testing only a continuous large transfer. The complete
schedule is maintained in [the implementation](../tooling/jtag_verify.py), so this
guide does not duplicate every length or transport opcode.

The seed and cycle count reproduce the pattern sequence. Timings can vary with
USB traffic and host scheduling. Reported bit counts count compared BYPASS bits;
they exclude instruction scans, IDCODE scans and state-transition clocks.

### What happens when a check fails

An incorrect instruction capture, unknown identity, transfer failure or data
mismatch stops the affected adapter's remaining raw-scan tests. The verifier
does not retry a failing scan to obtain a pass. It also blocks cleanup clocks
through an uncertain transport and closes with pin levels retained.

Other selected adapters are still processed. On a healthy completion, the TAP is
returned to IDCODE and adapter outputs are disabled. Failure to disable outputs
is reported rather than silently counted as a successful run.

## Optional U64 processor tests

These are **disruptive**: attaching the Intel Nios II server halts the CPU.
They require `--allow-disruptive`, an explicit idle assertion, a passed transport
suite, the reviewed Intel 18.1 installation, the matching cable/FPGA identity and
a healthy read-only firmware endpoint. An already paused CPU fails the
precondition and is preserved.

The C64U never enters this backend, including when the disruptive flag is set.

| Debug feature | What is exercised or checked | Current coverage |
| --- | --- | --- |
| Attach and halt | Confirms the vendor server observed a running-to-paused transition. | Exercised on the U64; individual successful cycles do not qualify the full debug workflow. |
| Register reads | Repeatedly compares the halted general-register reply. | API/snapshot consistency was exercised. Intel serves saved register context, so these are not independent hardware reads on every request. Unavailable register slots are not proof of corresponding physical-register values. |
| Code-memory reads | Compares two 256-byte reads at the current PC inside the permitted DDR range; records a digest. | Exercised, including the timeout that stopped the long debug run. It checks consistency, not correctness against a matching firmware image. |
| Continue and remote interrupt | Continues at the existing PC, checks firmware health, interrupts the running session and checks register stability. | Exercised during completed cycles. The long run still failed and restoration of firmware health was not established. |
| Resume and cleanup | Explicitly continues execution, closes a one-shot server and checks firmware identity/health. | Successful smoke checks and later failed cleanup are both retained. An acknowledged continue is not sufficient evidence of healthy firmware. |
| Hardware instruction stepping | A small predictor can check selected ALU/branch instructions against the resulting PC/register slots. | **Disabled in the shipped live backend.** Earlier hardware attempts used an invalid interrupt-context guard and do not qualify safe stepping. The predictor and gate are host-tested. |
| Hardware breakpoints/watchpoints | Would require confirmed resources in the running bitstream and a safe installation/restoration procedure. | **UNVERIFIED.** None were installed. Public reference-design parameters do not establish the installed bitstream's resources. |
| Source-level step/next/finish | Requires appropriate symbols and additional debugger behavior. | **UNVERIFIED.** No matching ELF was available; no software-breakpoint fallback was used. |
| Software breakpoints, memory/register writes, resets and image loading | Outside the permitted verifier operations. | **Excluded.** The disruptive flag does not enable them. |

Because live step coverage is blocked, the current CLI cannot award a fully
qualified debug-session pass. Requested but unmet step coverage remains
`UNVERIFIED`, even if halt/read/continue checks complete.

### Why stepping is disabled

The earlier verifier interpreted GDB register slot 35 as valid pre-debug interrupt
status. Static inspection of the installed Intel library showed that this slot
is unavailable, and the server returns an all-ones placeholder. Its low bit was
incorrectly accepted as an enabled-interrupt flag.

That defect meant the verifier could not prove it was avoiding interrupt handlers.
Intel documents hazards when stepping those handlers. The current qualification
gate blocks live stepping before sending a step request, including when a reply
appears to contain a valid status bit. No CLI flag overrides the gate.

The [investigation](jtag-verify-investigation.md) records the artifact identities,
code observations and their limits. The defect is established; whether it caused
the observed firmware health failure remains unresolved.

## USB-Blaster results on the U64

The tested cable was the no-name USB-Blaster I compatible clone at physical
identifier `09fb:6001@1-6`. The mapped target was the Ultimate 64 Elite I with
Cyclone V IDCODE `02B050DD`. The USB identity does not identify the clone firmware
or establish that the adapter is an authentic Altera cable.

### Raw transport: passed the recorded workload

The [100-cycle report](evidence/jtag-verify-2026-10-18.json) records:

| Measurement | Recorded result |
| --- | --- |
| Pattern seed | 1541 |
| Completed BYPASS scans | 7,200 |
| Compared BYPASS bits | 63,677,100 |
| Observed mismatches/retries | Zero / zero |
| Scan-suite elapsed time | 154.53 seconds |
| Short-scan median / maximum latency | 0.420 / 3.667 milliseconds |
| Identity, instruction capture and one-bit BYPASS behavior | Passed |

This is a transport pass for the measured workload. It is not an electrical
certification or a processor-debugging pass. USB-Blaster I has no equivalent
FT232H programmable-clock requirement; it was not judged against one.

The [final-source SAFE check](evidence/jtag-verify-final-source-2026-10-18.json)
also passed: 72 scans and 636,771 compared bits in approximately 1.54 seconds.
It selected only the clone after a preceding successful verifier run had left
the TAP in known Idle. Its source hashes identify the final tested implementation.

### CPU debugging: the requested soak did not complete

The [debug soak report](evidence/jtag-verify-u64-debug-soak-2026-10-18.json)
requested 1,000 debug cycles and at least 100 actual checked steps. It stopped in
the 117th attempted cycle, reported as zero-based `failed_cycle: 116`.

| Observation before the run stopped | Recorded count |
| --- | --- |
| Completed cycles | 116 |
| Stable register-snapshot comparisons | 1,402 |
| Instruction prediction/readback matches | 91 |
| Completed 256-byte code-window comparisons | 116 |
| Remote interrupts | 116 |
| Debug-phase elapsed time | 157.17 seconds |

The immediate failure was a code-memory read timeout. The cleanup continue then
timed out, followed by the firmware health check. The report preserves the full
exception chain and completed measurements; the failed cycle was not retried.

These counts include observations from the failing cycle where applicable. They
are not 1,000 successful cycles, 1,402 independent hardware-register reads, or
proof that all debug features worked. The invalid interrupt-context guard also
prevents treating the run as a clean adapter-reliability experiment.

A separate one-shot Intel server subsequently acknowledged an explicit continue
and reported the CPU running. A later register-only attach/resume confirmed it
could run again, but the U64 network service remained unavailable during the
recorded follow-up checks. No reset, firmware reload or FPGA reconfiguration was
used for recovery. See the [post-soak health record](evidence/jtag-verify-post-soak-health-2026-10-18.json).

### Other retained observations

- The [initial debug attempt](evidence/jtag-verify-u64-debug-initial-2026-10-18.json)
  exposed another verifier assumption: the server acknowledged detach without
  resuming the CPU. The backend now uses explicit continue and a one-shot server.
- The [explicit-continue check](evidence/jtag-verify-u64-debug-restoration-2026-10-18.json)
  and [full smoke check](evidence/jtag-verify-u64-full-debug-smoke-2026-10-18.json)
  recorded successful individual sessions before the interrupt-status defect was
  discovered. Their historical `PASS` labels do not establish safe stepping.
- A [client-handoff attempt](evidence/jtag-verify-u64-handoff-rejected-2026-10-18.json)
  returned an all-ones identity and was rejected before explicit IR/debug scans.
  An independent Intel identity query later succeeded. The cause is unresolved.

The defensible conclusion is that raw transport worked for the recorded sample,
while reliable debugging has **not** been established. Neither the cable alone nor
the verifier defect has been conclusively identified as the cause of the health
failure. Loss of network service does not prove FPGA configuration loss.

## FT232H results on the C64 Ultimate

The tested board was the user-identified Adafruit FT232H breakout at
`0403:6014@5-1.4`, mapped to the C64 Ultimate. It uses MPSSE rather than the
USB-Blaster protocol, so it is an additional compatibility test, not a
protocol-equivalent reference for the clone.

| Check | What actually happened | Result to report |
| --- | --- | --- |
| USB discovery | The device, location and USB metadata were recorded. Some descriptor strings contained U+FFFF characters and were preserved without repair. | Enumeration completed; authenticity or an EEPROM defect was not inferred from strings. |
| Parser synchronization | Some attempts returned the documented response and passed `FT-SYNC`. Another returned `fa00` where `faaa` was expected. | Mixed outcomes; not a reliable parser pass across attempts. |
| Adapter initialization | An attempt timed out while opening the interface. | `ERROR`; does not by itself diagnose defective hardware. |
| FPGA identity and scans | Attempts that got beyond synchronization encountered scan/transport timeouts. | No successful target-scan suite was established. |
| Clock sweep and sustained readback | The functional scan workload did not complete. | Not verified; no successful soak or physical clock measurement is claimed. |
| CPU halt/step | The target exclusion was checked with the disruptive flag supplied. | `UNSUPPORTED` by the reviewed tooling/policy; no C64U CPU-debug attempt was made. |
| Board health | Read-only follow-up checks returned the same recorded version fields and no firmware-reported errors. | C64U was reachable after testing; this does not prove every transient state was unchanged. |

The [long transport report](evidence/jtag-verify-2026-10-18.json) and
[later scan attempt](evidence/jtag-verify-before-cleanup-guard-2026-10-18.json)
contain parser passes followed by operational errors. The
[C64U exclusion check](evidence/jtag-verify-c64u-debug-capability-2026-10-18.json)
contains the `fa00` response and confirms CPU debug remained excluded.
The [open-failure report](evidence/jtag-verify-final-check-2026-10-18.json)
records the separate initialization timeout.

No target clocks were emitted after the failed parser check. No USER-chain,
processor-control, firmware, configuration or flash operation was performed.
No further FT232H target scan was attempted under the final starting-state policy
after a timeout left TAP state uncertain.

It would be incorrect to report that the FT232H passed the full verifier, or that
these results prove an Adafruit hardware defect. The host backend, USB path,
wiring, target and their interaction have not been separated as possible causes.

## Host tests and CI

The physical results above are separate from the automated software tests.
Host tests simulate adapters, target TAPs and debug servers; they do not operate
the connected boards.

| Test group | What it establishes about the verifier software |
| --- | --- |
| Actual transport encoders against an edge-level TAP model | Both adapter encodings work with the reviewed six-bit and ten-bit profiles; deterministic patterns detect wrong delay, instruction capture and corrupted data. |
| Policy rejection | Prohibited instructions, unconfirmed identities, invalid lengths, write-only scans and bitstreams are rejected before transmission. |
| Failure handling | Partial transfers, semantic mismatches, clock failures and failed output-disable operations are reported; uncertain state blocks further cleanup clocks. |
| Discovery and selection | Physical location distinguishes duplicate serials; descriptor errors survive reporting; selection and custom compatible USB IDs are validated. |
| CLI and reporting | Inventory does not open an adapter; exit codes distinguish failures and incomplete coverage; metrics and historical failure details survive reporting. |
| Debug protocol | Reply checksums, framing, escaping, register/code reply validation, deadlines and stop responses are checked using simulated replies. |
| Debug safety and restoration | Missing prerequisites prevent attach; an initially paused CPU is preserved; resume is attempted after errors; server shutdown is bounded; C64U is excluded; flags/status replies cannot enable unqualified live stepping. |
| Instruction predictor | Synthetic qualified models exercise the limited instruction oracle and step-count bounds. This does not qualify the Intel live backend or prove real CPU stepping. |

The two verifier test files contain **42 tests**, included in the **575-test
tooling suite**. Four existing expected failures remain in that wider suite.
The PR also passed 14 Vivado host tests, 17 installer tests, six release tests,
shell syntax checks and the existing shell helper tests. Both new verifier
modules reached 100% line/branch coverage; coverage is software execution
coverage, not a percentage of hardware features verified.

The [PR checks](https://github.com/chrisgleissner/1541ultimate-tools/pull/15/checks)
also include installation and build/integration against a fresh public firmware
checkout. They do not repeat the physical adapter experiments.

## Reading the reports

| Status | How to interpret it |
| --- | --- |
| `PASS` | That particular check met its expected result. Other capabilities can still be unverified or fail. |
| `FAIL` | A measured scan mismatch or incomplete scan suite was observed. It is not automatically a localized cable diagnosis. |
| `ERROR` | An operational failure occurred, such as a USB timeout or failed debug session. |
| `UNTESTABLE` | A required precondition or measurement facility is missing. |
| `UNVERIFIED` | The stated capability has not been established. |
| `UNSUPPORTED` | The reviewed implementation/policy provides no permitted test for that capability. |

A successful transport-only exit does not certify debugging. When U64 debug
coverage is explicitly requested but remains incomplete, the CLI exits with an
incomplete-coverage result rather than converting it into a debug pass.

Reports include the adapter metadata, settings, source SHA-256 hashes, runtime
versions, per-check results, compared bit counts, readback digest and timings.
Use those fields to identify exactly which implementation and workload produced
a result. Historic reports retain their original labels and source hashes;
earlier debug `PASS` entries and older verdict wording must be read with the
qualification defects described above.

The earlier hardware reports also predate the final Idle/reset clarification and
semantic-failure cleanup guard. They are retained as observations, not presented
as proof that the final safety policy was already active.

## What would be needed for stronger conclusions

- A verified, safe pre-debug interrupt-context check and successful live
  qualification before enabling instruction stepping.
- Completed debug soaks with enough actual operations, reliable restoration and
  healthy firmware checks, plus explicit coverage of each additional feature.
- Resolution of the FT232H parser/timeout failures before claiming its FPGA
  scans, frequency sweep or sustained transport passed.
- External instruments for electrical timing, voltage and signal-integrity
  claims. USB readback alone cannot measure these.
- Independent configuration-state evidence to establish FPGA configuration loss,
  and identified clone firmware/failing transactions to attribute a cause to a
  particular implementation.

The [standards matrix](jtag-verify-standards.md) explains the requirements and
authoritative references. The [investigation](jtag-verify-investigation.md)
separates public code observations, measurements and unresolved hypotheses.
Private FPGA repositories are not sources for this public guide.
