#!/usr/bin/env python3
"""Flash an Ultimate 64 Elite (MK1) without anyone at the keyboard.

Runs the U64 updater over JTAG and answers its popups by their text through
gdb/MI. The updater ends by switching the machine off; it comes back on only
through the power button, pressed by --power-button-cmd or by a person.

    with-device-locks u64 -- python3 tooling/flash_u64.py \\
        --host 192.168.1.13 --u64 update.u64 \\
        --sym-elf target/u64/nios2/updater/result/update.elf \\
        [--power-button-cmd "python3 tooling/switchbot_press.py --mac AA:BB:CC:DD:EE:FF --hold 0"] \\
        [--expect-commit d5686424f]

--sym-elf is the updater ELF from a build of the same commit. It supplies the
breakpoint addresses, and its code must match the .u64 image at each of them.
A CI package carries only the .u64, so build the same commit locally for it.

See docs/u64-unattended-flash.md.
"""

import argparse
import fcntl
import glob
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

OK, YES, NO = 1, 2, 4  # software/userinterface/ui_elements.h
NAMES = {OK: "OK", YES: "YES", NO: "NO"}

# Exact popup text, the buttons it offers, and the answer. Settings and the
# flash disk are kept. Any other popup is left unanswered and the run stops.
ANSWERS = [
    ("Reformat Flash Disk?", YES | NO, NO),
    ("About to update. Continue?", YES | NO, YES),
    ("Reset Configuration? (Recommended)", YES | NO, NO),
    ("Flashing ESP32 Success!", OK, OK),
    ("Flashing ESP32 Failed!", OK, OK),
    ("Could not set ESP32 to download mode", OK, OK),
]

SYMBOLS = {
    "popup": "UserInterface::popup(char const*, unsigned char)",
    "flash_buffer_at": "flash_buffer_at(Flash*, Screen*, int, bool, void*, void*, char const*, char const*)",
    "update_esp32": "update_esp32()",
    "turn_off": "turn_off()",
}

ITU_IRQ_GLOBAL = 0x04000000   # software/system/itu.h, IOBASE 0x04000000
FPGA_ID = "5CE"               # jtagconfig names the Cyclone V as 5CE(BA4|FA4)
LOCK_PATH = os.path.join(os.environ.get("DEVICE_LOCK_DIR", "/tmp/1541ultimate-device-locks"),
                         "u64.lock")
T0 = time.monotonic()


class FlashError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def log(message):
    print(f"[flash-u64 {time.monotonic() - T0:6.1f}s] {message}", flush=True)


# ---------------------------------------------------------------- toolchain

def intel_env():
    root = os.environ.get("INTEL_FPGA_ROOT")
    if not root and os.environ.get("QUARTUS_ROOTDIR"):
        root = os.path.dirname(os.path.abspath(os.environ["QUARTUS_ROOTDIR"]))
    if not root:
        home = os.path.expanduser("~")
        for pattern in (f"{home}/intelFPGA_lite/*", f"{home}/intelFPGA/*", f"{home}/altera_lite/*",
                        "/opt/intelFPGA_lite/*", "/opt/intelFPGA/*", "/opt/altera_lite/*"):
            hits = [d for d in sorted(glob.glob(pattern))
                    if os.access(f"{d}/quartus/bin/jtagconfig", os.X_OK)]
            if hits:
                root = hits[0]
                break
    if not root:
        raise FlashError("could not locate the Intel FPGA tools; set INTEL_FPGA_ROOT")
    env = dict(os.environ)
    env["QUARTUS_ROOTDIR"] = f"{root}/quartus"
    env["PATH"] = ":".join([f"{root}/quartus/bin", f"{root}/nios2eds/bin",
                            f"{root}/nios2eds/bin/gnu/H-x86_64-pc-linux-gnu/bin", env["PATH"]])
    return env


ENV = None


def run(cmd):
    return subprocess.run(cmd, env=ENV, capture_output=True, text=True)


