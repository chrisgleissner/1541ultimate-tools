# JTAG on the C64 Ultimate and Ultimate 64 Elite II

`tooling/u64ii_jtag.sh` loads an FPGA image and an application into a C64 Ultimate or
an Ultimate 64 Elite II over JTAG, through an FT232H or a USB-Blaster. It also reads the application's
console output and its memory. Everything it does is volatile. It never writes the
SPI flash, and a power cycle returns the board to its flashed FPGA image and flashed
application.

## How these boards differ from the Ultimate 64

The Ultimate 64 JTAG loop in `docs/u64-jtag-deploy.md` does not carry over. The two
board families share firmware sources but not the parts that JTAG talks to.

| | Ultimate 64 (Elite) | C64 Ultimate, Ultimate 64 Elite II |
|---|---|---|
| FPGA | Intel Cyclone V | Xilinx Artix-7, XC7A50T or XC7A100T |
| Application CPU | Nios II | RISC-V soft core |
| Build target | `u64`, `update.u64` | `u64ii`, `update.ue2` (alias `c64u`) |
| JTAG cable | USB-Blaster | FT232H, e.g. Adafruit FT232H, or a USB-Blaster |
| Host software | Quartus, `nios2-download`, `nios2-terminal` | Python with pyftdi |
| Application load | Nios debug module writes the ELF | FPGA user JTAG chain writes `ultimate.bin` to RAM |
| Application start | `nios2-download -g` | Bootloader in the FPGA image, via a boot request in RAM |
| Console | None (the image has no JTAG UART) | Every byte the CPU writes to its UART, from a FIFO in the FPGA |

The board-side JTAG logic is the user chain in
`fpga/io/jtag/vhdl_source/jtag_client_xilinx.vhd`, behind the Artix-7 `USER4`
instruction. It provides an identification word (`0xDEAD1541`), an output register
whose bit 7 holds the CPU in reset, memory reads and writes, and the console FIFO.
The ESP32-S3 on the board has the same four JTAG signals wired to it. Its firmware
switches them to inputs at start-up, so it does not compete with an external cable.

The bootloader (`software/portable/riscv/bootloader_u64ii.c`) jumps to the address at
`0xFFF8` when `0xFFFC` holds `0x1571BABE`. Loading an application is therefore: hold
the CPU in reset, write the image to `0x30000`, write that pair, and release reset.
`recovery/u64ii/recover.py` in the firmware repository uses the same sequence after it
has configured the FPGA.

## Wiring

Use the JTAG header P5. Five wires; do not connect 3.3 V, because the Adafruit board
has no target voltage sense.

| FT232H | Signal | P5 pin |
|---|---|---|
| D0 | TCK | 1 |
| D1 | TDI | 9 |
| D2 | TDO | 3 |
| D3 | TMS | 5 |
| GND | GND | 2 or 10 |

- The Adafruit board's **I2C mode switch must be off**. When it is on, it joins D1 and
  D2, and TDO then repeats TDI.
- **Power the FT232H over USB before switching the machine on**, and never leave it
  on P5 unpowered while the machine runs. An unpowered FT232H clamps TCK, TMS, TDI and
  TDO towards ground through its protection diodes, so the lines float at an undefined
  level while the FPGA configures and the CPU boots. One boot in that state showed a
  rolling HDMI picture; the same machine booted normally with the adapter removed. A
  powered FT232H that no tool has opened holds TCK at a steady level and leaves TMS and
  TDI to the FPGA's pull-ups, so the TAP stays in reset.
- Order: supply unplugged, fit the socket on P5, plug in the FT232H's USB, plug in the
  supply and switch the machine on. Reverse it to disconnect.
- Parts of the board stay powered while the power supply is plugged in, even when the
  machine is switched off. Fit and remove the socket with the supply disconnected.
- The tool drives TCK, TDI and TMS only while a command runs, and leaves all four pins
  as inputs when it exits.

### USB-Blaster

