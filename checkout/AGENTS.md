<!-- Installed by 1541ultimate-tools; the next install replaces this file and keeps a locally edited copy as AGENTS.md.local-<time>. -->
# 1541 Ultimate: agent guide

Firmware for the 1541 Ultimate family: FPGA bitstreams, embedded C/C++ (RISC-V, Nios II) and an ESP32 WiFi coprocessor. Upstream is GideonZ/1541ultimate. The maintainer merges pull requests by hand, so keep each one small and easy to review. Read `CONTRIBUTING.md` first: bigger changes start with an issue, and FPGA changes go through the maintainer.

If `AGENTS.local.md` exists in the checkout root, read it too. It holds machine- and contributor-specific context (devices, addresses, power control, other trees). Never copy its contents into commits, PRs, issues or any tracked file. A template is in `tooling/docs/agents/AGENTS.local.example.md`.

The tools in this checkout (`build`, `build-tool`, `tooling/`) come from [1541ultimate-tools](https://github.com/chrisgleissner/1541ultimate-tools) and are git-excluded. Never commit them, and never edit tracked files to work around a tooling problem. Open a procedure in `tooling/docs/agents/` only when its task comes up, and run a tool with `--help` before guessing options.
The skills `c64u-jtag-run`, `u2pl-recover`, `u64-flash-unattended`, `pr-minimal-diff`, `vhdl-analysis` and
`device-failure-triage` are installed in `.claude/skills/` and `.agents/skills/`; use one when its task comes up.

## Targets

| Target | Output | Hardware |
|---|---|---|
| `u64` | `update_<id>.u64` | Ultimate 64, Elite (Nios II) |
| `u64ii` (`c64u`) | `update_<id>.ue2` | Elite II, C64 Ultimate (RISC-V, Artix-7) |
| `u2` | `update_<id>.u2r` | Ultimate II (RISC-V) |
| `u2plus` | `update_<id>.u2p` | Ultimate II+ (Nios II) |
| `u2pl` | `update_<id>.u2l` | Ultimate II+L (RISC-V, ECP5) |

The `U64` macro is 1 on `u64` and 2 on `u64ii`; those share almost all hardware.

## Tools

Run from the checkout root. Reference docs, in the tools repository at `{{TOOLS_REF}}` (`https://github.com/chrisgleissner/1541ultimate-tools/blob/{{TOOLS_REF}}/docs/<name>`): `c64u-jtag.md`, `u64-jtag-deploy.md`, `u64-unattended-flash.md`, `u2pl-jtag.md`, `u2pl-build.md`.

| Entry point | Purpose |
|---|---|
| `./build-tool` | Builds targets in Docker; optional FTP or JTAG deploy; `--apply-pr N` builds an upstream PR in a throwaway worktree |
| `./build` | Full sweep: clean, host tests, `u64`, JTAG and FTP deploy, `u64ii`, `u2` |
| `tooling/build_and_deploy_u64.sh` | Runs the built U64 ELF from RAM over USB-Blaster (volatile, about 30 s) |
| `tooling/flash_u64.py` | Flashes a U64 Elite unattended |
| `tooling/u64ii_jtag.sh` | C64U / U64E2 over FT232H: `probe`, `run`, `console`, `dump`, `fpga`, `reset`, `recover` |
| `tooling/u64ii_gdb.sh` | Read-only gdb on a running C64U / U64E2 application |
| `tooling/u64ii_menu.py` | Reads and drives the menu over Telnet |
| `tooling/c64u_monitor.py` | Watches a C64U's video stream, REST status and JTAG console |
| `tooling/c64u_mode.py` | Switches a C64U between firmware setups; power cycle only with `--allow-power-cycle` |
| `tooling/u2pl_jtag.py`, `u2pl_jtag_sample.py` | U2+L backup, recovery and startup diagnosis over FT232H |
| `tooling/jtag_verify.sh` | Non-disruptive checks of a JTAG adapter |

## Devices

Hosts come from the environment or `.build-tool.env` (copy `.build-tool.env.example`): `U64_VERIFY_HOST`, `U64II_VERIFY_HOST`, `DEPLOY_HOST`, `U64II_JTAG_URL`, `INTEL_FPGA_ROOT`. The test runner reads `U64_HOST`. Below, `$U64_HOST` is the device under test and `$C64U_HOST` a C64 Ultimate. Never write an address, hostname or serial number into a tracked file, commit message or PR.

Deploy only when the task asks for it, to a device nobody else is using. Do not power off or cycle a device unless the task says so.

## Build

- `./build-tool --check-support` shows what can be built here. `BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool u64ii` builds one target; the flag is needed for any list lacking one of `u64`, `u64ii`, `u2`. `-s` builds software only against cached FPGA images.
- Doc-only changes need no build; otherwise run the narrowest validation for what you touched.
- **A green device run does not prove the code compiles.** Shared files under `software/io/` and `software/monitor/` are built by every target, and only the `u64*` makefiles define `U64`. A wrapper placed after an `#endif` instead of before it once broke every non-U64 target while a U64 stayed green.
- Before you report firmware work complete, build every target: `./build-tool -s u64 u64ii u2 u2pl`. `u2pl -s` needs a cached `target/fpga/u2plus_ecp5/impl1/u2p_ecp5_impl1.bit`. `u2 -s` can stop at `No rule to make target 'rv700dd.chk'` when cached FPGA artifacts are missing; that is not a code error if `target/u2/riscv/ultimate` compiled.
- `./build` covers neither `u2plus` nor `u2pl`. For shared code used by U2+ Nios images, also run `make u2plus_swonly` (`tooling/docs/agents/firmware-lessons.md`).
- Bitstreams in `external/` are the maintainer's: do not rebuild or replace one for a software task. A U2+L with a mismatched ECP5 bitstream does not boot.

## Run on a device

- **U64:** `./build-tool --jtag u64` builds and runs from RAM; a power cycle returns to the flashed firmware. Never reconfigure the FPGA (`quartus_pgm`) in the deploy loop: it left a board with no video. The image has no JTAG UART; read firmware output through syslog (`device-invariants.md`). For a wedged U64 use JTAG gdb, not guesses: `tooling/docs/agents/jtag-debug.md`.
- **C64U / U64E2:** run `tooling/u64ii_jtag.sh probe` first and after any wiring change. It must show IDCODE `0x?362C093` (50T) or `0x?3631093` (100T) and user ID `0xDEAD1541`. `./build-tool --jtag c64u` then builds only `ultimate.bin`, reconfigures the FPGA and runs it; `--jtag-monitor c64u` reads the CPU's UART.
  - Power the FT232H over USB before switching the C64U on: an unpowered adapter on P5 once gave a rolling HDMI picture at boot.
  - The instruction cache keeps its tags across a CPU-only reset, so the tool boots through a NOP trampoline at `0x8000`. Never jump to `0x30000` directly after a CPU reset: the image runs stale code and dies silently.
  - Every JTAG command takes the c64u device lock. Never bypass or delete it, and never run `ecpprog` and a JTAG tool at once.
  - Run `tooling/c64u_monitor.py` during C64U work and watch its ALERT and WARN lines. Stop it with `--stop`, never `pkill -f`. Its video stream must be multicast.
  - A board that will not boot: `u64ii_jtag.sh recover`, then leave with `fpga`, never `reset` (the flashed firmware crashes on the kit's FPGA image).
- **U2+L:** it cannot flash itself unattended. Every key and the final power cycle go through the C64U it is plugged into: `tooling/docs/agents/u2pl-deploy.md`. A cartridge that does not boot: `u2pl-jtag.md` in the tools docs.
- **Other trees, upstream PRs:** `./build-tool --repo-dir <tree> c64u`; `./build-tool --apply-pr N --pr-base <ref> --jtag c64u`.

## Tests

- Host unit tests sit next to their code (for example `software/api/tests/`). Run the existing tests for what you change and add tests for non-trivial logic.
- Hardware end-to-end suites are the release gate: `./run-tests --list`, `./run-tests -s <suite> $U64_HOST`. `python3 tests/lib/health.py -H $U64_HOST` shows which listener answers. See `tests/README.md`.
- **New or changed e2e suite:** a suite that `./run-tests` cannot select is in no gate. Add a `Suite(...)` entry in `run-tests` (profile and a one-line comment saying what it proves), regenerate the matrix with `python3 tools/docs/update_test_docs.py`, and update the README prose. Before the first commit run `python3 tests/lib/registry_test.py` and `./run-tests --list | grep <name>`.
- **Red/green on hardware, per defect.** Run the test on firmware that still has the defect and record the failure. Apply the fix, rebuild, deploy, run the same test unchanged, and record the pass. With several fixes in one change, revert exactly one, rebuild, deploy and confirm only that defect's check fails: the first failure masks the others. If a defect is real but silent, say so and leave the code alone. Never claim red/green for what was only reasoned about.
- Ground every expected value in the firmware source and confirm it on the device; the manuals under `doc/` have drifted.
- **A failing check is a test defect until proven otherwise.** Verify independently that it measures what it claims, then compare with a tree where it passes, at the level of the code path. Report a firmware defect only after that.
- **Check device invariants after a green suite.** A passing suite can leave the device damaged for the next one. Probe a known-good constant between suites; never take a baseline from the device. Which memory primitive to write with, and how to read C64 memory: `tooling/docs/agents/device-invariants.md`.
- `machine:input`: `restore` is wired to NMI, not to the matrix, so only `tap` affects it and it must stand alone in its entry. Keys reach a C64U's keyboard matrix only while the C64 runs and its own menu is closed (`machine:menu_screen` answers 404).

## Code standards

1. Match the surrounding style. Touch only what the task needs, reuse small local code, keep the diff minimal. Refactor only when no direct fix is clean. Make the smallest compatibility-preserving fix for the reproduced bug, and add no speculative helpers.
2. Keep use-case behaviour out of shared code. Localise a workflow-specific feature in its own module. Use a derived type or narrow hook when behaviour varies by type, and a local conditional when it varies by runtime state. Share data through a neutral accessor; never copy tables.
3. Keep feature-specific locks, `volatile` and barriers out of shared code; document a benign bounded race at the call site.
4. Do not limit a fix to one hardware target without a hardware reason; a missing device is not one. `#if U64 == 1` excludes the Elite II and C64 Ultimate, which usually need the same fix. Say in the PR which targets you verified on hardware.
5. When the defect is in a shared primitive, say in one PR sentence where the fix belongs. Fix the shared thing when its callers expect the correct behaviour. Fix it separately, naming the call sites you cannot test, when correcting it changes them. Write local code only when the primitive cannot express what you need. Never route around a broken primitive.
6. Formatting goes through `software/system/small_printf.cc`, which has no length modifiers (`%l`, `%ll`, `%z`): cast to `int` or print halves. Unsupported libc formatting pulls newlib into RISC-V links (missing `_kill`, `_getpid`). Its `sscanf` ignores literal characters and has no `%c`.
7. Comments say why, in one or two lines. An address or measurement is worth a line when it is the evidence for the decision.
8. Use exact-match edits, never a script that rewrites the whole file. Many files under `software/io/` use CRLF, and Python `write_text()` turns a 3-line fix into a 1000-line diff.

## Git

- Commit messages: one short subject line, at most one short context line. No bullet lists. One focused, buildable change per commit.
- Push once, when the work is finished and tests pass. Every push starts a paid CI build.
- **Never `git stash`.** On a clean tree it does nothing, and the following `git stash pop` applies an unrelated old entry and conflicts in hundreds of files. To test the base, use `git worktree add`, `git diff` or `git show <base>:path`.
- **Never commit a binary.** Builds leave artifacts in the working tree, and a committed blob stays in history after a removal commit. Stage by path; never `git add -A` or `git add .` in a built tree. Check `git diff --cached --stat`: a `Bin` line or an unexpected file means stop. Build output (`*.app`, `*.elf`, `*.bin`, `*.o`, `*.u64`, `*.u2l`, `*.ue2`, `*.bit`, `*.rbf`, `*.sof`) is not committed; a few files tracked under `external/` and `recovery/` change only deliberately, said out loud. A blob already committed needs `git filter-branch --index-filter 'git rm --cached --ignore-unmatch <path>' <base>..HEAD` and `git push --force-with-lease`; say so in the report.

## Write-ups

- **Describe the code as it stands, not its history**, in comments, docs, OpenAPI and help text, test docs and summaries. Never compare the current state with an earlier one. Banned: "previously", "used to", "no longer", "now does", "instead of the old". Wrong: "a tap of commodore and restore is one request for what used to need three." Right: "a tap of commodore and restore puts the C= key down in the matrix before it raises the restore line, so the combination is one request."
- Only the PR description and commit message may describe change. There, state the previous behaviour, the defect, the implemented change and the verification as separate statements, in plain engineering language.
- Be short: say the one thing the reader cannot get from the code, then stop.
