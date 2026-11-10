# How the U64 JTAG deploy works

`tooling/build_and_deploy_u64.sh` swaps the Nios II application on a running U64 over
JTAG. It is the fast path for iterating on firmware changes: about 30 seconds, against
a board that is already up.

The important part is what it does not do. It does not reconfigure the FPGA, and it
does not hand off to a bootloader. Understanding why saves time when the loop appears
to misbehave.

## What the script does

The whole script is setup around a single command:

1. Resolve the repository root from the script's own location.
2. Locate the Intel FPGA tools, export `QUARTUS_ROOTDIR` and `QSYS_ROOTDIR`, and
   prepend the `quartus/bin`, `nios2eds/bin`, Nios GNU toolchain, `nios2eds/sdk2/bin`
   and `sopc_builder/bin` directories to `PATH`.
3. Refuse to run if given arguments, if the ELF is missing, or if `jtagconfig` or
   `nios2-download` are absent.
4. Run `jtagconfig` and require at least one cable in the output.
5. Run `nios2-download -g target/u64/nios2/ultimate/result/ultimate.elf`.

Three deliberate omissions:

- **It does not build.** The ELF must already exist. This is intentional: the
  end-to-end test suites call the script as a fast recovery step, and a build must not
  start in the middle of a test run.
- **It does not run `quartus_pgm`,** and does not touch the FPGA configuration.
- **It does not hand off to a bootloader.** There is no load address and no magic
  value.

## Why no bootloader handoff is needed

DDR2 calibration on the U64 happens well before a JTAG download, and it is not
something the deploy step can or should perform.

`target/u64/nios2/boot/Makefile` builds `u64_boot.c` and `ddr2_calibrator_u64.c` into
a 4 KB on-chip memory image:

```
DEST     =  ../../../../target/u64_a4/onchip_mem.hex
HEXBASE  =  0x30000000
HEXEND   =  0x30000FFF
```

That hex file is the initialization content of the Nios on-chip RAM inside the FPGA
image. It runs out of reset every time the FPGA is configured, before anything can be
downloaded over JTAG. `software/system/u64_boot.c` shows what it does:

```c
int main()
{
    ddr2_calibrate();
    outbyte('*');
    uint32_t flash_addr = 0x290000;
    /* read dest, length, run_address from SPI flash, copy image into DDR2 */
    jump_run(run_address);
}
```

So the U64 boot sequence is: configure the FPGA, calibrate DDR2 from on-chip RAM, copy
the application from SPI flash at `0x290000` into DDR2, jump to it.

By the time a USB-Blaster is attached to a normally booted board, DDR2 is calibrated
and the C64 core is running. `nios2-download -g` halts the Nios, writes the ELF into
that already-working DDR2, and starts it.

Two consequences worth stating explicitly:

- **The U64 application does not calibrate DDR2 itself.**
  `target/u64/nios2/ultimate/Makefile` links `alt_do_ctors.c`. A sibling file,
  `software/portable/nios/alt_do_ctors_with_calibration.c`, calls `ddr2_calibrate()`
  before running the C++ constructors, and the U64 application does not link it. The
  application image assumes calibrated DRAM.
- **There is no handoff protocol on the U64 to use even if one were wanted.** The
  U64-II bootloader reads a load address and magic value from `0xFFF8`, which is why
  `recovery/u64ii/recover.py` writes that pair. The U64 boot code takes no parameters.
  It always loads from the fixed flash address `0x290000`. The absence of a U64
  equivalent is a property of the boot code, not a gap in the tooling.

The U64-II recovery script and this deploy script solve different problems. For
JTAG on the C64 Ultimate and Ultimate 64 Elite II, see `c64u-jtag.md`.
`recover.py` is a cold-start path for a board that is not running its normal boot
chain: hold the CPU in reset, upload an image, plant the handoff values, release reset.
The U64 JTAG loop is a warm swap on a board that already booted.

## Do not reconfigure the FPGA as part of the deploy loop

A sequence like this looks reasonable and is not:

```bash
quartus_pgm -c "USB-Blaster [3-7]" -m jtag -o "p;external/u64.sof"
nios2-download -g target/u64/nios2/ultimate/result/ultimate.elf
```