An Altera USB-Blaster or one of its clones (USB `09fb:6001`) works as well. Its 10-pin
plug has the layout of P5 (TCK 1, GND 2, TDO 3, VCC sense 4, TMS 5, TDI 9, GND 10) and
goes straight on, no wires. Select it with `--url blaster` or
`U64II_JTAG_URL=blaster`; every command, the gdb server and the monitor take it.

`blaster` opens a USB-Blaster only when it is the only one attached. With a second
one on the same host, for example on an Ultimate 64, it stops and lists what it
found, because it could otherwise open the other machine's cable. Name the one on
this machine by its serial number:

```bash
U64II_JTAG_URL=blaster:8aB75VK4 tooling/u64ii_jtag.sh probe
```

Clones whose serial numbers clash are named by bus and address instead, in hex as
pyftdi reads them, for example `blaster:1:1a`; the list shows that form for them.
A Blaster held by another program, such as Quartus' `jtagd`, cannot be opened, and
the error says so.

- TCK is set by the cable; `--frequency` does not apply.
- When a command ends the tool switches the cable's outputs off (bit 5). That turns
  the drivers off on an original USB-Blaster; some clones use the bit only for the
  LED and keep driving TCK, TMS and TDI. To be sure, unplug the USB.
- Unplug the Blaster's USB before switching the machine on or off. With the cable
  active at power-on a C64 Ultimate has been seen to stay dark, its FPGA not loaded
  from flash; an idle, plugged-in Blaster has also booted normally, so this is a
  caution, not a rule that always bites.
- On Linux the udev rule is the same as below with `idVendor` `09fb` and `idProduct`
  `6001`.

On Linux, give your user access to the FT232H once:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", MODE="0666", TAG+="uaccess"' \
    | sudo tee /etc/udev/rules.d/99-ft232h.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

The first run of `tooling/u64ii_jtag.sh` creates a virtual environment with pyftdi under
`~/.cache/1541ultimate-tools/jtag-venv`.

## First connection

`probe` changes nothing on the board. Run it first, and again whenever the wiring has
been touched:

```bash
tooling/u64ii_jtag.sh probe
```

On a board that is running normally the output should look like this:

```
[u64ii-jtag] IDCODE 0x1362C093: XC7A50T, revision 1
[u64ii-jtag] IR capture 0x35: configuration done
[u64ii-jtag] user chain answers 0xDEAD1541: an Ultimate design is running
[u64ii-jtag] user chain debug word 0x........
[u64ii-jtag] bitstream for this part: external/u64e2_50t.bit
```

`probe` confirms three separate things. The IDCODE shows that the cable, power and
TDO wiring work. `DEAD1541` shows that the user chain framing is correct, including the
select bit that precedes every user register scan. The part name tells which bitstream
`--fpga auto` will use. The revision nibble and the exact IR capture value can differ
from the example.

The tool checks the IDCODE before it shifts any instruction. It refuses a Lattice ECP5
(an Ultimate II+L), which matters when the same FT232H is also used to recover that
cartridge: an instruction sized for the Artix could clear an ECP5's configuration.

## Commands

| Command | Effect | Changes the board |
|---|---|---|
| `probe` | IDCODE, configuration state, user chain ID | No |
| `console [--secs N]` | The application's UART output (FIFO of 1024 bytes) | No |
| `dump ADDR LEN [-o FILE]` | Read memory | No |
| `run [--bin FILE] [--fpga BIT \| --warm] [--no-verify] [--console N]` | Configure the FPGA, load an application into RAM and start it | FPGA and RAM, until power cycle |
| `fpga [--bit BIT]` | Configure the FPGA; the flashed application then boots | FPGA, until power cycle |
| `reset` | Restart the CPU through the cache flush, so the bootloader starts the flashed application | CPU restart |
| `recover` | Same as `recovery/u64ii/recover.py`: the recovery kit's bitstream and application (XC7A50T only) | FPGA and RAM, until power cycle |

`run` reads the whole image back before it releases the CPU, and writes it again once
if a block does not match. `--no-verify` skips the read-back and roughly halves the
load time.

