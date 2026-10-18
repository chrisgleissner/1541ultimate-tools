# JTAG adapter verifier

`tooling/jtag_verify.py` evaluates the JTAG transport of USB-Blaster I compatible
cables and FT232H boards, using the existing `Blaster`, `Mpsse` and `Tap` code.
It does not certify an adapter or an entire debugger. Optional, constrained
U64 processor debug checks provide additional evidence beyond transport scans.
A cable that programs an FPGA successfully can still fail the mixed short and
bulk readback traffic exercised here.

## Running

Use the repository's shared PyFtdi environment:

```sh
# First-time dependency setup only; --help does not open an adapter.
tooling/u64ii_jtag.sh --help
# Inventory every supported adapter without opening its interface.
tooling/jtag_verify.sh --list --json inventory.json
```

The Python entry point also works directly with Python 3.10+ and PyFtdi 0.57.2
(which installs PyUSB); libusb and permission to claim the USB interfaces are
required. Linux is the tested host. `install.sh CHECKOUT` automatically includes
both verifier entry points through its existing tooling glob. Documentation stays
in this repository. CI discovers `tooling/test_jtag_verify.py` automatically.

The default enumerates **all** matching adapters, preserving USB descriptors,
physical locations and speed. It performs target tests only for explicitly mapped
adapters. A missing wiring profile is `UNTESTABLE`, not a successful scan. It cannot
safely discover the semantics of an arbitrary running target by issuing probes.

An identifier such as `09fb:6001@1-6` describes VID:PID and the physical USB port
path. It remains stable across USB address changes at that port; moving a cable or
changing host topology changes it. If the OS cannot supply a port path, the fallback
contains the current USB address and is stable only for that attachment. Serial
numbers are metadata, never selection keys. This handles missing and duplicated
clone serials. USB identity does not prove authenticity.

Before mapping a target, confirm all of these conditions:

- It is the intended board, powered and running its normal configured design.
- The adapter is wired directly to its single FPGA TAP, with correct ground and
  I/O voltage; FT232H D0/D1/D2/D3 are TCK/TDI/TDO/TMS, without inversion or extra
  board reset wiring. The Adafruit I2C switch must be off. No power pin is driven.
- No debugger, programming operation, logic analyzer or console reader is using
  JTAG. In particular, a USB-Blaster must be at a complete transport command
  boundary; do not use it after an interrupted or failed programming operation.
- The FPGA is fully configured (Cyclone V nCONFIG/nSTATUS high). These are physical
  preconditions, not facts the verifier can prove through USB enumeration.

Copy identifiers from the inventory, then map them explicitly:

```sh
tooling/jtag_verify.sh \
  --target '09fb:6001@1-6=u64' \
  --target '0403:6014@5-1.4=c64u' \
  --json results.json

# Primary cable only, longer deterministic soak (~minutes).
tooling/jtag_verify.sh --type blaster \
  --target '09fb:6001@1-6=u64' --cycles 100 --timeout 600 --json soak.json
```

`--adapter ID` is repeatable; `--type blaster|ft232h` filters adapters. Without
filters all discovered supported adapters are processed, including unmapped ones.
`--usb-id abcd:1234=blaster` or `=ft232h` declares a compatible third-party USB
identity; it does not relax the target policy or support arbitrary FTDI wiring.
Only original USB-Blaster I protocol and single-channel FT232H JTAG wiring are
implemented. USB-Blaster II/III and other MPSSE chips are not implicitly included.

`--seed` and `--cycles` determine the complete pattern sequence. There are no
random retries. The default ten cycles mix zero, one and seeded random data,
IR/DR transitions, IDCODE comparisons and BYPASS scans around bit, byte and USB
packet boundaries. All returned BYPASS bits are compared, including the initial
zero capture and exact one-bit delay. Before target clocks, FT232H command synchronization is checked using the
manufacturer-documented bad-command echo (which emits no JTAG clocks).
The FT232H repeats the suite at 100 kHz,
1 MHz and the selected maximum (default 3 MHz), omitting settings above that
maximum. USB-Blaster I has no equivalent programmable-clock requirement.

`--timeout` bounds the suite between scan transactions; USB transfers also have
finite timeouts. A scan already in progress can exceed the suite deadline by its
bounded transfer/read-attempt budget. The timeout is not a hard real-time kill.
Counts and deadline limit execution; output reports the actual elapsed time.

