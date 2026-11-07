#!/usr/bin/env python3
"""Update the firmware of a working Ultimate II+L through the C64 Ultimate it is plugged into.

    ./build-tool --u2-flash [OPTIONS]
    python3 tooling/u2_flash.py --host U2_HOST --c64-host C64U_HOST --artifact update_ID.u2l \\
        --confirm-flash --confirm-power-cycle

The tool uploads the .u2l image over FTP, checks the upload through REST, opens the cartridge's
menu overlay, browses to the file, runs the update, watches it finish, and then power-cycles the
C64 Ultimate. A cartridge that does not boot needs tooling/u2pl_jtag.py instead.

Hosts come from --host and --c64-host, or from U2_FLASH_HOST and U2_FLASH_C64_HOST (which
./build-tool also reads from .build-tool.env). U2_FLASH_PASSWORD and U2_FLASH_C64_PASSWORD hold
the REST passwords. There are no default hosts.

Why two hosts. machine:menu_screen is local UI state: only the cartridge can show its own overlay,
so the screen is read from --host. The cartridge's own machine:input is compiled out
(software/api/route_input.cc builds it under `#if U64`, so it answers HTTP 501), but the keyboard
matrix of the host reaches the cartridge over the cartridge bus. Every keystroke therefore goes to
--c64-host's machine:input. The cartridge's updater cannot power-cycle the machine
(software/application/update_u2p/update_common.h only does so for U64 builds); it prints
"PLEASE TURN OFF YOUR MACHINE" and stops. Finishing the update needs a power cycle of --c64-host,
which powers the cartridge port. The tool does that through the host's own menu (F1, Power &
Reset, Power Cycle) and only with --confirm-power-cycle. When the menu differs from that path, the
tool stops without touching anything and asks for a manual power cycle.

Once the update runs, the cartridge's REST API stays down until the power cycle. The tool then
reads the updater's text from the C64 screen RAM ($0400-$07E7) through --c64-host's
machine:readmem. The overlay draws into that memory with the same glyph convention as
machine:menu_screen.

The selected row of a listing is found by its foreground colour: 1 for the selected row, 12 for
the others. The tool never counts keypresses. It reads the cursor row after every key and steps
towards the target row, so a dropped key cannot move the selection onto another entry. A wrong
entry on the cartridge's USB list, such as WiFi, is not harmless.

Without --confirm-flash the tool stops at the "Run Update" context menu and backs out. It holds
the c64u device lock (tooling/device_lock.py) for the whole run, except with --dry-run, and
refuses when someone else holds it, or waits with --wait SECONDS. A lock held by an ancestor,
for example tooling/with_device_locks.py, covers the run.

Exit codes:
  0  success
  1  stopped safely: nothing destructive was attempted, a destructive step was withheld because
     its confirmation flag was missing, or the device lock is held by someone else
  2  unsafe or unknown state: a flash may be in progress; inspect the device by hand and do not
     power-cycle anything
  3  usage error
"""

import argparse
import contextlib
import ftplib
import glob
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import device_lock  # noqa: E402

ARTIFACT_EXTENSION = "u2l"
DEFAULT_DEST_ROOT = "/USB1/Firmware/latest"
DEFAULT_REMOTE_NAME = "update.u2l"
MIN_ARTIFACT_SIZE = 256 * 1024
MAX_ARTIFACT_SIZE = 32 * 1024 * 1024

# Texts of software/application/update_u2p/update_common.h and update_u2pl/update.cc.
TXT_REFORMAT_PROMPT = "Reformat Flash Disk?"
TXT_FLASH_CONFIRM = re.compile(r"\bFlash \S[^?]*\?")        # "Flash <version>?"
TXT_RESET_CONFIG_PROMPT = "Reset Configuration?"
TXT_WIFI_UPDATE_PROMPT = "Want to update the WiFi Module?"
TXT_FLASHING = "Flashing"
TXT_DONE = "Done!"
TXT_TURN_OFF_MANUAL = "PLEASE TURN OFF YOUR MACHINE"
TXT_RUN_UPDATE = "Run Update"
TXT_SAVE_PROMPT = "Save changes to Flash?"

# In the file browser title ("*** Ultimate II+L 3.15 (122) ***") and in the updater title; it
# tells the cartridge's overlay from the C64 Ultimate's own menu.
U2L_MENU_MARKER = "Ultimate II+L"

# Progress words such as "Flashing" and "Writing" are expected; these are not.
DANGER_SUBSTRINGS = ("error", "fail", "abort", "problem", "retry")

KEY_LEFT = ["left_shift", "cursor_left_right"]
KEY_RIGHT = ["cursor_left_right"]
KEY_UP = ["left_shift", "cursor_up_down"]
KEY_DOWN = ["cursor_up_down"]
KEY_RETURN = ["return"]
KEY_RUN_STOP = ["run_stop"]