`run` configures the FPGA before it loads the application, so every hardware block
starts from reset. The flashed application then runs for about a second before the
tool holds the CPU, so blocks it has started, such as the network receive DMA, are
running again when the new application starts, until it initialises them. `--warm`
skips the reload and only restarts the CPU, so everything the previous application
set up keeps running. Both have booted the applications tested so far. The reload is
the default because it starts from the cleaner state; `--warm` saves about 7 s.

Holding the CPU in reset stops the running application wherever it is. If it was
writing its configuration or a file to flash at that moment, that write is cut off,
just as a power cut would cut it off. The tool itself never writes flash.

### The instruction cache, and why the boot request points at a trampoline

The CPU's instruction cache (`fpga/cpu_unit/rvlite/vhdl_source/icache.vhd`) is 2 KB,
direct-mapped, one word per entry, over the lowest 32 MB. Its reset clears the state
machine but not the tags. After a CPU-only reset it therefore still returns the
previous application's instructions for every address that application executed. A
new image at the same addresses runs partly stale code and stops before its first
line of output. The first hardware runs failed exactly like this, including the run
after an FPGA reload, because the flashed application has already run for a second
by the time the tool holds the CPU in reset.

`run` and `reset` therefore point the boot request at a small program at `0x8000`: a
cache-sized run of NOPs followed by a jump. Executing it replaces every cache entry
with addresses no application uses. `run` then jumps to `0x30000`; `reset` jumps back
into the bootloader at `0x80000000`, which has cleared the request by then and loads
the flashed application from flash. `recovery/u64ii/recover.py` does not do this and
jumps straight to `0x30000`.

Measured at 3 MHz with read-back: FPGA configuration 6.7 s, application load 11-12 s.
At `--frequency 6e6 --no-verify`: 3.8 s and 1.9 s.

Every command takes the device lock `/tmp/1541ultimate-device-locks/c64u.lock` (set
`DEVICE_LOCK_DIR` to move it, `U64II_JTAG_LOCK=off` to disable it), waiting up to two
minutes for another user of the adapter. A lock held by a parent process, such as a
`with-device-locks c64u -- ...` wrapper, is honoured without deadlocking.

`BIT` is `auto` or a path. `auto` picks `external/u64e2_50t.bit` or
`external/u64e2_100t.bit` from the IDCODE; these are the images that `update.ue2`
installs.

Set `U64II_JTAG_URL` or pass `--url` when more than one FTDI device is attached, and
lower `--frequency` (default 3 MHz) for long or noisy wires.

## Build and run loop

```bash
./build-tool --jtag c64u                    # build ultimate.bin (~15 s), FPGA + app from RAM
./build-tool --jtag-monitor c64u --jtag-monitor-secs 10
```

`--jtag c64u` builds only the application (`target/u64ii/riscv/ultimate`), without the
ESP32 firmware or the update package, and then runs
`tooling/build_and_deploy_u64ii.sh`. That script, like its U64 counterpart, does not
build and takes no arguments. It loads
`target/u64ii/riscv/ultimate/result/ultimate.bin` and can be rerun on its own. When
`U64II_VERIFY_HOST` is set, build-tool then checks the machine over REST.

`build-tool` deploys over JTAG only a target it built successfully in the same run,
unless `--deploy-only` is given. The deploy scripts load whatever image is on disk, so
running them after a failed or skipped build would start a stale image.

`--jtag-fpga BIT` picks the bitstream (default `auto`). `--jtag-fpga warm` requests
the application-only swap described above.

The whole loop, `./build-tool --jtag c64u` with `U64II_VERIFY_HOST` set, takes about
33 s including the REST check (READY prompt, raster, jiffy clock).

### A full `update.ue2`

```bash
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool u64ii            # this repository
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool --repo-dir <tree> c64u
```

The second form builds another firmware tree with the same layout, for example a C64
Ultimate firmware tree. build-tool builds the ESP32-S3 firmware first when that tree's
`u64ii` make goal does not already depend on it.