The JSON report includes settings, runtime versions, source SHA-256 hashes, clock settings, per-adapter
results, compared bit counts, readback digest, throughput and latency. Short-scan
latency is reported separately from bulk transfer latency. Clock frequency is
software configured, not instrument measured. Latency and throughput include USB,
host scheduling, transport framing and TAP overhead; they are not electrical TCK
measurements. Bit counts describe compared BYPASS bits, excluding IR/IDCODE and
state-transition clocks.

## Results and safety

`PASS` means a particular measured check passed; `FAIL` means an observed scan
mismatch or incomplete scan suite. `ERROR` denotes an operational failure such as
USB initialization or I/O errors; it does not diagnose a defective adapter.
`UNTESTABLE` means a prerequisite or measurement facility is missing.
`UNVERIFIED` means the stated capability has not been established. `UNSUPPORTED`
means the target has no permitted implementation of that capability. A wrong target
or chain is `UNTESTABLE` and is rejected before any explicit instruction scan.
After an error, remaining checks for that adapter stop; other adapters continue.

Exit status is 0 when every selected adapter completed its transport suite, 1 for
measured failures or operational errors, and 2 for incomplete coverage, inventory,
invalid arguments or no matching adapters. Exit 0 still leaves processor debugging
and electrical conformance unverified. Automation must examine the capability
results, not interpret exit 0 as debugger certification.

The scan boundary permits only IDCODE and BYPASS for the reviewed profiles:
Cyclone V A4 (`u64`, ten-bit IR, ID 02B050DD) and Artix-7 50T/100T
(`c64u`, six-bit IR). Revision bits are ignored for identity matching, but exact
IDCODE readback must remain stable during a suite. Initial TAP reset selects
IDCODE; a 96-bit read checks the expected part and the single-device serial
pipeline before enabling explicit IR scans. This is not general chain discovery.
TAP reset changes JTAG state; for these configured devices it does not reset the
processor or reload FPGA configuration. Cleanup returns a healthy TAP to IDCODE.

Tests receive a restricted scan interface with no public raw cable or programming
API. Unknown instructions, unconfirmed targets, invalid lengths, write-only DR
scans and bitstreams are rejected before transmission. Only bounded scans of the
inert registers are available. Neither `Board` nor `UserChain` is instantiated.
USER reads were excluded because some read paths issue memory commands or consume
FIFOs; resetting that interface or restoring a selector is not automatically safe.

The existing per-target locks are taken without waiting. USB interface claims
provide further exclusion from other processes, but third-party tools need not
honor repository locks. Do not use this concurrently with them. The verifier
refuses `U64II_JTAG_LOCK=off`. Unmapped adapters are never opened. The USB-Blaster
open path reuses `Blaster` but omits its normal recovery padding: resynchronizing
an unknown, partially consumed shift command on a running board is not proven safe.

After uncertain raw-scan transfers the verifier sends no recovery clocks or commands and
closes with pin levels retained. Automatic recovery could complete an unintended
instruction. Inspect the connection and quiesce its other users before further
access. Clean completion uses the existing output-disable release procedures.
The shared MPSSE open/release primitives now also close failed initialization or
output-disable attempts with pin levels retained. All callers need this safe
resource cleanup, so the fix belongs in those primitives.

`--allow-disruptive` enables a separate, constrained U64/Nios II debug backend
only when the transport suite passed, `--debug-idle` asserts there is no storage,
recording or transfer activity, and a matching health host and Intel installation
are provided. The reviewed C64U tooling provides no JTAG CPU halt/step support and is
reported `UNSUPPORTED`; it never enters this backend, regardless of flags.

```sh
tooling/jtag_verify.sh --type blaster --target '09fb:6001@1-6=u64' \
  --allow-disruptive --debug-idle \
  --intel-root /path/to/intelFPGA_lite/18.1 \
  --debug-health-host u64 --debug-cycles 30 --debug-steps 16 \
  --json debug-results.json
```

This attaches to the **existing running firmware** through Intel's Nios II
server. No ELF or image is loaded. The backend verifies the exact cable location
and Cyclone V ID through Intel's enumerator, checks the read-only health endpoint,
and confirms the server observed a running-to-paused transition. An already
paused CPU is preserved and fails the precondition. Each cycle repeatedly reads
the halted general register file and checks stability, then attempts up to eight
instruction steps within a half-second loop scheduling budget (the shipped
qualification gate skips these attempts). It compares two
256-byte reads of the current code window, explicitly continues execution, checks
health, interrupts the running CPU, checks register stability again and resumes.
The default is ten cycles and at least eight actual checked steps. Repeated
register requests check server-snapshot/API consistency; Intel's backend saves
register context, so they are not independent hardware reads on every request.
Each attach, step and remote interrupt establishes a new stop context.

