# Deploying U2+L firmware through the C64U

The U2+L cartridge has no keyboard and no power switch of its own. It owns the upload, the REST file check, the menu overlay and the menu button. The C64 Ultimate it is plugged into owns every keystroke and the power cycle. A U2+L cannot flash itself unattended: its `machine:input` is compiled out (HTTP 501), and it cannot power-cycle itself.

Placeholders: `$U2_HOST` is the cartridge, `$C64U_HOST` is the C64 Ultimate that hosts it. Get both from the person running the bench. Never write them into tracked files. The tool has no default host. Pass `--host` and `--c64-host`, or set `U2_FLASH_HOST` and `U2_FLASH_C64_HOST` in the environment or in `.build-tool.env` (see `.build-tool.env.example`). The REST passwords, if the devices have any, are `U2_FLASH_PASSWORD` and `U2_FLASH_C64_PASSWORD`.

The deploy runs through `./build-tool --u2-flash`, which calls `tooling/u2_flash.py`. `./build-tool --u2-flash --help` lists its options. For a cartridge that does not boot, see `docs/u2pl-jtag.md` in the tools repository.

The flash is persistent and the final step restarts the C64 Ultimate. Run it only when the task asks for it, and not while someone else uses either device. The tool holds the `c64u` device lock for the whole run (see `tooling/device_lock.py`) and refuses when another tool holds it. `--wait SECONDS` waits for the lock instead. A lock held by a parent such as `tooling/with_device_locks.py` covers the run. Never delete the lock file.

## 1. Build

`u2pl` is not a default target, so `./build` never builds it. With a cached `target/fpga/u2plus_ecp5/impl1/u2p_ecp5_impl1.bit`, build software only. This reuses the ECP5 bitstream and changes only the RISC-V application. That is what makes flashing safe: a rebuilt bitstream has bricked cartridges.

```bash
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool -s u2pl    # software only, cached FPGA
BUILD_TOOL_ALLOW_PARTIAL=1 ./build-tool u2pl       # full target, rebuilds the FPGA (needs Diamond)
```

The build writes `update_<git-id>.u2l` in the checkout root. Record `sha256sum` of it so the deployed image can be tied to the build.

A software-only build packages whichever ECP5 bitstream is checked in under `target/fpga/u2plus_ecp5/impl1/`. That copy can be older than the one CI uses, and flashing it can lower the cartridge's `fpga_version`. The `update_package_*` artifact of a CI run carries every product's image, including the `.u2l` with the current bitstream. Prefer it when you need a particular commit and have no reason to build locally.

## 2. Check the USB volume name

Do not assume it: the default `--dest-root` names `USB1`, and a cartridge may have its stick in another slot. List the cartridge's FTP root first. When the volume does not exist, the upload stops with the server's error and a list of the entries at the FTP root.

```bash
python3 -c "from ftplib import FTP; f=FTP('$U2_HOST'); f.login(); print(f.nlst('/'))"
```

## 3. Deploy

Check the artifact first. A dry run makes no network connection and needs no host:

```bash
./build-tool --u2-flash --dry-run --artifact update_<git-id>.u2l
```

Then flash. This command uploads, checks the upload, navigates to the file, runs the update, and restarts the C64 Ultimate once the update has finished:

```bash
./build-tool --u2-flash --host "$U2_HOST" --c64-host "$C64U_HOST" \
  --dest-root /<volume>/Firmware/latest --branch <short-branch> \
  --artifact update_<git-id>.u2l --git-id <unique-id> \
  --confirm-flash --confirm-power-cycle
```

- `--branch` must be a short name. The cartridge's file browser truncates long names. The tool matches each directory name exactly and stops with a safe exit code when it does not find one.
- `--git-id` must be new for every run that gets past `--dry-run`. The upload goes to `<dest-root>/<branch>/<date>/<git-id>/update.u2l` and never replaces an existing file. This includes a run without `--confirm-flash`, which uploads and then backs out, and `--upload-only`.
- Leave reformat, config reset and WiFi update at their default No answers unless the user asks otherwise. They erase firmware contents.
- Without `--confirm-flash` the tool validates the menu path up to the `Run Update` context menu and backs out with exit code 1. `--upload-only` stops after the REST check. Without `--confirm-power-cycle` the tool leaves the final power cycle to a person: the flash is complete, and the cartridge runs the new firmware after the next power cycle of the C64 Ultimate.
- If the path to the Power Cycle entry in the C64 Ultimate's menu differs from the one the tool knows, it presses nothing there and asks for a manual power cycle. The flash is complete at that point.
- After the power cycle the C64 Ultimate boots again. Allow up to two minutes before the checks below.

Exit codes: 0 success; 1 stopped safely (nothing destructive happened, a confirmation flag was missing, or the device lock is held); 2 unsafe or unknown state, which means a flash may be running, so look at the devices by hand and do not power-cycle anything; 3 usage error.

The tool uploads over FTP, checks the size through REST, reads the menu from the cartridge, and sends keys through the C64 Ultimate's `machine:input`. It finds the selected row by its foreground colour and reads the cursor after every key, so it never counts keypresses. Cartridge REST goes offline while it flashes, so the tool then reads the updater's text from C64 screen RAM (`$0400-$07E7`) through the C64 Ultimate's `machine:readmem`. Before it power-cycles it must see FPGA flashing, application flashing, `Done!` and `PLEASE TURN OFF YOUR MACHINE` on three consecutive reads. Danger words on that screen (`error`, `fail`, `abort`, `problem`, `retry`), a timeout, or missing progress markers end the run with exit code 2 and no power cycle.

## Two preconditions that look like a navigation bug

Both give the same symptom: every key is accepted with HTTP 200 and nothing happens.

1. **The C64 must be running.** The keys reach the cartridge through the C64U's keyboard matrix, which is scanned only while the C64 executes. If an earlier test left a monitor or freeze session up, no key arrives, although REST answers normally. The dependable check: read the menu screen, send one cursor key, read again, and see whether the selected row changed. A DMA read of `$C000` returning `$FF` does not indicate a freeze; it also reads `$FF` with the menu merely open.
2. **The C64U's own menu must be closed.** `software/api/route_input.cc` sends keys to the on-device UI's queue while that UI has the keyboard matrix disabled, and writes the C64 matrix only when it is enabled. The cartridge sees only the matrix. `machine:menu_screen` on the C64U returning 404 means its menu is closed, which is what you want. Close it with `machine:menu_button` and confirm the 404 first.

## 4. Verify both devices

`/v1/info` of the cartridge shows the new `git_commit_hash`. Do not read `fpga_version` alone as a health signal. To see that the C64 runs, read the jiffy clock twice, a second apart, from the C64 Ultimate: `GET /v1/machine:readmem?address=00A2&length=1`. The value must change.

```bash
python3 tests/lib/health.py -H "$U2_HOST"   -c ping -c rest -c ftp -c telnet -c ident -c dma
python3 tests/lib/health.py -H "$C64U_HOST" -c ping -c rest -c ftp -c telnet -c ident -c dma
```