The FPGA bitstreams are not built. They are checked in under `external/`, and building
them needs AMD Vivado and HDL sources that are not in the firmware repository.

## Running a pull request

`tooling/apply_pr.sh` makes a throwaway git worktree with one or more 1541ultimate pull
requests applied as uncommitted changes. `build-tool --apply-pr N` drives it and then
builds that worktree:

```bash
./build-tool --apply-pr 705 --pr-base upstream/master --jtag c64u
```

Each PR goes in as its net change: the diff from its merge base with the upstream
`master` to its head. Merging the PR branch instead would also bring in every upstream
commit the target tree lacks, which matters for the second use below. The diff is
applied with a three-way merge; files the PR changes but the tree does not have are
skipped and listed. When conflicts remain, the script exits 3, and the worktree
(`<repo>-pr705` beside the repository) keeps its markers. Resolve them there and rerun the same
`build-tool` command. It continues in that worktree, skips the PRs already applied, and
applies the ones after the conflict. Nothing is committed or pushed;
`git worktree remove <path>` undoes it.

The same works for another firmware tree with this layout, such as a C64 Ultimate
firmware tree, as long as it has a remote pointing at GideonZ/1541ultimate:

```bash
./build-tool --repo-dir ../other-tree --apply-pr 705 --pr-base <its release branch> --jtag c64u
```

When resolving conflicts in such a tree, take only what the PR itself changes
(`git diff <merge base> <PR head> -- <file>`). Keep the tree's own code, and do not pull
in unrelated upstream features that happen to be in the PR's context lines.

A build from such a worktree reports the commit it started from in `/v1/info`,
because the firmware takes its version hash from `git rev-parse HEAD`. Identify it by
the feature under test or by its console output, not by the hash.

### Ultimate firmware on a C64 Ultimate: system ROMs

The Ultimate firmware loads the C64 KERNAL, BASIC and character ROMs only from
`/flash/roms`, under the names in *C64 and Cartridge Settings → Kernal ROM / Basic ROM
/ Char ROM*. A C64 Ultimate ships with those names empty, because its own firmware
boots without them. The Ultimate build then shows "Welcome to the Ultimate-64! ...
without System ROMs" instead of BASIC. For it to boot to BASIC:

1. Put the stock ROM files in the machine's `/Flash/roms` once, for example over FTP.
   The C64 Ultimate firmware ignores them while its names are empty.
2. After each JTAG boot of the Ultimate build, set the three names over REST without
   saving to flash, then reboot the C64 so the ROMs load:

```bash
CAT="C64%20and%20Cartridge%20Settings"
curl -X PUT "http://<host>/v1/configs/$CAT/Kernal%20ROM?value=kernal.901227-03.bin"
curl -X PUT "http://<host>/v1/configs/$CAT/Basic%20ROM?value=basic.901226-01.bin"
curl -X PUT "http://<host>/v1/configs/$CAT/Char%20ROM?value=characters.901225-01.bin"
curl -X PUT "http://<host>/v1/machine:reboot"
```

The stored configuration stays unchanged, so the flashed firmware finds it as it was.

## Watching the machine

`tooling/c64u_monitor.py` combines three sources into one event log, so problems show
up as they happen:

```bash
~/.cache/1541ultimate-tools/jtag-venv/bin/python tooling/c64u_monitor.py <host> --out mon &
tail -F mon/events.log | grep -E ' (ALERT|WARN) '
python3 tooling/c64u_monitor.py --stop --out mon
```

- **video**: the VIC stream, frame rate, packet loss, an all-black picture; the latest
  frame is saved as `mon/latest.png`. The stream goes to its own multicast group,
  `239.0.1.65:11064`. Unicast does not start on a machine with both Ethernet and WiFi
  up: the firmware looks up the destination MAC by ARP, never sees the answer, and
  replies `Network Host Resolve Error`.
- **rest**: `/v1/info` every five seconds; unreachable, version changes, errors.
- **console**: the CPU's UART output over JTAG every two seconds, with lines such as
  `Hello world` (a reboot), `assert` or `exception` raised as alerts. It takes the
  device lock without waiting and skips a poll when a deploy holds it.