A restricted RSP interface permits only stop-status and register reads, aligned
four-byte instruction and 256-byte code-window reads in application DDR,
explicit continue `c` without an address and remote interrupt. The retained
`s` path is blocked by the code-level qualification gate.
`D` is deliberately absent: the tested Intel server acknowledged detach while
leaving the CPU paused. A one-shot server with explicit continue was required;
`--tcppersist` is excluded because a subsequent listener can halt it again.
There is no arbitrary packet or command argument. The fixed native-server argv
contains no S-record, reset, cache initialization or entry-point option. Native
Intel debug operations use the documented Nios II OCI/SLD infrastructure; this is
an explicitly disruptive backend, separate from the IDCODE/BYPASS-only policy.
Intel Quartus 18.1.0.625 is the reviewed tool version; other version strings are
rejected before attach. GNU `stdbuf` ensures the server readiness/state banner
is available promptly. Other tool versions need their own qualification before
being added to the allowed versions.

**Live stepping is disabled in the shipped backend.** Binary analysis found
that Intel 18.1's register mapping marks GDB slot 35 (the expected bstatus slot)
unavailable. Its all-ones response cannot establish pre-debug interrupt state.
An earlier guard wrongly accepted it; this could permit stepping in an ISR,
which Intel warns can invalidate drivers and require reset. The failed live
soak below therefore cannot qualify or condemn the adapter.

The retained five-opcode R1 predictor and guarded-step path are exercised only
in host models with a synthetic qualified status source. The separate code-level
qualification gate is false, has no CLI override, and rejects stepping even if
an unavailable/corrupted status response happens to contain a set PIE bit.
Neither `--allow-disruptive` nor an idle assertion can enable it. No software or
hardware breakpoint is installed. New independent pre-debug context evidence and
live qualification are required before enabling this path. Requested actual
step coverage remains `UNVERIFIED`, and an incomplete U64 debug suite exits 2.

Resume is attempted even after a failed check, sockets and only this run's
server process are closed, and health is checked after cleanup. Startup, RSP
replies and server shutdown have bounded waits. The half-second budget schedules
only the repeated-register/step loops; it is not a maximum CPU pause duration.
Initial status/register reads, code-window checks and the second interrupt pause
have additional bounded exchanges (two seconds each). Cleanup attempts do
not erase a failed measurement. If a broken connection prevents resume, the run
fails and does not reset or redeploy the board. Physical adapter failure cannot
provide a guaranteed software restoration path; do not run this during real work.

Configuration, flash, firmware, CPU resets, arbitrary memory/register writes,
breakpoints, FIFO consumption, adapter EEPROM changes, uncontrolled GPIO and
arbitrary instruction execution remain unavailable in either mode. C64U USER
access remains prohibited. See the [standards matrix](jtag-verify-standards.md).

A passing transport soak establishes only its observed workload, wiring and
target. Native debug results are scored separately, and do not prove indefinite
reliability or all interactive debugger features. A failed soak also implicates wiring, target power and the host USB path,
not just the adapter. The FT232H uses a different protocol and cannot serve as a
USB-Blaster protocol reference.

## Recorded hardware run, 2026-10-18

The [100-cycle report](evidence/jtag-verify-2026-10-18.json) and
[final one-cycle check](evidence/jtag-verify-final-check-2026-10-18.json) preserve
USB identity, settings, runtime versions and source hashes. The final transport check uses
the transport implementation, including the shared MPSSE release-error cleanup fix and
more precise clock-error attribution added after the longer run began.

| Adapter / target | Observed result | Interpretation |
| --- | --- | --- |
| No-name USB-Blaster I compatible, `09fb:6001@1-6`, U64 Elite I, `02B050DD` | 7,200 scans, 63,677,100 compared bits, zero mismatches/retries in 154.53 s. Short-scan median/max 0.420/3.667 ms. | Passed this transport workload. The later native debug soak failed; see below. |
| Adafruit FT232H, `0403:6014@5-1.4`, C64U | Intermittent USB initialization/clock-control timeouts. The long run passed the MPSSE echo before an operational error; the final check failed during open. | Transport/target compatibility could not be established. These observations do not establish an adapter defect. |