# ---------------------------------------------------------------- image

def parse_u64(raw):
    """A .u64 file: load address, length and entry (little endian), then the image."""
    if len(raw) < 12:
        raise FlashError("file too short for a .u64 header")
    load, length, entry = struct.unpack("<III", raw[:12])
    image = raw[12:]
    if len(image) != length:
        raise FlashError(f"header length {length:#x} does not match the image ({len(image):#x})")
    if not load <= entry < load + length:
        raise FlashError(f"entry {entry:#x} lies outside the image")
    return load, entry, image


def wrap_elf(image, load, entry, workdir):
    """The image unchanged, as an executable ELF with one LOAD segment."""
    raw = os.path.join(workdir, "image.bin")
    obj = os.path.join(workdir, "image.o")
    script = os.path.join(workdir, "image.ld")
    elf = os.path.join(workdir, "image.elf")
    with open(raw, "wb") as handle:
        handle.write(image)
    with open(script, "w") as handle:
        handle.write(f"ENTRY(_start)\n_start = {entry:#x};\n"
                     f"SECTIONS {{ . = {load:#x}; .text : {{ *(.data) }} }}\n")
    for cmd in (["nios2-elf-objcopy", "-I", "binary", "-O", "elf32-littlenios2", "-B", "nios2", raw, obj],
                ["nios2-elf-ld", "-T", script, "-o", elf, obj]):
        result = run(cmd)
        if result.returncode:
            raise FlashError(f"{cmd[0]}: {result.stderr.strip()}")
    if elf_image(elf) != image:
        raise FlashError("the wrapped ELF does not reproduce the image")
    return elf


def elf_image(elf):
    with tempfile.NamedTemporaryFile(suffix=".bin") as out:
        result = run(["nios2-elf-objcopy", "-O", "binary", elf, out.name])
        if result.returncode:
            raise FlashError(f"nios2-elf-objcopy {elf}: {result.stderr.strip()}")
        with open(out.name, "rb") as handle:
            return handle.read()


def find_symbols(nm_output):
    """Address and size of each breakpoint function, from `nm -C -S`."""
    symbols = {}
    for key, name in SYMBOLS.items():
        lines = [l for l in nm_output.splitlines() if l.endswith(" " + name)]
        if len(lines) != 1:
            raise FlashError(f"symbol {name!r} found {len(lines)} times, expected once")
        fields = lines[0].split()
        symbols[key] = (int(fields[0], 16), int(fields[1], 16))
    return symbols


# Nios II opcodes whose immediates hold absolute addresses: call, jmpi, and
# the movhi/addi/ori pairs that build data pointers. Two builds of one commit
# place data differently, so these may differ while the code is the same.
ADDRESS_OPCODES = {0x00, 0x01, 0x04, 0x14, 0x34}


def code_matches(image, reference, at, size, threshold=0.9):
    words = size // 4
    same = exact = 0
    for i in range(words):
        a = struct.unpack_from("<I", image, at + 4 * i)[0]
        b = struct.unpack_from("<I", reference, at + 4 * i)[0]
        if a == b:
            same += 1
            exact += 1
        elif a & 0x3f == b & 0x3f and a & 0x3f in ADDRESS_OPCODES:
            same += 1
    first_two = image[at:at + 8] == reference[at:at + 8]
    return first_two and words > 0 and same >= threshold * words


def check_code(image, reference, load, symbols):
    """The breakpoints come from the symbol ELF, so its code must be the image's."""
    for key, (addr, size) in symbols.items():
        at = addr - load
        if at < 0 or at + size > len(image) or at + size > len(reference):
            raise FlashError(f"{key} at {addr:#x} lies outside the image")
        if not code_matches(image, reference, at, size):
            raise FlashError(f"{key} at {addr:#x}: the code differs from the symbol ELF; "
                             "build the same commit as the .u64")