MENU_SCREEN_PATH = "/v1/machine:menu_screen"
MENU_BUTTON_PATH = "/v1/machine:menu_button"
INPUT_PATH = "/v1/machine:input"
READMEM_PATH = "/v1/machine:readmem"
SCREEN_WIDTH = 40
SCREEN_HEIGHT = 25
SCREEN_CELLS = SCREEN_WIDTH * SCREEN_HEIGHT
SELECTED_FG_COLOR = 1
LISTING_ROWS = (2, SCREEN_HEIGHT - 1)

NAV_SETTLE_SECONDS = 0.3
ENTER_SETTLE_SECONDS = 0.6
POPUP_SETTLE_SECONDS = 0.5
MENU_TOGGLE_SETTLE_SECONDS = 0.25
MENU_WAIT_SECONDS = 2.0
MARKER_WAIT_SECONDS = 8.0
FLASH_START_SECONDS = 2.0
POLL_SECONDS = 1.0
GET_ATTEMPTS = 3
GET_RETRY_SECONDS = 0.5
MENU_CLOSE_ATTEMPTS = 12
FILESYSTEM_ROOT_STEPS = 15
SELECT_ROW_STEPS = 48

EXIT_OK = 0
EXIT_ABORTED_SAFE = 1
EXIT_UNSAFE_UNKNOWN = 2
EXIT_USAGE = 3


class Failure(Exception):
    """A step failed; what that means depends on whether a flash has started."""


class Abort(Exception):
    """Stop safely: nothing destructive has happened."""


class UnsafeState(Exception):
    """Evidence is inconclusive while a flash may be running. Never followed by a power cycle."""


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

class Rest:
    """The REST API of one device. A GET is repeated after a transport error; nothing else is,
    because a request that reached the device may have been applied."""

    def __init__(self, host, password=None, timeout=10.0):
        self.host, self.password, self.timeout = host, password, timeout

    def request(self, method, path, params=None, payload=None):
        """Returns (status, headers with lower-case names, body). Raises Failure on a transport error."""
        if params:
            path += "?" + urllib.parse.urlencode(params)
        headers = {"X-Password": self.password} if self.password else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        attempts = GET_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            connection = http.client.HTTPConnection(self.host, timeout=self.timeout)
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
            except (OSError, http.client.HTTPException) as exc:
                error = exc
            finally:
                connection.close()
            if attempt + 1 < attempts:
                time.sleep(GET_RETRY_SECONDS)
        raise Failure(f"{method} {path} on {self.host}: {error}")

    def menu_screen(self):
        """The menu screen (2000 character bytes, then 2000 colour bytes), or None while no menu is open."""
        status, headers, body = self.request("GET", MENU_SCREEN_PATH)
        if status == 404:
            return None
        if status != 200 or "application/octet-stream" not in headers.get("content-type", ""):
            raise Failure(f"menu_screen: HTTP {status}, {headers.get('content-type')!r}: {body[:160]!r}")
        if len(body) != 2 * SCREEN_CELLS:
            raise Failure(f"menu_screen: expected {2 * SCREEN_CELLS} bytes, got {len(body)}")
        return body

    def press_menu_button(self):
        status, _, body = self.request("PUT", MENU_BUTTON_PATH)
        if status != 200:
            raise Failure(f"menu_button: HTTP {status}: {body[:160]!r}")
        time.sleep(MENU_TOGGLE_SETTLE_SECONDS)

    def input_supported(self):
        # machine:input exists only on U64-class hardware and answers 501 on a U2+L; anything but 200
        # counts as unsupported.
        return self.request("GET", INPUT_PATH)[0] == 200

    def tap(self, keys):
        status, _, body = self.request(
            "POST", INPUT_PATH, payload={"events": [{"kind": "keyboard", "inputs": keys, "transition": "tap"}]})
        if status != 200:
            raise Failure(f"input {keys}: HTTP {status}: {body[:160]!r}")

    def files_info(self, remote_path):
        """The files:info record of a file, or None when it does not exist."""
        status, _, body = self.request("GET", "/v1/files/" + urllib.parse.quote(remote_path) + ":info")
        if status == 404:
            return None
        if status != 200:
            raise Failure(f"files:info: HTTP {status} for {remote_path}: {body[:200]!r}")
        try:
            files = json.loads(body.decode()).get("files")
        except (ValueError, AttributeError) as exc:
            raise Failure(f"files:info: malformed response for {remote_path}: {body[:200]!r}") from exc
        if not isinstance(files, dict):
            raise Failure(f"files:info: no file record for {remote_path}: {body[:200]!r}")
        return files

    def screen_ram_text(self):
        """The 25 rows of C64 screen RAM as text, or None when it cannot be read."""
        try:
            status, _, body = self.request("GET", READMEM_PATH, params={"address": "0400", "length": 1000})
        except Failure:
            return None
        if status != 200 or len(body) != 1000:
            return None
        return "\n".join(glyphs(body[row * 40:(row + 1) * 40]) for row in range(25))