The console FIFO keeps what the CPU printed before anyone read it, including the whole
boot log, and drops new bytes once it holds 1024. The monitor treats that backlog as
history and raises alerts only for text printed after it started.

Runs of `~` in the console are not lost data. `software/io/wifi/wifi_cmd.cc` writes a
`~` for every message the ESP32 sends unasked, which is WiFi receive traffic, and an
`N` for every reply. The monitor counts the tildes per minute and removes them from
the text. `ERROR reading from socket` and `ERROR writing to socket` follow a client
closing its connection first; the monitor logs them without alerting.

Stop the monitor with `--stop`, which signals the pid in `mon/monitor.pid`. A
`pkill -f` pattern also matches the shell that runs it.

## Debugging with gdb

The CPU has no debug module: nothing over JTAG can halt it, step it or read its
live registers, and the only CPU control is holding it in reset. What JTAG does
offer is enough for most post-mortem and hang analysis, and
`tooling/u64ii_gdb.sh` turns it into a gdb session without changing the firmware:

```bash
tooling/u64ii_gdb.sh                       # ELF of this checkout's u64ii build
tooling/u64ii_gdb.sh path/to/ultimate.elf  # the ELF of whatever is running
```

`u64ii_gdbstub.py` is a gdb remote server on the host. It reads memory through the
FPGA's JTAG memory port while the firmware keeps running; the CPU has no data
cache, so it reads what the CPU wrote. It presents every FreeRTOS task as a gdb
thread. A task's registers are the ones the FreeRTOS RISC-V port saved when the
task last trapped: a 30-word frame on its stack (`port_asm.S`), whose address is
the first word of the task's control block. So:

- `info threads` lists every task with its state and priority;
- `thread N` then `bt` shows where a blocked task waits, with arguments;
- after a GURU MEDITATION, the crashed task's frame shows where it faulted;
- `p variable`, `x/16x address` read live memory.

Limits: the running task's registers are those of its last trap; `continue`,
`step` and register writes are refused; memory writes need `U64II_GDB_WRITES=1`;
there are no watchpoints. Reads above `0x10000000` (the I/O space) are refused,
because reading some registers has side effects.

gdb's own unwinders stop after the first frame of this firmware: the compiler's
call frame information marks the return address undefined in shrink-wrapped
functions, and the RISC-V prologue analyser gives up at the first branch.
`u64ii_gdb_unwind.py` replaces them with a scan of the whole function for its frame
allocation and its `ra`/`s0` saves. It needs a RISC-V gdb built with Python, such
as the xPack `riscv-none-embed-gdb-py3` or `gdb-multiarch`; the wrapper finds one
and warns when it can only find a gdb without Python.

The server takes the device lock for its whole session. Stop it (quit gdb) before
running other JTAG commands.

## Recovery

- **Anything the tool did:** `tooling/u64ii_jtag.sh fpga` (about 7 s) reconfigures the
  FPGA, after which the bootloader starts the flashed application. A power cycle does
  the same. Flash was never written.
- **Back to the flashed application without a power cycle:** `tooling/u64ii_jtag.sh reset`.
- **A board that does not boot:** `tooling/u64ii_jtag.sh recover`. It configures the
  kit's FPGA image, loads the kit's Ultimate 3.14c application into RAM and starts it,
  which takes about 20 s. From its menu, open the file browser, pick an `update.ue2` on
  the USB stick (mounted as `/USB2` in 3.14c) and run the update to make the fix
  permanent. `run` with a known-good `ultimate.bin` is the alternative when the
  firmware you want is not on the stick.
- On a C64 Ultimate the recovery application shows "Welcome to the Ultimate-64! ...
  shipped without System ROMs" instead of BASIC. 3.14c is Ultimate firmware and does
  not find its C64 ROMs where it expects them. The screen is only text; open the menu
  and install `update.ue2` as usual, and the C64 Ultimate firmware brings its ROMs back.