An earlier FT232H attempt returned an invalid identity scan and was rejected
before any explicit IR scan. Its descriptor strings contained U+FFFF characters;
those strings are preserved as received, without attempting EEPROM repair.
No C64U USER chain, CPU control, configuration, firmware or flash operation was
performed. These initial transport runs did not attach a debugger to either board.
The opt-in debug backend was added subsequently; these artifacts do not establish
its live stepping behavior.

Both targets answered read-only `GET /v1/info` before and after testing with the
same product, firmware, firmware commit, FPGA and core versions and empty error
lists. The [health evidence](evidence/jtag-verify-health-2026-10-18.json) records
those selected fields. Reachability and unchanged versions are useful independent
checks; they do not prove preservation of every transient execution state.

The later [C64U capability check](evidence/jtag-verify-c64u-debug-capability-2026-10-18.json)
was run with `--allow-disruptive` to verify the target exclusion. It reports CPU
stepping `UNSUPPORTED` and an independent MPSSE echo error (`fa00`); no target
clocks were emitted after that failed parser check. The C64U remained reachable
with unchanged versions and an empty error list.

### Native debug qualification and failed soak

Before discovering the status-guard defect, the [one-cycle full debug check](evidence/jtag-verify-u64-full-debug-smoke-2026-10-18.json)
completed guarded stepping, register checks, repeated code-window reads,
continue/interrupt and resume health. Earlier qualification exposed a verifier
restoration defect: Intel's acknowledgement of `D` did not resume execution.
The [initial failure](evidence/jtag-verify-u64-debug-initial-2026-10-18.json)
and [explicit-continue check](evidence/jtag-verify-u64-debug-restoration-2026-10-18.json)
are retained. This was a test/backend assumption error, not evidence of a cable fault.
The later status-guard defect also means these earlier successful steps do not
establish safe stepping coverage.

The subsequent [1,000-cycle requested debug soak](evidence/jtag-verify-u64-debug-soak-2026-10-18.json)
**failed at zero-based cycle 116**, after 116 completed cycles, 1,402 stable
register comparisons, 91 instruction-oracle matches, 116 code-window checks and
116 remote interrupts. A code-memory read timed out; the cleanup continue also
timed out, followed by a health timeout. The full exception chain is retained.
The older report's top-level `TRANSPORT_UNTESTABLE` label conflated a debug error
with transport coverage; the current implementation reports `DEBUG_SESSION_FAILED`.
The raw scans in that same run passed. No failed cycle was retried or hidden.

A separate one-shot Intel session subsequently acknowledged explicit continue
and reported the processor running. A further register-only check confirmed a
running-to-paused transition and again resumed it, but network health remained
unavailable. No reset, firmware reload or FPGA reconfiguration was used to hide
or recover this failure. Further disruptive testing stopped because the health
precondition no longer held. The cause is unresolved: these measurements do not
separate cable, vendor backend, target debug behavior and interrupted firmware.
The setup has **not qualified for reliable debugging**. Subsequent binary
analysis also invalidated the original ISR-exclusion guard, as described above.
The verifier defect may have contributed to the firmware health failure; this
run cannot be used as a clean adapter reliability experiment.

A preceding [handoff rejection](evidence/jtag-verify-u64-handoff-rejected-2026-10-18.json)
read an all-ones identity after a native client session and refused explicit IR
or debug scans. A subsequent independent Intel identity query succeeded. This
separate failed observation is retained rather than silently retried.

Hardware breakpoints/watchpoints remain `UNVERIFIED` for the installed bitstream.
The public reference design configures zero such resources; this does not prove
that the running FPGA has the same configuration. Software breakpoints and
memory/register writes remain prohibited. Source-level next/finish/step were not
qualified because no matching ELF was available. C64U stepping remains excluded.

The evidence files contain source hashes for their historical implementations;
older runs are not claimed to execute the final source revision. Neither unchanged
version strings nor loss of network service establishes whether FPGA configuration
was lost. See the [code-driven investigation](jtag-verify-investigation.md).

The [post-soak health record](evidence/jtag-verify-post-soak-health-2026-10-18.json)
preserves the manual continue observations and the unresolved U64 health failure.
The [final-source SAFE check](evidence/jtag-verify-final-source-2026-10-18.json)
again passed 72 U64 raw scans/636,771 bits. FT232H passed its parser echo in
that check, then a target scan timed out; its transport remains unverified. That check did not enter CPU debugging or
establish recovery of the U64 network service.