def glyphs(cells):
    return "".join(chr(c & 0x7F) if 0x20 <= (c & 0x7F) <= 0x7E else " " for c in cells)


# ---------------------------------------------------------------------------
# Menu screens and navigation
# ---------------------------------------------------------------------------

class Screen:
    def __init__(self, body):
        self.chars, self.colors = body[:SCREEN_CELLS], body[SCREEN_CELLS:]
        self.rows = [glyphs(self.chars[r * SCREEN_WIDTH:(r + 1) * SCREEN_WIDTH]).strip() for r in range(SCREEN_HEIGHT)]
        self.text = "\n".join(self.rows)

    def selected_row(self, min_col=0, row_range=LISTING_ROWS):
        """The row whose first non-blank cell at or after min_col has the selected colour, or None.

        row_range keeps the scan inside the overlay: a menu below an overlay keeps its own stale
        highlight."""
        for row in range(*row_range):
            for col in range(min_col, SCREEN_WIDTH):
                index = row * SCREEN_WIDTH + col
                if 0x21 <= (self.chars[index] & 0x7F) <= 0x7E:
                    if (self.colors[index] & 0x0F) == SELECTED_FG_COLOR:
                        return row
                    break
        return None


class Menu:
    """A menu that is read from one device and driven by the keys of another (or the same) one."""

    def __init__(self, reader, keys):
        self.reader, self.keys = reader, keys

    def screen(self):
        body = self.reader.menu_screen()
        if body is None:
            raise Failure("the menu is not open")
        return Screen(body)

    def try_screen(self):
        body = self.reader.menu_screen()
        return None if body is None else Screen(body)

    def tap(self, keys):
        self.keys.tap(keys)

    def wait(self, want_open, timeout=MENU_WAIT_SECONDS):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self.reader.menu_screen() is not None) == want_open:
                return True
            time.sleep(MENU_TOGGLE_SETTLE_SECONDS)
        return False

    def open(self):
        self.reader.press_menu_button()
        if not self.wait(True):
            raise Failure("the menu stayed closed after the menu button")

    def wait_marker(self, marker, timeout=MARKER_WAIT_SECONDS, interval=0.5):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            screen = self.try_screen()
            if screen is not None:
                last = screen
                if marker in screen.text:
                    return screen
            time.sleep(interval)
        if last is not None:
            raise Failure(f"menu opened but {marker!r} not found; last screen:\n{last.text}")
        raise Failure(f"the menu never opened while waiting for {marker!r}")

    def close_from_anywhere(self):
        """Back out with RUN/STOP, answering a "Save changes to Flash?" popup with No."""
        for _ in range(MENU_CLOSE_ATTEMPTS):
            screen = self.try_screen()
            if screen is None:
                return
            self.tap(["n"] if TXT_SAVE_PROMPT in screen.text else KEY_RUN_STOP)
            time.sleep(POPUP_SETTLE_SECONDS)
        if self.reader.menu_screen() is None:
            return
        self.reader.press_menu_button()
        if not self.wait(False):
            raise Failure("the menu stayed open after the menu button")


def first_token(name):
    """Matches a listing row whose first word is `name` (a directory row may add size and date columns)."""
    def match(row):
        words = row.split()
        return bool(words) and words[0].upper() == name.upper()
    return match


def contains(needle):
    return lambda row: needle.upper() in row.upper()


def select_row(menu, match, label, min_col=0, row_range=LISTING_ROWS, max_steps=SELECT_ROW_STEPS):
    """Move the cursor onto the first row that `match` accepts, one key at a time, and confirm it."""
    screen = menu.screen()
    for _ in range(max_steps):
        target = next((r for r in range(*row_range) if match(screen.rows[r])), None)
        if target is None:
            raise Failure(f"{label!r} not found in the listing; screen:\n{screen.text}")
        current = screen.selected_row(min_col, row_range)
        if current == target:
            log(f"Confirmed selection on {screen.rows[current]!r}")
            return screen
        if current is None:
            raise Failure(f"could not determine the cursor row; screen:\n{screen.text}")
        menu.tap(KEY_DOWN if current < target else KEY_UP)
        time.sleep(NAV_SETTLE_SECONDS)
        screen = menu.screen()
    raise Failure(f"could not reach {label!r} after {max_steps} steps; last screen:\n{screen.text}")