Both steps can report success while leaving the board with no video and no REST
interface, and the design hash reading back as all ones. A power cycle restores
everything, and flash is never touched.

`quartus_pgm -o "p;..."` performs a volatile SRAM reconfiguration. It discards the
design the board booted with and replaces it for as long as power is maintained. That
is why a power cycle recovers the board: the next power-on reloads the flash-resident
design.

Note that DDR2 calibration is not what fails here. After reconfiguration the on-chip
boot code runs again and calibrates DDR2 again, which is consistent with the download
verifying successfully.

The more likely explanation is the bitstream. `external/u64.sof` in the upstream
repository is the standard U64 Cyclone V image. `quartus_pgm` reporting no errors only
means the device family on the chain matched and configuration completed; it does not
confirm the image matches a particular board variant. Loading it onto a variant it was
not built for is consistent with a device that is configured but not running a design
appropriate for the hardware around it.

Either way the remedy is the same: do not reconfigure the FPGA. Deploy against a board
that has been powered on normally.

## When reconfiguration is appropriate

There is one case for it. If the fabric itself has stalled and a plain ELF redeploy
cannot clear the condition, reconfiguring and then redeploying can recover the board
without a physical power cycle:

```bash
export QUARTUS_ROOTDIR=<intel-fpga-root>/quartus
quartus_pgm -c "USB-Blaster [1-5]" -m JTAG -o "p;external/u64.sof"
nios2-download -g target/u64/nios2/ultimate/result/ultimate.elf
```

Two caveats. The configuration is volatile, so a later power cycle returns the board to
the flash design and, with it, the older flash firmware; always redeploy a known ELF
before measuring anything. And this only helps for a stalled fabric. It is not part of
the normal edit, build, deploy loop.

## Running a tree other than the checkout

To compare two firmware trees on one board (for example upstream `master` and a pull
request on top of it), build each in its own git worktree and point the deploy script at
it:

```bash
git worktree add --detach ../wt-master upstream/master
git -C ../wt-master submodule update --init --recursive
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool -s --no-submodule-update \
    --build-tools-dir <intel-fpga-root> --repo-dir ../wt-master u64
ULTIMATE_REPO_DIR=../wt-master bash tooling/build_and_deploy_u64.sh
```

- `build-tool` and `tooling/` exist only in the checkout that carries them, so run them
  from there and pass the tree with `--repo-dir`.
- `--build-tools-dir` takes the Intel FPGA version directory, the one that contains
  `quartus/` and `nios2eds/` (for example `~/altera_lite/18.1`), not its parent.
- A single target needs `BUILD_TOOL_ALLOW_PARTIAL=1`. A software-only build of `u64`
  takes about a minute.
- The deploy script loads the ELF of `ULTIMATE_REPO_DIR` and does not build. The board
  reports the commit of that tree in `/v1/info`.
- The loaded image is volatile and stays until the next power cycle. When the
  comparison is done, load the tree that should keep running rather than leaving a
  pull request build on the board.
- Merge a pull request into a worktree by commit id: `FETCH_HEAD` is per worktree.

## The FTP deploy path depends on the USB slot

`build-tool -d u64` uploads to `/Usb1/firmware/u64/custom/` by default. The volume name
follows the USB slot that holds the stick: on the bench Ultimate 64 the stick is in the
second slot, the FTP root lists `Temp`, `Flash` and `USB2`, and the default directory does
not exist, so the upload fails with exit status 2. Check the volume names with
`curl ftp://<device>/ --list-only` and pass the directory explicitly:

```
./build-tool --deploy-only -d u64 --deploy-host my-u64 --deploy-path /USB2/firmware/u64/custom/
```

`./build` reads `DEPLOY_PATH` instead. The tool does not detect the volume.

## Recovering a wedged board during testing

Re-running the deploy script is the normal recovery, which is why the end-to-end suites
print it as a hint on failure:

```
Recover with: bash tooling/build_and_deploy_u64.sh (JTAG redeploy)
```

See `tests/e2e/io/printer/printer_test.py` and
`tests/e2e/filesystem/ftp_client_test.py` in the upstream repository.
