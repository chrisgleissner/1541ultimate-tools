#!/usr/bin/env python3
"""Switch a C64 Ultimate (or Ultimate 64 Elite II) between firmware setups with one command.

    c64u-mode status                 what runs on the board now, and what the flash holds
    c64u-mode list                   the modes this installation knows
    c64u-mode <mode>                 switch to a mode
    c64u-mode ensure <mode>          switch only when the board is not in that mode already; exit 0 when it is
    c64u-mode snapshot <name>        save the whole flash as the latest image of <name>
    c64u-mode check-power            record that 'Power On After Power Loss' is On (needs the Ultimate REST)
    c64u-mode install-desktop        KDE/freedesktop launchers (and global hotkeys) for the modes

Options: --dry-run (change nothing), --wait S (wait up to S seconds for the device lock instead of refusing),
--allow-power-cycle (this command may power-cycle the board), --force (see 'Flash images').

Built-in modes:

    flash:<name>   Make the flash hold image <name>. The flash is read first (twice, compared) and saved under the
                   name whose code (the first 4 MiB: bitstream and application) it carries, or as
                   unknown-<time>; then the latest image of <name> is written over JTAG and read back.
                   The way to keep two complete firmware setups (for example the vendor firmware and an
                   alternative firmware) and swap between them.
    jtag           Run any bitstream and application from RAM (u64ii_jtag.sh run; volatile):
                   c64u-mode jtag -- --fpga x.bit --bin app.bin
    reload         Reconfigure the FPGA from flash (what a power-on starts), without touching the flash.

Modes from plugins: every *.py in tooling/c64u_modes/ and in the directories of $C64U_MODE_PLUGINS
(colon-separated) that defines register(registry). See tooling/c64u_modes/README.md.

Configuration: ~/.config/c64u-mode/config.ini

    [board]
    host = 192.168.1.64          ; REST/FTP address of the board
    outlet = C64U                ; exact alias of the smart outlet that powers ONLY this board (powerctl)
    outlet_id = 8022AC...        ; optional: its powerctl device id; a different device under that alias is refused
    deny_outlets = Desk, Router  ; aliases never switched (added to a built-in floor that cannot be removed)
    [modes]
    stock = flash:stock          ; short names for launchers and hotkeys
    [hotkeys]
    stock = Meta+Alt+1

Device lock: every command that touches the board holds the c64u lock ($DEVICE_LOCK_DIR/c64u.lock, the flock
u64ii_jtag.py and tooling/with_device_locks.py use; the older C64U_JTAG_LOCK names the file when DEVICE_LOCK_DIR is
not set) for its whole run and writes 'pid=<pid> ...' into it. Held by someone else: the command refuses (with a
desktop notification), or waits with --wait. Held by an ancestor process, for example under
'with_device_locks.py c64u -- c64u-mode ...': it runs.

Power cycles: only with --allow-power-cycle (desktop launchers pass it: pressing one is the consent). The outlet
must be listed by powerctl under exactly the configured alias (and id), and must not be denied. With the Ultimate
REST up: 'Power On After Power Loss' must read On, then a REST power-off, then the cut once the board is silent.
Without it: the setting must have been seen On within 30 days (check-power), else the cut is refused.

Flash images live in ~/.local/share/c64u-mode/flash/<name>/, each with a .sha256 of the whole image and a .code
hash of its first 4 MiB. state.json records the image the flash holds; it is a hint: swaps identify the flash by
reading it. A swap that did not finish leaves state 'writing' and blocks the next swap until --force.
"""
from __future__ import annotations

import argparse
import configparser
import datetime as _dt
import glob
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import device_lock

HERE = os.path.dirname(os.path.abspath(__file__))
# plugins import ModeError from "c64u_mode"; when this file runs as a script that must be this module, not a copy
sys.modules.setdefault("c64u_mode", sys.modules[__name__])
CONFIG = os.path.expanduser(os.environ.get("C64U_MODE_CONFIG", "~/.config/c64u-mode/config.ini"))
DATA = os.path.expanduser(os.environ.get("C64U_MODE_DATA", "~/.local/share/c64u-mode"))
# DEVICE_LOCK_DIR is the setting every tool honours; C64U_JTAG_LOCK, the full path of the lock file, is the older
# name of this tool and applies only when DEVICE_LOCK_DIR is not set.
LOCK = (device_lock.lock_path("c64u") if os.environ.get(device_lock.LOCK_DIR_ENV)
        else os.environ.get("C64U_JTAG_LOCK") or device_lock.lock_path("c64u"))