def go_to_filesystem_root(menu, volume):
    screen = None
    for attempt in range(FILESYSTEM_ROOT_STEPS + 1):
        screen = menu.screen()
        if any(first_token(volume)(row) for row in screen.rows):
            return screen
        if attempt < FILESYSTEM_ROOT_STEPS:
            menu.tap(KEY_LEFT)
            time.sleep(NAV_SETTLE_SECONDS)
    raise Failure(f"could not reach the filesystem root (no {volume!r} row) after "
                  f"{FILESYSTEM_ROOT_STEPS} LEFT presses; last screen:\n{screen.text}")


def assert_overlay(screen):
    if U2L_MENU_MARKER not in screen.text:
        raise Failure(f"expected the U2+L overlay ({U2L_MENU_MARKER!r}), but the screen shows:\n{screen.text}")


# ---------------------------------------------------------------------------
# Artifact and upload
# ---------------------------------------------------------------------------

def discover_local_artifact(repo_root):
    plain = os.path.join(repo_root, DEFAULT_REMOTE_NAME)
    if os.path.isfile(plain):
        return plain
    candidates = glob.glob(os.path.join(repo_root, f"update_*.{ARTIFACT_EXTENSION}"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def local_precheck(path):
    """Returns (size, sha256) of a plausible .u2l, or raises Failure."""
    if not os.path.isfile(path):
        raise Failure(f"artifact not found: {path}")
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext != ARTIFACT_EXTENSION:
        raise Failure(f"artifact extension '.{ext}' is not '.{ARTIFACT_EXTENSION}': {path}")
    size = os.path.getsize(path)
    if not MIN_ARTIFACT_SIZE <= size <= MAX_ARTIFACT_SIZE:
        raise Failure(f"artifact size {size} bytes is outside {MIN_ARTIFACT_SIZE}..{MAX_ARTIFACT_SIZE}: {path}")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    log(f"Local artifact OK: {path} ({size} bytes, sha256={digest.hexdigest()})")
    return size, digest.hexdigest()


def git_output(repo_root, *args):
    try:
        result = subprocess.run(["git", "-C", repo_root, *args], capture_output=True, text=True,
                                timeout=10, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def path_component(value):
    """One directory name: no extra levels, no empty or traversal segments."""
    return value.strip().replace("/", "-").replace("..", "-") or "unknown"


def ftp_dir_exists(ftp, path):
    current = ftp.pwd()
    try:
        ftp.cwd(path)
        return True
    except ftplib.error_perm:
        return False
    finally:
        ftp.cwd(current)


def ftp_file_exists(ftp, path):
    try:
        ftp.size(path)
        return True
    except ftplib.error_perm:
        return False


def ftp_root_entries(ftp):
    try:
        return ", ".join(sorted(ftp.nlst("/"))) or "(none)"
    except ftplib.all_errors:
        return "(unavailable)"


def ftp_ensure_dirs(ftp, remote_dir):
    path = ""
    for part in (p for p in remote_dir.split("/") if p):
        path += "/" + part
        if ftp_dir_exists(ftp, path):
            continue
        try:
            ftp.mkd(path)
            log(f"Created remote directory: {path}")
        except ftplib.error_perm as exc:
            if not ftp_dir_exists(ftp, path):
                raise Failure(f"could not create remote directory {path}: {exc}; "
                              f"entries at the FTP root: {ftp_root_entries(ftp)}") from exc


def ftp_upload_atomic(ftp, local_path, remote_dir, name, local_size):
    """Upload under a temporary name, check the size, and rename. An existing final file is never replaced."""
    temp, final = f"{remote_dir}/{name}.uploading", f"{remote_dir}/{name}"
    if ftp_file_exists(ftp, final):
        raise Failure(f"remote artifact already exists at {final}; refusing to overwrite "
                      "(use another --git-id or remove it)")
    if ftp_file_exists(ftp, temp):        # left by an interrupted attempt; the server's STOR will not replace it
        log(f"Removing a partial upload from an earlier attempt: {temp}")
        ftp.delete(temp)
    log(f"Uploading {local_path} -> {temp}")
    with open(local_path, "rb") as handle:
        ftp.storbinary(f"STOR {temp}", handle)
    remote_size = ftp.size(temp)
    if remote_size != local_size:
        raise Failure(f"FTP upload size mismatch: local {local_size} bytes, remote {remote_size} bytes at {temp}")
    ftp.rename(temp, final)
    log(f"Renamed into place: {final}")


def upload_artifact(args, local_path, remote_dir, name, local_size):
    try:
        with ftplib.FTP() as ftp:
            ftp.connect(args.host, timeout=30)
            ftp.login(args.ftp_user, args.ftp_pass)
            ftp_ensure_dirs(ftp, remote_dir)
            ftp_upload_atomic(ftp, local_path, remote_dir, name, local_size)
    except ftplib.all_errors as exc:
        raise Failure(f"FTP error: {exc}") from exc


def verify_upload(rest, remote_path, expected_size):
    info = rest.files_info(remote_path)
    if info is None:
        raise Failure(f"remote file not found through REST: {remote_path}")
    if info.get("size") != expected_size:
        raise Failure(f"remote size {info.get('size')} != local size {expected_size} for {remote_path}")
    log(f"VERIFY_UPLOAD OK through REST: {remote_path} ({expected_size} bytes)")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.local_artifact = self.local_sha256 = self.remote = self.completion_evidence = ""
        self.local_size = 0
        self.state, self.result = "PRECHECK", "UNKNOWN"
        self.reboot_performed = False
        self.observed = []

    def note(self, tag, text):
        self.observed.append(f"[{tag}] " + " | ".join(line.strip() for line in text.split("\n") if line.strip())[:160])

    def set_state(self, state):
        self.state = state
        log(f"\n=== STATE: {state} ===")

    def print_summary(self):
        log("\n" + "=" * 70 + "\nU2+L FLASH SUMMARY\n" + "=" * 70)
        log(f"Local artifact:      {self.local_artifact}")
        log(f"Local size / sha256: {self.local_size} bytes / {self.local_sha256}")
        log(f"Remote destination:  {self.remote}")
        log(f"Final state:         {self.state}")
        log(f"Completion evidence: {self.completion_evidence or '(none)'}")
        log(f"Reboot performed:    {self.reboot_performed}")
        log(f"Result:              {self.result}")
        if self.observed:
            log("Observed screens (most recent last):")
            for line in self.observed[-12:]:
                log(f"  {line}")
        log("=" * 70)


@contextlib.contextmanager
def phase(report, state, what):
    """Run one step. A Failure before the flash starts is a safe stop."""
    report.set_state(state)
    try:
        yield
    except Failure as exc:
        raise Abort(f"{what}: {exc}") from exc


def execute(args, report):
    # ---- PRECHECK --------------------------------------------------------
    report.set_state("PRECHECK")
    repo_root = args.repo_root or git_output(os.getcwd(), "rev-parse", "--show-toplevel") or os.getcwd()
    artifact = args.artifact or discover_local_artifact(repo_root)
    if not artifact:
        raise Abort(f"no U2+L (.{ARTIFACT_EXTENSION}) artifact found under {repo_root}")
    try:
        size, digest = local_precheck(artifact)
    except Failure as exc:
        raise Abort(str(exc)) from exc
    report.local_artifact, report.local_size, report.local_sha256 = artifact, size, digest

    branch = path_component(args.branch or git_output(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown")
    date = args.date or datetime.now().strftime("%y%m%d")
    git_id = path_component(args.git_id or git_output(repo_root, "rev-parse", "--short", "HEAD") or "unknown")
    dest_root = args.dest_root.rstrip("/")
    remote_dir = f"{dest_root}/{branch}/{date}/{git_id}"
    name = args.remote_name
    report.remote = f"{remote_dir}/{name}"
    log(f"Branch={branch!r} Date={date!r} GitId={git_id!r}\nRemote destination: {report.remote}")

    if args.dry_run:
        log("\n[DRY RUN] Would upload the artifact over FTP to the path above, check it through REST,\n"
            "[DRY RUN] open the cartridge's menu, browse to the file and, with --confirm-flash, run the\n"
            "[DRY RUN] update, driving the keys through --c64-host. No network connection was made.")
        report.result = "DRY_RUN_OK"
        return EXIT_OK

    # ---- UPLOAD and VERIFY_UPLOAD ---------------------------------------
    with phase(report, "UPLOAD", "upload failed"):
        upload_artifact(args, artifact, remote_dir, name, size)
    cartridge = Rest(args.host, args.password, args.timeout)
    with phase(report, "VERIFY_UPLOAD", "upload verification failed"):
        verify_upload(cartridge, report.remote.lstrip("/"), size)
    if args.upload_only:
        report.result = "UPLOAD_ONLY_OK"
        return EXIT_OK
    if not args.c64_host:
        log("\n--c64-host was not given, so the menu cannot be driven (the cartridge cannot take REST keys). "
            f"The artifact is uploaded and verified. Finish by hand: open the menu, browse to {remote_dir}, "
            f"select {name}, and choose '{TXT_RUN_UPDATE}'.")
        report.result = "UPLOAD_VERIFIED_MANUAL_COMPLETION_REQUIRED (no --c64-host given)"
        return EXIT_OK
    host = Rest(args.c64_host, args.c64_password or args.password, args.timeout)
    if not host.input_supported():
        raise Abort(f"--c64-host {args.c64_host!r} does not support REST keyboard injection (machine:input); "
                    "it must be the C64 Ultimate the cartridge is plugged into, not the cartridge itself")

    # ---- OPEN_MENU ---------------------------------------------------------
    menu = Menu(cartridge, host)
    with phase(report, "OPEN_MENU", "could not open the U2+L menu"):
        if cartridge.menu_screen() is not None:
            log(f"Closing the menu that is open on {args.host} to start from a known state")
            menu.close_from_anywhere()
        cartridge.press_menu_button()
        report.note("OPEN_MENU", menu.wait_marker(U2L_MENU_MARKER).text)

    # ---- NAVIGATE_TO_FILE ----------------------------------------------------
    with phase(report, "NAVIGATE_TO_FILE", f"navigation to {name} failed"):
        segments = dest_root.strip("/").split("/") + [branch, date, git_id]
        screen = go_to_filesystem_root(menu, segments[0])
        assert_overlay(screen)
        report.note("root", screen.text)
        for segment in segments:
            select_row(menu, first_token(segment), segment)
            menu.tap(KEY_RIGHT)
            time.sleep(ENTER_SETTLE_SECONDS)
            screen = menu.screen()
            assert_overlay(screen)
            report.note(f"cd {segment}", screen.text)
        screen = select_row(menu, first_token(name), name)
        assert_overlay(screen)
        report.note("file selected", screen.text)

    # ---- CONFIRM_UPDATE_ACTION -------------------------------------------------
    with phase(report, "CONFIRM_UPDATE_ACTION", f"could not open the '{TXT_RUN_UPDATE}' context action"):
        menu.tap(KEY_RETURN)
        time.sleep(ENTER_SETTLE_SECONDS)
        screen = menu.screen()
        report.note("context menu", screen.text)
        if TXT_RUN_UPDATE not in screen.text:
            raise Failure(f"expected a context menu with {TXT_RUN_UPDATE!r}, got:\n{screen.text}")
        log(f"Context menu confirmed: {TXT_RUN_UPDATE!r} is present.")

    if not args.confirm_flash:
        log(f"\nNavigation is validated up to '{TXT_RUN_UPDATE}', but --confirm-flash was not given. "
            "Backing out without touching the flash.")
        with contextlib.suppress(Failure):
            menu.tap(KEY_RUN_STOP)
            time.sleep(NAV_SETTLE_SECONDS)
            menu.close_from_anywhere()
        report.result = "STOPPED_BEFORE_CONFIRM (use --confirm-flash to proceed)"
        return EXIT_ABORTED_SAFE

    # ---- FLASH_STARTED: from here on a REST failure or an unclear screen is UNSAFE ------
    report.set_state("FLASH_STARTED")
    try:
        menu.tap(KEY_RETURN)                    # "Run Update" is the only item and is preselected
        time.sleep(FLASH_START_SECONDS)
        return monitor_flash(menu, host, report, args)
    except Failure as exc:
        raise UnsafeState(f"REST failure after triggering {TXT_RUN_UPDATE}: {exc}") from exc


def answer(menu, prompt, yes):
    log(f"Answering {prompt!r} -> {'y' if yes else 'n'}")
    menu.tap(["y" if yes else "n"])
    time.sleep(POPUP_SETTLE_SECONDS)


def monitor_flash(menu, host, report, args):
    """Follow the updater on the C64 screen RAM until it asks for the power-off, then finish."""
    deadline = time.monotonic() + args.flash_timeout
    seen = set()
    turn_off_hits = 0
    report.set_state("FLASH_IN_PROGRESS")

    def first_time(key, condition):
        if key not in seen and condition:
            seen.add(key)
            return True
        return False

    while time.monotonic() < deadline:
        text = host.screen_ram_text()
        if text is None:
            time.sleep(POLL_SECONDS)
            continue
        report.note("flash", text)
        lowered = text.lower()
        danger = next((word for word in DANGER_SUBSTRINGS if word in lowered), None)
        if danger:
            raise UnsafeState(f"danger text {danger!r} on screen during the flash:\n{text}")

        # A popup stays on screen until its key arrives, so each prompt is answered once. The
        # confirmation pattern must not match the reformat prompt, which also reads "Flash ...?".
        if first_time("reformat", TXT_REFORMAT_PROMPT in text):
            answer(menu, TXT_REFORMAT_PROMPT, args.reformat_flash_disk)
            continue
        confirm = any(TXT_FLASH_CONFIRM.search(line) and TXT_REFORMAT_PROMPT not in line for line in text.split("\n"))
        if first_time("confirm", confirm):
            log("Answering the flash confirmation -> y")
            menu.tap(["y"])
            time.sleep(POPUP_SETTLE_SECONDS)
            continue
        upper = text.upper()
        if first_time("fpga", TXT_FLASHING in text and "FPGA" in upper):
            log("Observed: flashing the FPGA started")
        if first_time("app", TXT_FLASHING in text and "APPLICATION" in upper):
            log("Observed: flashing the application started")
        if "app" in seen and TXT_DONE in text:
            seen.add("done")
        if first_time("reset-config", TXT_RESET_CONFIG_PROMPT in text):
            answer(menu, TXT_RESET_CONFIG_PROMPT, args.reset_config)
            continue
        if first_time("wifi", TXT_WIFI_UPDATE_PROMPT in text):
            answer(menu, TXT_WIFI_UPDATE_PROMPT, args.update_wifi_module)
            continue

        if TXT_TURN_OFF_MANUAL in text:
            turn_off_hits += 1
            report.set_state("FLASH_COMPLETE_CANDIDATE")
            log(f"Observed the power-off instruction ({turn_off_hits} consecutive read(s))")
            if turn_off_hits >= args.confirm_reads:
                if not {"fpga", "app", "done"} <= seen:
                    raise UnsafeState("the power-off text was seen, but the progress markers are incomplete "
                                      f"(seen: {sorted(seen & {'fpga', 'app', 'done'})})")
                log(f"Settling {args.settle_seconds}s before declaring completion...")
                time.sleep(args.settle_seconds)
                report.set_state("FLASH_COMPLETE_CONFIRMED")
                report.completion_evidence = (f"'{TXT_TURN_OFF_MANUAL}' on {turn_off_hits} consecutive reads, "
                                              "after FPGA and application flashing and 'Done!'")
                return power_cycle(host, report, args)
        else:
            turn_off_hits = 0
        time.sleep(POLL_SECONDS)

    raise UnsafeState(f"flash monitoring timed out after {args.flash_timeout}s without a confirmed completion")


def power_cycle(host, report, args):
    """Power-cycle --c64-host through its menu, only with --confirm-power-cycle."""
    report.set_state("POWER_CYCLE_REQUESTED")
    if not args.confirm_power_cycle:
        report.result = "DONE (flash confirmed complete; --confirm-power-cycle withheld)"
        log(f"\nThe flash is confirmed complete. --confirm-power-cycle was not given, so {args.c64_host} is not "
            "touched. Power-cycle it by hand (it powers the cartridge port), or run again with the flag.")
        return EXIT_OK

    log(f"\nPower-cycling {args.c64_host!r} through Power & Reset -> Power Cycle...")
    menu = Menu(host, host)
    try:
        # The cartridge overlay is a frozen frame now (its CPU waits in the updater); use the host's own menu.
        menu.close_from_anywhere()
        menu.open()
        menu.tap(["f1"])
        time.sleep(ENTER_SETTLE_SECONDS)
        report.note("F1 menu", select_row(menu, contains("Power & Reset"), "Power & Reset",
                                          min_col=6, row_range=(8, 17), max_steps=20).text)
        menu.tap(KEY_RETURN)
        time.sleep(ENTER_SETTLE_SECONDS)
        report.note("Power & Reset submenu", select_row(menu, contains("Power Cycle"), "Power Cycle",
                                                        min_col=17, row_range=(8, 17), max_steps=20).text)
        log("Activating Power Cycle")
        menu.tap(KEY_RETURN)
    except Failure as exc:
        report.result = f"DONE (flash confirmed complete; power cycle of {args.c64_host} FAILED: {exc})"
        log(f"\nCould not drive the power cycle on {args.c64_host}: {exc}\n"
            "The flash itself is confirmed complete. Power-cycle the host by hand.")
        return EXIT_OK

    report.reboot_performed = True
    report.set_state("DONE")
    report.result = f"DONE (flash confirmed complete; power-cycled {args.c64_host})"
    return EXIT_OK


def run(args):
    report = Report()
    try:
        code = execute(args, report)
    except Abort as exc:
        report.result = f"ABORTED_SAFE: {exc}"
        log(f"\nABORT (safe): {exc}")
        code = EXIT_ABORTED_SAFE
    except UnsafeState as exc:
        report.result = f"UNSAFE/UNKNOWN: {exc}"
        log(f"\nABORT (UNSAFE / UNKNOWN STATE): {exc}\nDo NOT power-cycle or reset anything. Inspect the device by hand.")
        code = EXIT_UNSAFE_UNKNOWN
    report.print_summary()
    return code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits with 2, which this tool uses for "unsafe state".
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser():
    p = Parser(
        prog="u2_flash.py",
        description="Update a working Ultimate II+L: upload an update.u2l, run it from the cartridge's menu "
                    "with keys sent through the C64 Ultimate it is plugged into, and watch it finish. "
                    "See the header of tooling/u2_flash.py for the safety model.")
    p.add_argument("--host", help="the U2+L cartridge: FTP upload, REST file check and menu screen "
                                  "(or U2_FLASH_HOST)")
    p.add_argument("--c64-host", help="the C64 Ultimate the cartridge is plugged into; needed to drive the "
                                      "menu, watch the flash and power-cycle (or U2_FLASH_C64_HOST)")
    p.add_argument("--password", help="REST password of --host (or U2_FLASH_PASSWORD)")
    p.add_argument("--c64-password", help="REST password of --c64-host, if different (or U2_FLASH_C64_PASSWORD)")
    p.add_argument("--timeout", type=float, default=10.0, help="REST request timeout in seconds (default 10)")
    p.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                   help="wait up to SECONDS for the c64u device lock instead of refusing at once")

    p.add_argument("--artifact", help="local .u2l file (default: update.u2l or the newest update_*.u2l in the repository root)")
    p.add_argument("--repo-root", help="repository root for artifact discovery and the git defaults")
    p.add_argument("--dest-root", default=DEFAULT_DEST_ROOT,
                   help=f"destination directory on the cartridge (default {DEFAULT_DEST_ROOT}); list the "
                        "cartridge's FTP root to see which USB volume it has")
    p.add_argument("--branch", help="branch directory name (default: current git branch)")
    p.add_argument("--date", help="date directory name, YYMMDD (default: today)")
    p.add_argument("--git-id", help="id directory name (default: current short commit); must be new for every upload")
    p.add_argument("--remote-name", default=DEFAULT_REMOTE_NAME, help=f"remote file name (default {DEFAULT_REMOTE_NAME})")
    p.add_argument("--ftp-user", default="anonymous", help="FTP user (default anonymous)")
    p.add_argument("--ftp-pass", default="", help="FTP password (default empty)")

    p.add_argument("--dry-run", action="store_true", help="check the artifact and print the plan; touch no network")
    p.add_argument("--upload-only", action="store_true", help="upload and verify, then stop before any menu step")
    p.add_argument("--confirm-flash", action="store_true",
                   help=f"run '{TXT_RUN_UPDATE}' (without it the tool validates the menu path and backs out)")
    p.add_argument("--confirm-power-cycle", action="store_true",
                   help="power-cycle --c64-host after a confirmed-complete flash")
    p.add_argument("--reformat-flash-disk", action="store_true", help="answer 'Reformat Flash Disk?' with Yes (default No)")
    p.add_argument("--reset-config", action="store_true", help="answer 'Reset Configuration?' with Yes (default No)")
    p.add_argument("--update-wifi-module", action="store_true",
                   help="answer 'Want to update the WiFi Module?' with Yes (default No)")

    p.add_argument("--flash-timeout", type=float, default=240.0, help="seconds to wait for completion (default 240)")
    p.add_argument("--confirm-reads", type=int, default=3,
                   help="consecutive reads of the power-off text that confirm completion (default 3)")
    p.add_argument("--settle-seconds", type=float, default=5.0,
                   help="settle time after the completion text (default 5)")
    return p


def parse(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    env = os.environ.get
    args.host = args.host or env("U2_FLASH_HOST")
    args.c64_host = args.c64_host or env("U2_FLASH_C64_HOST")
    args.password = args.password or env("U2_FLASH_PASSWORD")
    args.c64_password = args.c64_password or env("U2_FLASH_C64_PASSWORD")
    if args.upload_only and args.confirm_flash:
        parser.error("--upload-only and --confirm-flash are mutually exclusive")
    if args.confirm_flash and not args.c64_host:
        parser.error("--confirm-flash needs --c64-host (or U2_FLASH_C64_HOST)")
    if args.confirm_power_cycle and not args.confirm_flash:
        parser.error("--confirm-power-cycle needs --confirm-flash")
    if not args.host and not args.dry_run:
        parser.error("--host (or U2_FLASH_HOST) is required")
    return args


def main(argv=None):
    args = parse(argv)
    # The holder line is world-readable, so it names the mode and the host, never the passwords.
    mode = "upload-only" if args.upload_only else "flash" if args.confirm_flash else "validate"
    cmd = f"u2_flash.py {mode} {args.host}"
    try:
        # A dry run touches no device, so it needs no lock.
        with contextlib.ExitStack() as stack:
            if not args.dry_run:
                stack.callback(device_lock.release,
                               device_lock.acquire(device_lock.lock_path("c64u"), args.wait, "c64u", cmd))
            return run(args)
    except device_lock.Busy as busy:
        log(f"The C64U is in use ({busy.holder or 'lock held'}); nothing was changed. "
            "Try again later, or pass --wait SECONDS.")
        return EXIT_ABORTED_SAFE
    except KeyboardInterrupt:
        log("\nInterrupted. If a flash was in progress, do NOT power-cycle anything; inspect the device by hand.")
        return EXIT_UNSAFE_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