- While `recover` configures the FPGA, the flashed application starts for about a
  second on the kit's older FPGA image and may print a `GURU MEDITATION`. The tool
  replaces it right after, so ignore that crash.
- To leave the recovery application without flashing, use `fpga`, not `reset`.
  `reset` keeps the kit's FPGA image, which the flashed application does not run on.

### Recovery flow, step by step

This follows `recovery/u64ii/README.md` in the firmware repository (final steps: open the
file browser, select `update.ue2` on the USB stick, run the update) and is the sequence
verified on a C64 Ultimate.

1. `tooling/u64ii_jtag.sh probe` shows IDCODE `x362C093` and user ID `0xDEAD1541`.
2. `tooling/u64ii_jtag.sh recover` (about 18 s). The recovery application reports
   `Ultimate 64-II`, firmware `3.14c`, FPGA `121`, and serves REST and Telnet.
3. The recovery application has no `machine:input` and no `machine:menu_screen`, so the
   menu is driven through Telnet with `tooling/u64ii_menu.py`, which keeps a screen model
   and prints the screen after every key:

   ```bash
   M="tooling/u64ii_menu.py <host>"
   $M start                                  # file browser root: SD, Flash, Temp, USB2, ...
   $M key down down down enter enter         # a drive shows an Enter popup; Enter again opens it
   $M key down ... enter                     # to the directory holding the update
   $M key down ... enter                     # context menu of the update.ue2 file
   $M wait "Run Update" 5                    # read the menu before choosing
   $M key enter                              # Run Update
   ```

4. Run Update hands the machine to the updater. The updater draws on the C64 screen
   and stops all network services, so REST and Telnet no longer answer, and the JTAG
   console stays silent. Its prompts need the keyboard of the machine:
   - Reformat Flash Disk: answer No. Yes erases the Flash disk (configuration, ROMs).
   - Reset configuration and the WiFi/ESP32 update: leave the default No.
   - Wait for `Done!` and `PLEASE TURN OFF YOUR MACHINE`, then remove power at the
     supply. The soft power button is not enough.
5. After the power cycle the board runs the flashed FPGA image and application. Compare
   `GET /v1/info` (`firmware_version`, `git_commit_hash`, `fpga_version`) with the update
   that was installed, and compare `GET /v1/configs` item by item with a snapshot taken
   before step 2.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `IDCODE reads 0x00000000` or `0xFFFFFFFF` | Machine off, GND or TDO not connected, or the I2C switch is on |
| `bypass delay of 0` or another value than 1 | TDI and TDO bridged, or a marginal signal; check the wiring and lower `--frequency` |
| `IDCODE ... is Lattice LFE5U-25` | The cable is on an Ultimate II+L, not this board |
| User chain ID is not `0xDEAD1541` on a running board | Scan framing does not match the FPGA image; report it with the `probe` output |
| `verify failed` during `run` | Signal integrity; lower `--frequency` |
| `memory read ... stalled` | The memory bus did not answer; try `run --fpga auto` |
| `USB device busy` / cannot open the FT232H | Another program holds the adapter, for example ecpprog |

## Status

`tooling/test_u64ii_jtag.py` runs the tool against a simulated FT232H, and the same
tests again against a simulated USB-Blaster, with a Python model of the user chain written from `jtag_client_xilinx.vhd`.

On a C64 Ultimate (XC7A50T) every command has run: `probe`, `console`, `dump`,
`fpga`, `run` with and without an FPGA reload, `run --warm`, `reset` back to the
flashed firmware, `recover`, and the build-tool loop. Builds of
both this repository's `u64ii` target and a C64 Ultimate firmware tree booted and
passed the REST check. `build-tool --apply-pr` built GideonZ/1541ultimate#705 on the upstream
`master`, and `apply_pr.sh` applied it to a C64 Ultimate firmware tree, where it needed the
conflict resolution described above. The device configuration was compared item by item before and
after, and nothing changed. The recovery kit's application (Ultimate 3.14c) booted,
found the USB stick, and left the configuration untouched; `fpga` then returned the
machine to its flashed firmware.