FLASH_SIZE = 16 * 1024 * 1024
CODE_SIZE = 4 * 1024 * 1024        # bitstream slot and application slot; the flash drive and the config follow
FPGA_PART = "xc7a50tfgg484"
DENY_FLOOR = {"mickey", "c64"}     # outlets that power other machines; never switched (user rule 2026-10-08)
POWER_SETTING_MAX_AGE = 30 * 86400


class ModeError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[c64u-mode] {msg}", flush=True)


# ---- the device lock -----------------------------------------------------------------------------------------------

class DeviceLock:
    """The c64u lock for a whole command. wait 0: refuse at once when someone else holds it.

    A lock held by an ancestor process (with_device_locks.py, flock(1)) covers the command."""

    def __init__(self, what: str, wait: float = 0.0, path: str | None = None):
        self.what, self.wait, self.path = what, wait, path or LOCK
        self.handle = None

    def __enter__(self) -> "DeviceLock":
        if os.environ.get("C64U_MODE_LOCK") == "off":
            return self
        try:
            self.handle = device_lock.acquire(self.path, self.wait, "c64u", f"c64u-mode {self.what}")
        except device_lock.Busy as busy:
            raise ModeError(f"the C64U is in use ({busy.holder or 'lock held'}); nothing was changed. "
                            "Try again later, or pass --wait SECONDS")
        return self

    def __exit__(self, *exc) -> None:
        device_lock.release(self.handle)
        self.handle = None


# ---- configuration ------------------------------------------------------------------------------------------------

def load_config(path: str = CONFIG) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.optionxform = str
    cfg.read(path)
    for section in ("board", "modes", "hotkeys"):
        if not cfg.has_section(section):
            cfg.add_section(section)
    env_host = os.environ.get("C64U_HOST")
    if env_host:
        cfg["board"]["host"] = env_host
    return cfg


# ---- the board ----------------------------------------------------------------------------------------------------

