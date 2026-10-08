# 1541ultimate-tools

Build and deployment tooling for the [1541 Ultimate](https://github.com/GideonZ/1541ultimate)
firmware. These files are not part of that repository. They overlay onto a checkout of
it and provide:

- `build-tool` - a builder for every firmware target, compiling inside Docker, with
  optional FTP deploy, JTAG deploy, and device monitoring.
- `build` - an orchestrator that runs the full sweep: clean, host unit tests, build,
  deploy, then the remaining targets.
- `tooling/build_and_deploy_u64.sh` - a fast JTAG redeploy of an already-built U64
  application, used both by hand and by the end-to-end test suites.
- `tooling/u64ii_jtag.sh` and `tooling/build_and_deploy_u64ii.sh` - JTAG for the C64
  Ultimate and Ultimate 64 Elite II through an FT232H or a USB-Blaster: run an application or an FPGA
  image from RAM, read the console and memory. Nothing is flashed.
- `vivado/install.sh` - an unattended install of AMD Vivado 2024.1 with Artix-7 support
  only, including the AMD login, for building the Artix-7 FPGA images.
- `patches/` - optional patches against the upstream repository.

`docs/u64-jtag-deploy.md` explains what the U64 JTAG deploy does and, more importantly,
what it deliberately does not do. `docs/c64u-jtag.md` covers the C64 Ultimate and
Ultimate 64 Elite II, whose FPGA, CPU and JTAG path are different.
`docs/vivado-install.md` covers the Vivado install.

## Installing into a checkout

These files overlay onto the root of a 1541ultimate checkout:

```bash
git clone https://github.com/GideonZ/1541ultimate.git
git clone https://github.com/chrisgleissner/1541ultimate-tools.git

cd 1541ultimate
cp -r ../1541ultimate-tools/build \
      ../1541ultimate-tools/build.cmd \
      ../1541ultimate-tools/build-tool \
      ../1541ultimate-tools/build-tool.d \
      ../1541ultimate-tools/tooling \
      ../1541ultimate-tools/.build-tool.env.example .
chmod +x build build-tool tooling/*.sh tooling/*.py
```

The resulting layout:

```
1541ultimate/
├── build                 orchestrator
├── build.cmd             Windows shim for build
├── build-tool            the builder
├── build-tool.d/         helper libraries build-tool sources at startup
├── .build-tool.env       optional per-checkout settings (copy the .example)
└── tooling/
    ├── build_and_deploy_u64.sh      U64: run the built ELF via nios2-download
    ├── read_u64_jtag_terminal.sh    U64: nios2-terminal capture
    ├── read_u64_uart_terminal.sh    U64: debug UART through a USB-TTL adapter
    ├── build_and_deploy_u64ii.sh    C64U / U64E-II: run the built ultimate.bin
    ├── read_u64ii_jtag_terminal.sh  C64U / U64E-II: console over JTAG
    ├── u64ii_jtag.sh                C64U / U64E-II: JTAG tool (pyftdi venv)
    ├── u64ii_jtag.py
    ├── test_u64ii_jtag.py           host tests against a simulated FT232H and USB-Blaster
    ├── test_apply_pr.sh             host tests for apply_pr.sh
    ├── c64u_monitor.py              video stream, REST and console watcher
    ├── u64ii_gdb.sh                 gdb over JTAG: tasks as threads, backtraces
    ├── u64ii_gdbstub.py             the gdb remote server it starts
    ├── u64ii_gdb_unwind.py          unwinder for gdb's missing frames
    ├── test_u64ii_gdbstub.py        host tests for the server
    └── apply_pr.sh                  worktree with upstream PRs applied, uncommitted
```

`build-tool` will not start without `build-tool.d/` beside it.

## Keeping these files out of the upstream repository

None of these paths are in the upstream `.gitignore`, so after copying them in they
show up as untracked files and can be committed by accident. Exclude them locally
rather than by editing the tracked `.gitignore`, which would be an unwanted change to
the upstream repository:

```bash
cd 1541ultimate
cat >> .git/info/exclude <<'IGNORE'
build
build-tool
build.cmd
tooling/
.build-tool.env
.build-tool.env.example
IGNORE
```

`.git/info/exclude` uses the same syntax as `.gitignore` but is per-checkout and is
never committed, so this affects nobody else. Confirm it worked:

```bash
git status --porcelain     # should print nothing
```

`build-tool.d/` needs no entry. The upstream `.gitignore` has a `*.d` rule for
dependency files, which already covers it.

## Prerequisites

| Requirement | Needed for |
|---|---|
| Docker | Every RISC-V target (u2, u2pl, u64ii) |
| Quartus and Nios II EDS, on the host | The Nios II targets (u64, u2plus). The RISC-V image has no Nios toolchain, so these cannot build inside Docker. |
| USB-Blaster | U64 JTAG deploy and JTAG monitoring; C64 Ultimate / Ultimate 64 Elite II JTAG with `U64II_JTAG_URL=blaster` |
| FT232H (e.g. Adafruit) and Python 3 | C64 Ultimate / Ultimate 64 Elite II JTAG only; pyftdi is installed into a virtual environment on first use |
| Lattice Diamond | Full `u2pl` FPGA synthesis only. See the u2pl note below. |

`build-tool` derives a prepared image `1541u-build:latest` from
`ghcr.io/gideonz/riscv:latest` the first time it runs.

Check what is available before starting a long build:

```bash
./build-tool --check-support
./build-tool --list-targets
```

## Targets

```
Target   Output          Toolchain required
-------  --------------  ------------------------------------------
u2       update.u2r      RISC-V + Xilinx ISE (sw-only: cached FPGA)
u2plus   update.u2p      Nios2 + Quartus (sw-only: cached FPGA)
u2pl     update.u2l      RISC-V + Lattice Diamond + ESP32-C3
u64      update.u64      Nios2 + Quartus + ESP32
u64ii    update.ue2      RISC-V + ESP32-S3 (alias c64u; FPGA from external/)
all      (all above)     builds u64 u64ii u2 (default)
```

Short aliases are accepted: `ue2` and `c64u` for `u64ii`, `u2l` for `u2pl`. The C64
Ultimate is Ultimate 64 Elite II hardware and uses the same target.

## Usage

Build one or more targets:

```bash
./build-tool u64
./build-tool u64ii
./build-tool u64ii u64          # several in one invocation
./build-tool -s u2pl            # software only, using a cached FPGA bitstream
./build-tool --parallel         # default target set, concurrently
```

Build and deploy the U64 over JTAG in one step:

```bash
./build-tool --jtag u64
```

Or separately, which is the faster loop when only the application changed:

```bash
./build-tool u64
bash tooling/build_and_deploy_u64.sh     # about 30 seconds
```

The deploy script takes no arguments and reads one fixed path:

```
target/u64/nios2/ultimate/result/ultimate.elf
```

It fails immediately if that file is absent, so build first. Set `INTEL_FPGA_ROOT` if
the Intel FPGA tools are somewhere the script does not find on its own:

```bash
INTEL_FPGA_ROOT=/opt/intelFPGA_lite/19.1 bash tooling/build_and_deploy_u64.sh
```

Run a C64 Ultimate or Ultimate 64 Elite II application from RAM over JTAG (FT232H or USB-Blaster;
see `docs/c64u-jtag.md` for wiring and the first `probe`):

```bash
./build-tool --jtag c64u                     # build ultimate.bin, run it from RAM
./build-tool --jtag c64u --jtag-fpga warm    # keep the FPGA image, restart only the CPU
./build-tool --jtag-monitor c64u             # the application's console output
tooling/u64ii_jtag.sh probe                  # identify the board; changes nothing
```

Run a pull request without touching your checkout; build-tool applies it in a
throwaway worktree and builds that (see `docs/c64u-jtag.md`, "Running a pull request"):

```bash
./build-tool --apply-pr 705 --pr-base upstream/master --jtag c64u
```

Build another firmware tree with the same layout, for example a C64 Ultimate firmware
tree, with `--repo-dir`; build-tool builds its ESP32-S3 firmware first when needed:

```bash
./build-tool --repo-dir ../other-tree c64u
```

Run the full sweep, which cleans first, runs the host unit tests, and checks each
artifact's size against an expected range:

```bash
./build                  # u64 -> JTAG -> FTP -> u64ii -> u2
./build --parallel       # u64 and u64ii concurrently, u2 last
```

`build` takes no target argument. It is slower than calling `build-tool` directly, so
use it for a full check rather than for iteration.

## Device-specific configuration

Deploy and verification steps need to know which device to talk to. Nothing is
hardcoded; each setting is read from the environment, or from `.build-tool.env` beside
the scripts (see `.build-tool.env.example`), and each one is skipped rather than
failed when unset.

| Variable | Used by | Effect when unset |
|---|---|---|
| `DEPLOY_HOST` | `build`, FTP deploy step | Step is skipped and reported as `SKIPPED` |
| `DEPLOY_PATH` | `build`, FTP deploy step | Defaults to `/Usb1/firmware/u64/custom` |
| `U64_VERIFY_HOST` | post-deploy REST check for u64 | Verification is skipped with a warning |
| `U64II_VERIFY_HOST` | post-deploy REST check for u64ii | Verification is skipped with a warning |
| `U64II_JTAG_URL` | FT232H for u64ii JTAG, or `blaster` for a USB-Blaster | `ftdi://ftdi:232h/1` |
| `INTEL_FPGA_ROOT` | U64 JTAG deploy and monitor | Searched under `~/intelFPGA_lite`, `~/altera_lite`, `/opt/...` |

```bash
export DEPLOY_HOST=my-u64          # hostname or IP
export U64_VERIFY_HOST=my-u64
./build
```

`build-tool` also takes these per invocation:

```bash
./build-tool --deploy-only -d u64 --deploy-host <device-hostname>
./build-tool u64ii -d u64ii --deploy-host <device-ip> --verify-host <device-ip>
```

There are no credentials to configure. The Ultimate's FTP server accepts anonymous
logins, which is why the deploy defaults are user `anonymous` with an empty password.

## Notes on individual targets

### u2pl cannot be built end to end without Lattice Diamond

The full `u2pl` target runs `target/fpga/u2plus_ecp5`, which invokes `diamondc`.
Diamond is proprietary and is not present in any of the build images, so the full
target always fails at the FPGA step.

The working path is `-s` (`--sw-only`), which maps to the `u2pl_swonly` make target.
That embeds an existing ECP5 bitstream instead of synthesising one. The bitstream is
gitignored and never in the upstream repository, so it has to be supplied at:

```
target/fpga/u2plus_ecp5/impl1/u2p_ecp5_impl1.bit
```

Take it from a CI `factory_package_u2pl_*` artifact:

```bash
gh run download <RUN_ID> --repo GideonZ/1541ultimate \
    -n factory_package_u2pl_<version> -D /tmp/u2pl
cp /tmp/u2pl/target/fpga/u2plus_ecp5/impl1/u2p_ecp5_impl1.bit \
   target/fpga/u2plus_ecp5/impl1/
```

`./build-tool -s u2pl --check-support` reports u2pl as supported once that file is in
place. Without it the build fails late, in the updater step, with
`No rule to make target '.../u2p_ecp5_impl1.bit'`.

### The RISC-V targets can fail for reasons unrelated to your changes

`u64ii` and `u2pl` currently fail against the `ghcr.io/gideonz/riscv:latest` base with:

```
software/io/c64/c64_crt.cc:19: error: conflicting declaration 'uint8_t __cart_rom_start'
software/io/c64/c64.h:420: note: previous declaration as 'uint8_t __cart_rom_start [1048576]'
```

This reproduces on an unmodified upstream branch, so it is pre-existing rather than
caused by a local change. Upstream CI stays green because its self-hosted runner uses
a different image whose g++ is less strict. Before treating a failure here as a
regression, build the same target from a clean checkout and compare.

To build them with the toolchain upstream CI uses, place a `riscv32-unknown-elf` GCC
10.2.0 at `riscv/` inside the build tools directory (`--build-tools-dir`), so that
`<build tools>/riscv/bin/riscv32-unknown-elf-g++` exists. `build-tool` then puts it
ahead of the image's toolchain for u2, u2pl and u64ii, and stops if its version or
checksum is not the pinned CI build. Without that directory the image's toolchain is
used, and no build tools directory is needed for these targets.

`BUILD_TOOL_ALLOW_PARTIAL=1` lets a multi-target run continue past one failing target
instead of stopping at the first.

### ESP-IDF

The full `u64` and `u64ii` targets depend on ESP32 firmware (`esp32_raw_u64` and
`esp32_u64ctrl`). Without an ESP-IDF installation, use the upstream make targets that
skip that step:

```bash
make u64_no_esp
make u64ii_no_esp
```

Both still produce the application binary the deploy steps need.

## Patches

`patches/0001-add-u64_swapply-make-target.patch` adds a `u64_swapply` target to the
upstream Makefile, mirroring the existing `u2plus_swapply`: it builds the U64
application and then downloads it over JTAG in one step.

```bash
cd 1541ultimate
git apply ../1541ultimate-tools/patches/0001-add-u64_swapply-make-target.patch
make u64_swapply
```

This is a modification to a tracked file, so revert it with `git checkout Makefile`
before pushing a branch upstream.
