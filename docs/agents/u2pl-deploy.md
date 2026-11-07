# Deploying U2+L firmware through the C64U

The U2+L cartridge has no keyboard and no power switch of its own. It owns the upload, the REST file check, the menu overlay and the menu button. The C64 Ultimate it is plugged into owns every keystroke and the power cycle. A U2+L cannot flash itself unattended: its `machine:input` is compiled out (HTTP 501), and it cannot power-cycle itself.

Placeholders: `$U2_HOST` is the cartridge, `$C64U_HOST` is the C64 Ultimate that hosts it. Get both from the person running the bench. Never write them into tracked files.

The deploy runs through `./build-tool --u2-flash`; `./build-tool --u2-flash --help` lists its options. For a cartridge that does not boot, see `docs/u2pl-jtag.md` in the tools repository.

## 1. Build

`u2pl` is not a default target, so `./build` never builds it. With a cached `target/fpga/u2plus_ecp5/impl1/u2p_ecp5_impl1.bit`, build software only. This reuses the ECP5 bitstream and changes only the RISC-V application. That is what makes flashing safe: a rebuilt bitstream has bricked cartridges.

```bash
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool -s u2pl    # software only, cached FPGA
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool u2pl       # full target, rebuilds the FPGA (needs Diamond)
```

The build writes `update_<git-id>.u2l` in the checkout root. Record `sha256sum` of it so the deployed image can be tied to the build.

## 2. Check the USB volume name

Do not assume it. List the cartridge's FTP root first. A wrong `--dest-root` fails late, after the upload.

```bash
python3 -c "from ftplib import FTP; f=FTP('$U2_HOST'); f.login(); print(f.nlst('/'))"
```

## 3. Deploy

```bash
./build-tool --u2-flash --host "$U2_HOST" --c64-host "$C64U_HOST" \
  --dest-root /<volume>/Firmware/latest --branch <short-branch> \
  --artifact update_<git-id>.u2l --git-id <unique-id> \
  --confirm-flash --confirm-power-cycle
```

- `--branch` must be a short name. The U2 file browser truncates long directory names, and the helper refuses an ambiguous prefix.
- `--git-id` must be unique for every rebuild, including repeated dirty builds at one commit. Uploads never overwrite an existing path.
- Leave reformat, config reset and WiFi update at their default No answers unless the user asks otherwise. They erase firmware contents.
- Without the two `--confirm-*` flags, the helper validates navigation and backs out.

The helper uploads over FTP, checks the size through REST, reads the menu from the cartridge, and sends keys through the C64U's `machine:input`. Cartridge REST goes offline while it flashes, so the helper then reads the updater's text from C64 screen RAM (`$0400-$07E7`) through the C64U's `machine:readmem`. Before it power-cycles it must see FPGA flashing, application flashing, `Done!` and `PLEASE TURN OFF YOUR MACHINE`. Then it drives the C64U's F1, Power & Reset, Power Cycle.

## Two preconditions that look like a navigation bug

Both give the same symptom: every key is accepted with HTTP 200 and nothing happens.

1. **The C64 must be running.** The keys reach the cartridge through the C64U's keyboard matrix, which is scanned only while the C64 executes. If an earlier test left a monitor or freeze session up, no key arrives, although REST answers normally. The dependable check: read the menu screen, send one cursor key, read again, and see whether the selected row changed. A DMA read of `$C000` returning `$FF` does not indicate a freeze; it also reads `$FF` with the menu merely open.
2. **The C64U's own menu must be closed.** `software/api/route_input.cc` sends keys to the on-device UI's queue while that UI has the keyboard matrix disabled, and writes the C64 matrix only when it is enabled. The cartridge sees only the matrix. `machine:menu_screen` on the C64U returning 404 means its menu is closed, which is what you want. Close it with `machine:menu_button` and confirm the 404 first.

## 4. Verify both devices

```bash
python3 tests/lib/health.py -H "$U2_HOST"   -c ping -c rest -c ftp -c telnet -c ident -c dma
python3 tests/lib/health.py -H "$C64U_HOST" -c ping -c rest -c ftp -c telnet -c ident -c dma
```