class Board:
    """Everything a mode does to the board goes through here, so tests and --dry-run can replace it."""

    def __init__(self, cfg: configparser.ConfigParser, dry_run: bool = False, run=subprocess.run,
                 allow_power_cycle: bool = False, state=None):
        self.cfg = cfg
        self.host = cfg["board"].get("host", "")
        self.dry_run = dry_run
        self.allow_power_cycle = allow_power_cycle
        self.state = state            # the FlashStore whose state.json records the power setting
        self._run = run

    # REST -----------------------------------------------------------------------------------------------------------
    def http(self, method: str, path: str, body: bytes | None = None, timeout: float = 4.0):
        """(status, parsed JSON or text) or (None, error text) when the board does not answer."""
        if not self.host:
            raise ModeError("no board address: set [board] host in " + CONFIG + " or C64U_HOST")
        req = urllib.request.Request(f"http://{self.host}{path}", data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            return None, str(e)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def rest_info(self):
        """The /v1/info of an Ultimate firmware, or None."""
        status, data = self.http("GET", "/v1/info")
        return data if status == 200 and isinstance(data, dict) and "firmware_version" in data else None

    # commands -------------------------------------------------------------------------------------------------------
    def run(self, argv: list[str], check: bool = True, capture: bool = False, always: bool = False):
        """Run a command (the caller holds the device lock). always: also in --dry-run (read-only commands)."""
        log(("would run: " if self.dry_run and not always else "run: ") + shlex.join(argv))
        if self.dry_run and not always:
            return subprocess.CompletedProcess(argv, 0, "", "")
        r = self._run(argv, text=True, capture_output=capture)
        if check and r.returncode != 0:
            raise ModeError(f"{argv[0]} failed with exit code {r.returncode}")
        return r

    def ofl(self, *args: str, check: bool = True):
        return self.run(["openFPGALoader", "-c", "ft232", "--fpga-part", FPGA_PART, *args], check=check)

    def reload(self):
        self.ofl("-r")

    def flash_read(self, dest: str):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        self.ofl("--dump-flash", "--file-size", str(FLASH_SIZE), "--offset", "0", dest)
        if not self.dry_run and os.path.getsize(dest) != FLASH_SIZE:
            raise ModeError(f"{dest}: read {os.path.getsize(dest)} bytes, not {FLASH_SIZE}")

    def flash_write(self, image: str):
        self.ofl("-f", "--file-type", "bin", "--offset", "0", image)

    def jtag_run(self, args: list[str]):
        self.run([os.path.join(HERE, "u64ii_jtag.sh"), "run", *args])

    def wait_rest(self, timeout: float = 180.0) -> dict | None:
        if self.dry_run:
            return None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            info = self.rest_info()
            if info:
                return info
            time.sleep(2)
        return None

    # power ----------------------------------------------------------------------------------------------------------
    def power_cycle(self, wait_port: int = 80):
        """One power cycle through the configured outlet, with every check of the user's power rules first."""
        if not self.allow_power_cycle:
            raise ModeError("this needs a power cycle of the board; pass --allow-power-cycle to allow it")
        outlet = self.check_outlet()
        status, data = self.http("GET", "/v1/configs/U64%20Specific%20Settings/Power%20On%20After%20Power%20Loss")
        cur = _find_current(data) if status == 200 else None
        if status == 200 and cur is None:
            # another firmware can answer this URL with 200 without carrying the setting
            log("the firmware that answers does not report 'Power On After Power Loss'")
        if cur is not None:
            if cur != "On":
                raise ModeError(f"'Power On After Power Loss' is {cur!r}, not 'On': the board would stay off after "
                                "the cycle. Set it to On first (C64U menu, U64 Specific Settings).")
            if self.state is not None and not self.dry_run:
                self.state.update(power_on_seen=time.time())
            log("'Power On After Power Loss' is On; REST power-off first")
            self.http("PUT", "/v1/machine:poweroff", body=b"")
            if not self.dry_run and not self.wait_silent(60):
                raise ModeError("the board still answers 60 s after the REST power-off; not cutting the power")
        else:
            seen = self.state.get("power_on_seen") if self.state is not None else None
            if not seen or time.time() - float(seen) > POWER_SETTING_MAX_AGE:
                raise ModeError("the board runs no Ultimate firmware now, so 'Power On After Power Loss' cannot be "
                                "read, and it was not seen On within 30 days. Start the Ultimate firmware and run "
                                "'c64u-mode check-power' once, or power the board off and on yourself")
            log("no Ultimate REST now; 'Power On After Power Loss' was seen On "
                f"{int((time.time() - float(seen)) / 86400)} days ago: hard power cycle")
        self.run(["powerctl", "cycle", outlet, "--yes", "--json", "--off-seconds", "8",
                  "--wait-host", self.host, "--wait-port", str(wait_port), "--wait-timeout", "180"])

    def check_outlet(self) -> str:
        """The configured outlet, after checking it against the deny floor and powerctl's registry."""
        board = self.cfg["board"]
        outlet = board.get("outlet", "").strip()
        if not outlet:
            raise ModeError("no outlet configured ([board] outlet): power the board off and on yourself")
        deny = DENY_FLOOR | {d.strip().lower() for d in board.get("deny_outlets", "").split(",") if d.strip()}
        low = outlet.lower()
        # equal to a denied alias, or a prefix of one (powerctl resolves a unique alias prefix)
        if any(d == low or d.startswith(low) for d in deny):
            raise ModeError(f"outlet {outlet!r} is, or abbreviates, an outlet on the deny list; refusing to switch it")
        r = self.run(["powerctl", "list", "--json"], capture=True, always=True)
        try:
            devices = json.loads(r.stdout or "[]")
        except ValueError:
            raise ModeError("powerctl list --json did not return JSON; not switching anything")
        matches = [d for d in devices if isinstance(d, dict) and d.get("alias") == outlet]
        if len(matches) != 1:
            raise ModeError(f"powerctl lists {len(matches)} outlets named exactly {outlet!r}; need exactly one")
        want_id = board.get("outlet_id", "").strip()
        if want_id and matches[0].get("device_id") != want_id:
            raise ModeError(f"the outlet named {outlet!r} has device id {matches[0].get('device_id')}, "
                            f"not the configured {want_id}; refusing to switch it")
        return outlet

    def wait_silent(self, timeout: float) -> bool:
        """True once the board stops answering HTTP (after a REST power-off)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            status, _ = self.http("GET", "/v1/info", timeout=2.0)
            if status is None:
                return True
            time.sleep(1)
        return False

    def notify(self, text: str):
        log(text)
        if shutil.which("notify-send") and os.environ.get("C64U_MODE_NOTIFY", "1") != "0" and not self.dry_run:
            subprocess.run(["notify-send", "-a", "c64u-mode", "C64U", text], check=False)


def _find_current(data) -> str | None:
    """The 'current' value anywhere in a /v1/configs answer."""
    if isinstance(data, dict):
        if "current" in data and not isinstance(data["current"], (dict, list)):
            return str(data["current"])
        for v in data.values():
            found = _find_current(v)
            if found is not None:
                return found
    return None


# ---- flash images -------------------------------------------------------------------------------------------------

class FlashStore:
    """Whole-flash images per name, and state.json: what the flash holds (a hint), its state, the power setting."""

    def __init__(self, root: str = os.path.join(DATA, "flash")):
        self.root = root

    def _state_path(self):
        return os.path.join(self.root, "state.json")

    def _state(self) -> dict:
        try:
            with open(self._state_path()) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, default=None):
        return self._state().get(key, default)

    def update(self, **values):
        data = self._state()
        data.update(values)
        os.makedirs(self.root, exist_ok=True)
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self._state_path())

    def current(self) -> str | None:
        return self.get("current")

    def set_current(self, name: str):
        self.update(current=name, state="clean", since=_dt.datetime.now().isoformat(timespec="seconds"))

    def images(self, name: str) -> list[str]:
        return sorted(glob.glob(os.path.join(self.root, name, "*.bin")))

    def latest(self, name: str) -> str | None:
        files = self.images(name)
        return files[-1] if files else None

    def new_path(self, name: str) -> str:
        return os.path.join(self.root, name, _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".bin")

    def names(self) -> list[str]:
        return sorted(d for d in os.listdir(self.root) if os.path.isdir(os.path.join(self.root, d))) \
            if os.path.isdir(self.root) else []

    def code_hash(self, image: str) -> str:
        """SHA-256 of the image's first 4 MiB, cached in <image>.code."""
        side = image + ".code"
        if os.path.isfile(side):
            with open(side) as f:
                return f.read().split()[0]
        h = sha256(image, CODE_SIZE)
        with open(side, "w") as f:
            f.write(h + "\n")
        return h

    def names_of_code(self, code: str, exclude: str | None = None) -> list[str]:
        """The names whose images carry this code (bitstream and application); `exclude` is not looked at."""
        return [name for name in self.names()
                if any(img != exclude and self.code_hash(img) == code for img in self.images(name))]

    def name_of_code(self, code: str, exclude: str | None = None) -> str | None:
        names = [n for n in self.names_of_code(code, exclude) if not n.startswith("unknown-")]
        return names[0] if len(names) == 1 else None


def sha256(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20 if left is None else min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
                if left <= 0:
                    break
    return h.hexdigest()


def read_flash(board: Board, dest: str) -> str:
    """Read the whole flash twice into dest; both reads must agree. Returns dest."""
    board.flash_read(dest)
    if board.dry_run:
        return dest
    check = dest + ".check"
    board.flash_read(check)
    same = sha256(dest) == sha256(check)
    os.remove(check)
    if not same:
        os.remove(dest)
        raise ModeError("two reads of the flash differ; nothing saved")
    with open(dest + ".sha256", "w") as f:
        f.write(f"{sha256(dest)}  {os.path.basename(dest)}\n")
    return dest


def snapshot(board: Board, store: FlashStore, name: str, force: bool = False) -> str:
    """Save the flash as a new image of <name>. Refuses when its code belongs to another name (unless force)."""
    dest = read_flash(board, store.new_path(name))
    if board.dry_run:
        return dest
    owners = [n for n in store.names_of_code(store.code_hash(dest), exclude=dest) if n != name]
    owner = owners[0] if owners else None
    if owner and not force:
        for side in ("", ".sha256", ".code"):
            os.remove(dest + side)
        raise ModeError(f"the flash carries the code of {owner!r}, not {name!r}; nothing saved "
                        f"(--force files it under {name!r} anyway)")
    store.set_current(name)
    log(f"saved the flash as {dest}")
    return dest


def flash_swap(board: Board, store: FlashStore, target: str, force: bool = False) -> bool:
    """Make the flash hold the latest image of <target>. The flash is read and filed by its code first."""
    image = store.latest(target)
    if not image:
        raise ModeError(f"no image named {target!r}: save one with 'c64u-mode snapshot {target}' while the flash "
                        f"holds it (known: {', '.join(store.names()) or 'none'})")
    if store.get("state", "clean") != "clean" and not force:
        raise ModeError(f"the last flash write did not finish ({store.get('state')}); the flash content is unknown. "
                        f"Write a known image with --force, e.g. 'c64u-mode flash:{target} --force'")
    if board.dry_run:
        log(f"would read the flash (twice), file it by its code, then write {image} and read it back")
        return True
    tmp = read_flash(board, os.path.join(store.root, ".incoming.bin"))
    code = store.code_hash(tmp)
    name = store.name_of_code(code)
    if name == target and sha256(tmp) == sha256(image):
        for side in ("", ".sha256", ".code"):
            os.remove(tmp + side)
        store.set_current(target)
        log(f"the flash holds {target!r} already")
        return False
    keep = store.new_path(name or "unknown-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(keep), exist_ok=True)
    for side in ("", ".sha256", ".code"):
        os.replace(tmp + side, keep + side)
    log(f"saved what the flash held as {keep}" + ("" if name else " (code of no known image)"))
    store.update(state=f"writing {target}")
    log(f"writing {image} ({target}) to the flash")
    board.flash_write(image)
    back = os.path.join(store.root, ".readback.bin")
    board.flash_read(back)
    ok = sha256(back) == sha256(image)
    os.remove(back)
    if not ok:
        raise ModeError(f"the flash does not read back as {image}; the board may not start. Write it again: "
                        f"c64u-mode flash:{target} --force")
    store.set_current(target)
    log(f"the flash holds {target!r}")
    return True


# ---- modes and plugins --------------------------------------------------------------------------------------------

class Mode:
    """run(ctx, args) switches; reached(ctx) -> bool says whether the board is in the mode now (for ensure)."""

    def __init__(self, name: str, description: str, run, needs_args: bool = False, reached=None, timeout: float = 180):
        self.name, self.description, self.run, self.needs_args = name, description, run, needs_args
        self.reached, self.timeout = reached, timeout


class Registry:
    def __init__(self):
        self.modes: dict[str, Mode] = {}
        self.detectors = []          # functions(board) -> str | None, describing what runs

    def add(self, name: str, description: str, run, needs_args: bool = False, reached=None, timeout: float = 180):
        self.modes[name] = Mode(name, description, run, needs_args, reached, timeout)

    def detector(self, fn):
        self.detectors.append(fn)
        return fn


def builtin(reg: Registry, store: FlashStore):
    def flash_mode(target):
        def run(ctx, args):
            changed = flash_swap(ctx.board, ctx.store, target, force=ctx.force)
            if changed:
                ctx.board.reload()
            ctx.board.notify(f"the flash holds {target}" + ("; reloaded from flash" if changed else ""))

        def reached(ctx):
            return ctx.store.current() == target and ctx.store.get("state", "clean") == "clean"
        return run, reached

    for name in store.names():
        if name.startswith("unknown-"):
            continue
        run, reached = flash_mode(name)
        reg.add(f"flash:{name}", f"swap the whole flash to the saved image {name!r}", run, reached=reached)

    def jtag(ctx, args):
        if not args:
            raise ModeError("jtag needs the arguments of u64ii_jtag.sh run, after --")
        ctx.board.jtag_run(args)
        ctx.board.notify("JTAG run started (volatile; a power cycle returns to the flash)")

    reg.add("jtag", "run any bitstream/application from RAM (u64ii_jtag.sh run ...)", jtag, needs_args=True)

    def reload(ctx, args):
        ctx.board.reload()
        ctx.board.notify("reloaded from flash; the flashed firmware starts")

    reg.add("reload", "reconfigure the FPGA from flash (the flashed firmware starts)", reload)

    @reg.detector
    def ultimate(board):
        info = board.rest_info()
        if info:
            return f"{info.get('product', 'Ultimate')} firmware {info.get('firmware_version')}" \
                   f" (FPGA {info.get('fpga_version')})"
        return None


def plugin_dirs() -> list[str]:
    dirs = [os.path.join(HERE, "c64u_modes")]
    dirs += [d for d in os.environ.get("C64U_MODE_PLUGINS", "").split(":") if d]
    return dirs


def load_plugins(reg: Registry, cfg) -> list[str]:
    loaded = []
    for d in plugin_dirs():
        for path in sorted(glob.glob(os.path.join(d, "*.py"))):
            if os.path.basename(path).startswith(("_", "test_")):
                continue
            spec = importlib.util.spec_from_file_location("c64u_mode_plugin_" + os.path.basename(path)[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(reg, cfg)
                loaded.append(path)
    return loaded


class Context:
    def __init__(self, board: Board, store: FlashStore, cfg, registry: Registry):
        self.board, self.store, self.cfg, self.registry = board, store, cfg, registry

        self.force = False

    def mode(self, name: str, args=()):
        """Run another mode (plugins build on flash:<name>)."""
        m = self.registry.modes.get(name)
        if not m:
            raise ModeError(f"mode {name!r} does not exist" + (
                f": save the image first with 'c64u-mode snapshot {name[6:]}' while the flash holds it"
                if name.startswith("flash:") else ""))
        m.run(self, list(args))

    def ensure(self, name: str, args=()) -> bool:
        """Run the mode unless the board is in it already; True when it had to switch."""
        m = self.registry.modes.get(name)
        if not m:
            raise ModeError(f"unknown mode {name!r}; 'c64u-mode list' shows the modes")
        if m.reached is None:
            raise ModeError(f"mode {name!r} cannot tell whether the board is in it; run it without 'ensure'")
        if m.reached(self):
            log(f"the board is in {name} already")
            return False
        m.run(self, list(args))
        if self.board.dry_run:
            return True
        end = time.monotonic() + m.timeout
        while time.monotonic() < end:
            if m.reached(self):
                return True
            time.sleep(0.5)
        raise ModeError(f"the board did not reach {name} within {int(m.timeout)} s")


def resolve(name: str, cfg, reg: Registry) -> str:
    alias = cfg["modes"].get(name)
    if alias:
        return alias.split()[0]
    return name


def status(board: Board, reg: Registry, store: FlashStore) -> str:
    seen = [d(board) for d in reg.detectors]
    seen = [s for s in seen if s]
    lines = [f"board {board.host}: " + ("; ".join(seen) if seen else "no answer from any known firmware")]
    state = store.get("state", "clean")
    lines.append(f"flash holds: {store.current() or 'unknown (no snapshot recorded)'} (recorded; state {state})")
    return "\n".join(lines)


# ---- desktop launchers --------------------------------------------------------------------------------------------

def install_desktop(cfg, exe: str, apps_dir: str | None = None, dry_run: bool = False) -> list[str]:
    """One .desktop file per [modes] entry; hotkeys from [hotkeys] go to KDE's kglobalshortcutsrc."""
    apps_dir = apps_dir or os.path.expanduser("~/.local/share/applications")
    written = []
    for short in cfg["modes"]:
        fname = f"c64u-mode-{short}.desktop"
        path = os.path.join(apps_dir, fname)
        text = ("[Desktop Entry]\nType=Application\n"
                f"Name=C64U: {short}\nComment=c64u-mode {short} ({cfg['modes'][short]})\n"
                f"Exec={exe} {short} --allow-power-cycle\nIcon=computer\nTerminal=false\nCategories=Utility;\n")
        hk = cfg["hotkeys"].get(short)
        if hk:
            text += f"X-KDE-Shortcuts={hk}\n"
        log(("would write " if dry_run else "write ") + path)
        if not dry_run:
            os.makedirs(apps_dir, exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
            if hk and shutil.which("kwriteconfig6"):
                subprocess.run(["kwriteconfig6", "--file", "kglobalshortcutsrc", "--group", "services",
                                "--group", fname, "--key", "_launch", hk], check=False)
        written.append(path)
    if not dry_run and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", apps_dir], check=False)
    return written


# ---- command line -------------------------------------------------------------------------------------------------

def main(argv=None, board_factory=Board) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="c64u-mode", description=__doc__.splitlines()[0])
    p.add_argument("command", help="status, list, ensure, snapshot, check-power, install-desktop, or a mode")
    p.add_argument("name", nargs="?", help="ensure: the mode; snapshot: the image name")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    p.add_argument("--wait", type=float, default=0.0, metavar="S",
                   help="wait up to S seconds for the device lock (default: refuse at once when it is held)")
    p.add_argument("--allow-power-cycle", action="store_true", help="this command may power-cycle the board")
    p.add_argument("--force", action="store_true",
                   help="snapshot: file under the name anyway; flash:<name>: write although the last write failed")
    a = p.parse_args(argv)

    cfg = load_config()
    store = FlashStore()
    try:
        board = board_factory(cfg, dry_run=a.dry_run, allow_power_cycle=a.allow_power_cycle, state=store)
    except TypeError:                 # a test's factory that takes only (cfg, dry_run)
        board = board_factory(cfg, dry_run=a.dry_run)
    reg = Registry()
    builtin(reg, store)
    load_plugins(reg, cfg)
    ctx = Context(board, store, cfg, reg)
    ctx.force = a.force
    try:
        if a.command == "status":
            print(status(board, reg, store))
        elif a.command == "list":
            for short in cfg["modes"]:
                print(f"{short:14s} -> {cfg['modes'][short]}")
            for m in reg.modes.values():
                print(f"{m.name:14s}    {m.description}")
        elif a.command == "install-desktop":
            exe = shutil.which("c64u-mode") or os.path.join(HERE, "c64u_mode.sh")
            install_desktop(cfg, exe, dry_run=a.dry_run)
        elif a.command == "check-power":
            status_code, data = board.http("GET", "/v1/configs/U64%20Specific%20Settings/Power%20On%20After%20Power%20Loss")
            cur = _find_current(data) if status_code == 200 else None
            if status_code == 200 and cur is None:
                raise ModeError("the firmware that answers does not report the setting; start the Ultimate firmware")
            if cur != "On":
                raise ModeError("'Power On After Power Loss' is not readable as On "
                                f"({cur or 'no Ultimate REST'}); set it to On in the Ultimate firmware first")
            if not a.dry_run:
                store.update(power_on_seen=time.time())
            log("'Power On After Power Loss' is On; recorded")
        else:
            with DeviceLock(" ".join([a.command] + ([a.name] if a.name else [])), wait=a.wait):
                if a.command == "snapshot":
                    if not a.name:
                        raise ModeError("snapshot needs a name: what the flash holds now, e.g. 'stock'")
                    snapshot(board, store, a.name, force=a.force)
                elif a.command == "ensure":
                    if not a.name:
                        raise ModeError("ensure needs a mode, e.g. 'c64u-mode ensure c64u'")
                    ctx.ensure(resolve(a.name, cfg, reg), extra)
                else:
                    name = resolve(a.command, cfg, reg)
                    if name not in reg.modes:
                        raise ModeError(f"unknown mode {a.command!r}; 'c64u-mode list' shows the modes")
                    ctx.mode(name, extra)
    except ModeError as e:
        board.notify(f"failed: {e}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