def answer_for(message, flags):
    for text, offered, answer in ANSWERS:
        if text == message and offered == flags:
            return answer
    return None


# ---------------------------------------------------------------- gdb/MI

def mi_value(result_record):
    """MI renders registers as 0x..., pointers as "(type) 0x... <symbol>"."""
    value = re.search(r'value="([^"]*)"', result_record)
    # The symbol may be a template, so <...> can nest.
    number = value and re.search(r'(-?0x[0-9a-fA-F]+|-?\d+)\s*(?:<.*>)?\s*$', value.group(1))
    if not number:
        raise FlashError(f"gdb returned no number: {result_record}")
    return int(number.group(1), 0) & 0xffffffff


class Gdb:
    """nios2-elf-gdb 18.1 has no Python, so it is driven over MI from here."""

    def __init__(self):
        self.proc = subprocess.Popen(["nios2-elf-gdb", "--interpreter=mi2", "-q"], env=ENV,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.pending = b""
        try:
            self.read_until(lambda line: line.startswith("(gdb)"), 30)
        except FlashError:
            self.close()
            raise

    def read_until(self, done, timeout):
        # select() rather than readline(), which would block past the deadline
        # while gdb is silent.
        deadline = time.monotonic() + timeout
        fd = self.proc.stdout.fileno()
        lines = []
        while True:
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    raise FlashError(f"gdb timed out: {lines[-3:]}")
                chunk = os.read(fd, 4096)
                if not chunk:
                    raise FlashError("gdb exited")
                self.pending += chunk
            line, self.pending = self.pending.split(b"\n", 1)
            lines.append(line.decode("latin-1").rstrip("\r"))
            if done(lines[-1]):
                return lines

    def send(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def cmd(self, command, timeout=60):
        self.send(command)
        record = self.read_until(lambda line: line.startswith("^"), timeout)[-1]
        if record.startswith("^error"):
            raise FlashError(f"gdb {command}: {record}")
        return record

    def value(self, expression):
        return mi_value(self.cmd(f'-data-evaluate-expression "{expression}"'))

    def console(self, command):
        return self.cmd(f'-interpreter-exec console "{command}"')

    def string(self, addr, length=96):
        record = self.cmd(f"-data-read-memory-bytes {addr} {length}")
        raw = bytes.fromhex(re.search(r'contents="([0-9a-f]+)"', record).group(1))
        return raw.split(b"\0")[0].decode("latin-1")

    def wait_stop(self, timeout):
        return self.read_until(lambda line: line.startswith("*stopped"), timeout)[-1]

    def close(self):
        """End of input stops gdb the same way as this script exiting would."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        reap(self.proc)
        self.proc.stdout.close()


def reap(proc):
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def answer_popups(gdb, addrs, stop):
    """Runs the updater to turn_off(), answering each popup from ANSWERS."""
    answered = []
    while True:
        if stop is None:
            stop = gdb.wait_stop(1800)
        pc = gdb.value("$pc")
        if pc == addrs["popup"]:
            message = gdb.string(gdb.value("$r5"))
            flags = gdb.value("$r6") & 0xff
            answer = answer_for(message, flags)
            if answer is None:
                # The CPU stays halted at the entry of popup(); --resume
                # carries on once the table covers this popup.
                raise FlashError(f"unexpected popup {message!r} (buttons {flags:#x}); not answered", 2)
            log(f"popup {message!r} -> {NAMES[answer]}")
            answered.append((message, NAMES[answer]))
            # Return from popup() at its entry, before it draws anything.
            return_address = gdb.value("$ra")
            gdb.console(f"set $r2 = {answer}")
            gdb.console(f"set $pc = {return_address:#x}")
        elif pc == addrs["flash_buffer_at"]:
            log(f"writing flash at {gdb.value('$r6'):#x}")
        elif pc == addrs["update_esp32"]:
            log("checking the WiFi module")
        elif pc == addrs["turn_off"]:
            log("updater finished; it switches the machine off in 5 s")
            gdb.cmd("-break-delete")
            gdb.send("-exec-continue")
            return answered
        else:
            raise FlashError(f"unexpected stop at {pc:#010x}: {stop}", 3)
        stop = None
        gdb.cmd("-exec-continue")


# ---------------------------------------------------------------- device

class DeviceLock:
    """The u64 flock that `with-device-locks u64 -- ...` takes. A lock held by
    an ancestor (with-device-locks writes its pid into the file) covers us."""

    def __enter__(self):
        if os.environ.get("FLASH_U64_LOCK") == "off":
            self.handle = None
            return self
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        self.handle = open(LOCK_PATH, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.seek(0)
            holder = self.handle.read()
            self.handle.close()
            self.handle = None
            if not any(f"pid={pid} " in holder for pid in ancestors()):
                raise FlashError(f"the u64 is in use ({LOCK_PATH}: {holder.strip() or 'held'})")
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()


def ancestors():
    pids, pid = set(), os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as handle:
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def rest_info(host):
    try:
        with urllib.request.urlopen(f"http://{host}/v1/info", timeout=2) as response:
            return json.loads(response.read())
    except Exception:
        return None


def pingable(host):
    return subprocess.call(["ping", "-c1", "-W1", host], stdout=subprocess.DEVNULL) == 0


def fpga_on_chain():
    return FPGA_ID in run(["jtagconfig"]).stdout


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
        return True
    except OSError:
        return False


def start_gdb_server(port):
    # A server already on the port would take gdb's connection instead of ours.
    if port_open(port):
        raise FlashError(f"port {port} is already in use; stop the nios2-gdb-server "
                         "left by an earlier run, or use --resume")
    server = subprocess.Popen(["nios2-gdb-server", "--tcpport", str(port), "--tcppersist"], env=ENV,
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        if port_open(port):
            return server
        if server.poll() is not None:
            raise FlashError(f"nios2-gdb-server exited with {server.returncode}")
        time.sleep(0.5)
    server.kill()
    server.wait()
    raise FlashError("nios2-gdb-server did not start")


def wait_for_rest(host, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = rest_info(host)
        if info:
            return info
        time.sleep(2)
    return None


def power_on(args):
    attempts = 2 if args.power_button_cmd else 1
    for attempt in range(1, attempts + 1):
        if args.power_button_cmd:
            # A press on a running machine opens the menu or resets the C64.
            if attempt > 1 and (pingable(args.host) or fpga_on_chain()):
                raise FlashError("the machine is on but /v1/info does not answer")
            log(f"pressing the power button ({attempt}/{attempts})")
            result = subprocess.run(args.power_button_cmd, shell=True)
            if result.returncode:
                log(f"power button command exited {result.returncode}")
            info = wait_for_rest(args.host, args.up_timeout)
        else:
            log("press the U64 power button now")
            info = wait_for_rest(args.host, 3600)
        if info:
            return info
    raise FlashError("the machine did not come back on")


def flash(args):
    with open(args.u64, "rb") as handle:
        load, entry, image = parse_u64(handle.read())
    log(f"{args.u64}: load {load:#x}, entry {entry:#x}, {len(image)} bytes")
    result = run(["nios2-elf-nm", "-C", "-S", args.sym_elf])
    if result.returncode:
        raise FlashError(f"nios2-elf-nm {args.sym_elf}: {result.stderr.strip()}")
    symbols = find_symbols(result.stdout)
    check_code(image, elf_image(args.sym_elf), load, symbols)
    addrs = {key: addr for key, (addr, _) in symbols.items()}
    log("breakpoints " + ", ".join(f"{k} {v:#x}" for k, v in addrs.items()))
    before = rest_info(args.host)
    if before:
        log(f"before: {before.get('firmware_version')} {before.get('git_commit_hash')} "
            f"FPGA {before.get('fpga_version')}")

    server = gdb = None
    keep_server = False
    if not args.resume:
        if not fpga_on_chain():
            raise FlashError("no U64 FPGA on the JTAG chain; is the machine on?")
        with tempfile.TemporaryDirectory(prefix="flash_u64_") as workdir:
            elf = wrap_elf(image, load, entry, workdir)
            log("loading the updater over JTAG (about 160 s)")
            result = run(["nios2-download", elf])
        if result.returncode or "Verified OK" not in result.stdout:
            raise FlashError("nios2-download: " + (result.stdout + result.stderr)[-400:])
        server = start_gdb_server(args.port)

    try:
        gdb = Gdb()
        gdb.cmd("-gdb-set confirm off")
        gdb.cmd("-gdb-set remotetimeout 30")
        gdb.cmd(f"-target-select remote :{args.port}", 60)
        if args.resume:
            if gdb.value("$pc") != addrs["popup"]:
                raise FlashError("--resume: the CPU is not halted at popup()", 4)
            stop = "resume"
        else:
            # What jump_run() in filetype_u2p.cc does before entering an update image.
            gdb.console("set $status = 0")
            gdb.console("set $ienable = 0")
            gdb.console(f"set *(unsigned char *){ITU_IRQ_GLOBAL:#x} = 0")
            gdb.console(f"set $pc = {entry:#x}")
            stop = None
        for addr in addrs.values():
            gdb.cmd(f"-break-insert *{addr:#x}")
        if stop is None:
            gdb.cmd("-exec-continue")
        answered = answer_popups(gdb, addrs, stop)
        # Stay attached until the machine is off: the gdb-server cannot detach,
        # and quitting gdb early could halt the CPU inside turn_off().
        time.sleep(25)
        gdb.send("-gdb-exit")
    except FlashError as error:
        # An unanswered popup leaves the CPU halted in popup() for --resume.
        keep_server = error.code == 2
        raise
    finally:
        if gdb:
            gdb.close()
        if server and keep_server:
            log(f"nios2-gdb-server (pid {server.pid}) is left running for --resume")
        elif server:
            server.terminate()
            reap(server)

    time.sleep(5)
    if pingable(args.host) or fpga_on_chain():
        raise FlashError("the machine did not switch itself off")
    log("machine is off")

    info = power_on(args)
    log(f"after: {info.get('firmware_version')} {info.get('git_commit_hash')} "
        f"FPGA {info.get('fpga_version')} errors {info.get('errors')}")
    log(f"answers: {answered}")
    # Either hash may be abbreviated, and `git rev-parse --short` varies in length.
    reported = info.get("git_commit_hash") or ""
    if args.expect_commit and not (reported and (reported.startswith(args.expect_commit)
                                                 or args.expect_commit.startswith(reported))):
        raise FlashError(f"expected commit {args.expect_commit}, the device reports "
                         f"{info.get('git_commit_hash')}")
    log("done")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--u64", required=True, help="update .u64 file")
    parser.add_argument("--sym-elf", required=True, help="updater ELF built from the same commit")
    parser.add_argument("--host", default=os.environ.get("U64_HOST"), required="U64_HOST" not in os.environ,
                        help="the U64's address (or U64_HOST)")
    parser.add_argument("--port", type=int, default=2342)
    parser.add_argument("--power-button-cmd", default=os.environ.get("U64_POWER_BUTTON_CMD", ""),
                        help="presses the power button once; empty means a person does")
    parser.add_argument("--up-timeout", type=float, default=120.0)
    parser.add_argument("--expect-commit", default="", help="git_commit_hash expected afterwards")
    parser.add_argument("--resume", action="store_true",
                        help="attach to a running nios2-gdb-server with the CPU halted at popup()")
    args = parser.parse_args(argv)
    global ENV
    try:
        ENV = intel_env()
        with DeviceLock():
            flash(args)
    except FlashError as error:
        log(f"FAILED: {error}")
        return error.code
    except OSError as error:
        log(f"FAILED: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
