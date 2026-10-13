# Experimental UII+L firmware and FPGA builds

This guide is for experienced developers interested in experimenting with the
Ultimate II+L (UII+L / U2+L). It builds firmware and a fresh Lattice ECP5
`LFE5U-25F-6BG256C` bitstream. It is not a demonstrated fix for garbled menu
characters, and does not apply unchanged to the original Ultimate II or II+.

Use this at your own risk. The author takes no responsibility for damage to your
cartridge. Reason carefully about every bitstream change before applying it;
successful compilation and timing checks alone do not establish safe operation.
A JTAG approach is strongly recommended for experimentation. The
[UII+L JTAG tool](u2pl-jtag.md) supports volatile FPGA loading with `--sram`
and persistent FPGA/application recovery with a mandatory full flash backup.
An SRAM experiment runs the application already in flash, so check compatibility.

**Before applying a self-built FPGA image, have a working FT232H JTAG adapter,
verified recovery access to the UII+L itself, a known-good recovery image, and a
recovery procedure you understand.** Follow the existing
[UII+L FT232H wiring](../README.md#uiil-ft232h-jtag-wiring) and
[backup/recovery procedure](u2pl-jtag.md). Even a probe clears the running FPGA
configuration; it does not demonstrate a repair. Verify the board revision,
signal mapping and electrical levels before wiring. The C64U/Ultimate 64 Elite II
Artix JTAG commands and wiring in this repository are not UII+L ECP5 instructions.
An FT232H working on a host C64U does not prove cartridge recovery is working.
Building and inspecting files does not require an adapter; applying them does.

## Install the wrapper

On Linux x86_64, install git and Docker, then install release 0.2.0 or later of
these tools as the README describes, or the current development version:

```bash
git clone https://github.com/GideonZ/1541ultimate.git "$HOME/1541ultimate"
git clone https://github.com/chrisgleissner/1541ultimate-tools.git "$HOME/1541ultimate-tools"
"$HOME/1541ultimate-tools/install.sh" "$HOME/1541ultimate"
```

Use fresh destination directories, or follow the README's update instructions
for existing clones. The wrapper is installed locally into the firmware checkout;
it is not part of upstream firmware. Record both repositories' commit IDs when
reporting a result. The tested local firmware tree matched upstream commit
`1cedb5f0af16fa7354d2300ab08e76d32e71923b`; use that revision in a fresh checkout
if you want to compare the same source with
[Actions run 38090880532](https://github.com/GideonZ/1541ultimate/actions/runs/38090880532).
That run restored its UII+L bitstream from the Actions cache. The bitstream in the
cache was synthesized by
[Actions run 36720378294](https://github.com/GideonZ/1541ultimate/actions/runs/36720378294)
(commit `1e17efcb`, which has the same `fpga/` sources), whose log reports a cache
miss for the `u2pl` key.

## Diamond and licenses

1. Download the full **Diamond 3.13 64-bit Linux installer** from the
   [Lattice Software Archive](https://www.latticesemi.com/Support/SoftwareArchive),
   using your own Lattice account. The tested RPM was
   `diamond_3_13-base-56-2-x86_64-linux.rpm` (about 1.7 GB), version
   **3.13.0.56.2**. The small Diamond 3.2 *Encryption Pack* is a different product.
   Install according to Lattice's instructions. For the commands below, arrange
   the installation so that
   `$HOME/fpga-tools/lscc/diamond/3.13/bin/lin64/diamondc` exists. You can instead
   use a supported directory layout listed in `build-tool.d/docker.sh` and adjust
   `--build-tools-dir` to its parent root.
2. In Lattice's licensing portal, request **Diamond-Base-NL** (Diamond Base,
   node-locked) and **LATTICE-SYNPLIFY-NL** (SynplifyPro Lattice FPGA Edition,
   node-locked). This project's synthesis uses Synplify; Diamond Base alone was
   insufficient. Use your own machine's Ethernet host ID as requested by the
   portal. Registration/entitlement IDs are not license files. If registration
   fails or a serial-number field is unclear, contact Lattice Licensing Support.
   Support had to resolve portal access for the tested setup.
3. Download both generated license files. Store them outside both repositories
   and the Docker build context. Combine the complete files without editing
   signed features, using your own filenames:

   ```bash
   mkdir -p "$HOME/.local/share/lattice"
   chmod 700 "$HOME/.local/share/lattice"
   # First place your two downloaded files in that private directory.
   (
     umask 077
     cat "$HOME/.local/share/lattice/diamond.lic" \
         "$HOME/.local/share/lattice/synplify.lic" \
       > "$HOME/.local/share/lattice/combined.lic"
   )
   chmod 600 "$HOME/.local/share/lattice/combined.lic"
   ```

Never post license files, signed feature lines, activation/registration keys,
account identifiers, NIC addresses or personal support correspondence. Do not
put licenses in Docker images. The wrapper mounts the supplied license read-only
and sets its container path for both `LATTICE_LICENSE_FILE` and `LM_LICENSE_FILE`.
On the tested Linux/rootless Docker setup, host networking exposed the physical
Ethernet host ID required by the license. Verify host-ID matching on your setup;
do not change signed licenses or spoof a host ID.

Diamond 3.14 was tried but rejected because its PAR result assigned RISC-V data
signals to primary clock resources, which the project's boot-safety check forbids.
That is evidence for using the tested 3.13 toolchain here, not proof that every
3.14 build fails. Do not bypass the guard to obtain an updater.

## Build once

```bash
cd "$HOME/1541ultimate"
mkdir -p scratch/u2pl-build
touch scratch/u2pl-build/start
LATTICE_LICENSE_FILE="$HOME/.local/share/lattice/combined.lic" \
BUILD_TOOL_ALLOW_PARTIAL=1 \
./build-tool --build-tools-dir "$HOME/fpga-tools" -j 8 u2pl \
  > scratch/u2pl-build/build.log 2>&1
```

This is the **single firmware build command**. The wrapper updates submodules and
prepares `1541u-build:latest` from `ghcr.io/gideonz/riscv:latest` on a fresh setup,
including ESP-IDF tools and Synplify's required OpenGL/GStreamer/X11 runtime
libraries. Image preparation and tool/license installation add time beyond the
firmware build. A build that synthesizes the ECP5 always uses this prepared image,
even when a legacy `my_docker_image` exists, because the legacy image lacks
`libgthread-2.0.so.0` and Diamond fails to start in it. A custom image passed with
`--image` must contain those dependencies too (see `build-tool.d/image.sh` for the
package list). Check which image the log selects. Do not bake licenses into a
custom image.

The image supplies the RISC-V toolchain. An optional host toolchain at
`<build-tools-dir>/riscv` overrides it and must pass the wrapper's pinned GCC
10.2.0 checks. Record the actual compiler version and image digest for comparisons.
The `--build-tools-dir` directory is mounted read-only; use a dedicated toolchain
directory rather than exposing your entire home directory to the build container.

`--check-support u2pl` is an optional prerequisite check, not a second firmware
build or a full license checkout test. A plain `./build` does not cover `u2pl`.
Do **not** add `-s` when requesting fresh FPGA synthesis: it reuses the cached
bitstream. Preserve any existing `.bit` before building, and use a new evidence
directory/start stamp for each run so stale outputs cannot look fresh.

The full target builds the ESP32-C3 firmware, bootloader, FPGA, RISC-V application
and updater. The result is `update_<commit>.u2l` in the firmware checkout root;
the command above does not deploy it. A measured build with `-j8` took
**47 minutes 17 seconds** on an i7-6700K (4 cores / 8 threads), 46 GiB usable RAM,
Ubuntu 24.04, and an Ubuntu 22.04 Docker image. That excludes installation,
licensing and deployment. Mapping can appear silent while consuming one CPU core.

Most of that time is two Diamond steps. Map took 22 minutes on one core, and Diamond
has no option to spread it over more. Place and route took 21 minutes, because the
project strategy runs three placement seeds (cost tables 4, 5 and 6) one after
another and keeps the best result.

### Optional: run the three placement seeds at the same time

Diamond runs the three seeds concurrently when the strategy names a node list.
This edits a tracked upstream file, so restore it after the build and never commit
it. In `target/fpga/u2plus_ecp5/neorv321.sty` set:

```xml
<Property name="PROP_PAR_ParMultiNodeList" value="/tmp/par_nodelist.txt" time="0"/>
```

The wrapper writes that node list into the build container on every Diamond build,
naming the container's host name and three cores. Without the strategy edit
nothing reads it. A build run outside the wrapper has to write the file itself.

Diamond then passes `-m /tmp/par_nodelist.txt` to PAR and starts jobs `5_4`,
`5_5` and `5_6` together. The result does not change: from the same Map netlist,
the concurrent run produced the same seed ranking and slack figures, and a
bitstream identical to the one Actions run 36720378294 synthesized (apart from
the `Date:` field). A second design gave a timing report identical to its
sequential run. The run only finishes sooner when three cores are free. On the
measured machine it took 14 minutes with other builds running, and 28 minutes
when three other Diamond builds were competing for the same four cores.

## Verify before applying anything

- Require exit status zero and fresh, nonempty outputs newer than the start stamp:
  `target/fpga/u2plus_ecp5/impl1/u2p_ecp5_impl1.twr`, its `.bit`,
  `target/u2plus_L/riscv/ultimate/result/ultimate.app`, and the root `.u2l`.
- Inspect the final Diamond reports, not just an artifact's existence. Require
  `SUCCESS: Timing requirements met.`, zero final setup/hold timing errors, no
  unrouted connections, and a successful Bitgen DRC. Assess warnings separately.
  The tested run had intermediate hold warnings, but final trace reported zero
  setup/hold errors.
- Require the FPGA Makefile's `BAD='0'`, and inspect all PAR primary-resource
  warning blocks. RISC-V data nets on primary clock resources are a known cause
  of non-booting images. Do not disable timing or clock-resource checks or alter
  constraints merely to obtain a build that passes.
- Check the `.bit` header for `LFE5U-25F-6CABGA256`, Diamond's package spelling of
  the requested part. Verify that the updater contains the exact freshly generated
  bitstream and application and that their sizes fit the target flash layout.
  Record `sha256sum` of the `.bit`, `.app`, and `.u2l` before deployment.
- Compare with an official UII+L factory artifact from the same source tree.
  Inspect whether Actions synthesized FPGA or restored a cache; only a run whose
  log reports a cache miss for the `u2pl` key synthesized its own bitstream. The
  tested fresh local `.bit` matched the bitstream synthesized by run 36720378294 in
  every byte after normalizing **only** the NUL-terminated `Date:` field: all other
  metadata, CRC and the complete configuration payload/trailer were identical.
  Different payloads do not establish equivalence just because the RTL is equal.
  The CI artifact had no post-route simulation netlist; no comparative behavioral
  simulation was performed or claimed.
- Expect a changed design to need its own timing check. Setting only the FPGA
  version generic of `ultimate_logic_32` in `u2p_riscv_lattice.vhd` re-places the
  whole design: version `X"27"` missed hold timing by 0.373 ns on a
  `sys_clock` to `half_clock` path in all three seeds, while `X"7D"` met timing.
  Two different build containers gave identical timing reports for `X"27"`, so the
  failure belongs to the design, not to the local setup.

A failed build can leave a rejected `.bit` behind. Preserve it and the failure
reports separately, then restore the saved known-good cache only after all build
processes stop; otherwise a later `-s u2pl` can package the rejected image. The
bootloader build can regenerate tracked `bootrom_pkg.vhd`; preserve the generated
copy and restore only changes proven to be made by the build, after packaging.

Two local images were deployed through the normal updater, each followed by a
power cycle: the unmodified build, and a build with the FPGA version generic set to
`X"7D"`. With the second image, `/v1/info` reports `fpga_version` `17D`, which shows
the cartridge runs the locally synthesized bitstream rather than an Actions one.
Both passed REST, FTP, Telnet, identity and DMA checks, and the `X"7D"` image also
passed the firmware repository's `run-tests --profile=smoke u2@c64u` (15 suites).
These deployments did not validate UII+L JTAG recovery,
prove that every future source/tool version is safe, or establish that garbled menu
characters are fixed. This guide intentionally stops at build verification; do
not apply an image until the recovery prerequisites and the particular bitstream
changes have been reviewed.
