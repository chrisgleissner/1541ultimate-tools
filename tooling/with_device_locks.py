#!/usr/bin/env python3
"""Run a command while holding one or more device locks.

    with_device_locks.py [--wait SECONDS] DEVICE [DEVICE ...] -- COMMAND [ARGS ...]

    python3 tooling/with_device_locks.py c64u -- tooling/u64ii_jtag.sh probe
    python3 tooling/with_device_locks.py u64 c64u -- ./build-tool --jtag c64u

Each DEVICE names a lock file $DEVICE_LOCK_DIR/DEVICE.lock (default directory
/tmp/1541ultimate-device-locks; the tools use the names c64u and u64). The
locks are taken in sorted order, so two wrappers that ask for the same devices
cannot deadlock each other. The wrapper writes a holder line (pid=<its pid>
...) into each lock file, runs COMMAND, releases the locks, and exits with the
exit status of COMMAND (128+N when COMMAND died of signal N).

The tools in this directory that use a device (u64ii_jtag.py, flash_u64.py,
c64u_mode.py, jtag_verify.py) recognise a lock held by one of their ancestor
processes and run without waiting for it. Use this wrapper to hold a device
across several tool invocations.

Plain `flock FILE COMMAND` also works on Linux: the tools find an ancestor's
flock(1) lock in /proc/locks although it carries no holder line. The wrapper
is still preferred, because its holder line tells a refused user who holds the
device, and it does not depend on /proc/locks.

Exit status: 75 when a lock is held by someone else (after --wait seconds,
default 0 = refuse at once), 2 for a usage error, 127 when COMMAND does not
exist, 126 when it cannot be executed.
"""

import argparse
import os
import re
import signal
import subprocess
import sys

import device_lock

BUSY = 75
DEVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def parse(argv):
    parser = argparse.ArgumentParser(prog="with_device_locks.py",
                                     usage="%(prog)s [--wait SECONDS] DEVICE [DEVICE ...] -- COMMAND [ARGS ...]",
                                     description="Run a command while holding device locks.")
    parser.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                        help="wait up to SECONDS for a lock held by someone else (default: refuse at once)")
    parser.add_argument("devices", nargs="+", metavar="DEVICE", help="device name, for example c64u or u64")
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if split == len(argv):
        parser.error("missing -- before COMMAND")
    if not command:
        parser.error("no COMMAND after --")
    for name in args.devices:
        if not DEVICE_NAME.match(name):
            parser.error(f"invalid device name {name!r}")
    return sorted(set(args.devices)), args.wait, command


def run(argv):
    devices, wait, command = parse(argv)
    cmd = " ".join(command)[:200]
    handles = []
    try:
        for device in devices:
            path = device_lock.lock_path(device)
            try:
                handles.append(device_lock.acquire(path, wait, device, cmd))
            except device_lock.Busy as busy:
                print(f"with_device_locks: {device} is in use ({path}: {busy.holder or 'held'})",
                      file=sys.stderr)
                return BUSY
        env = dict(os.environ, **{device_lock.LOCK_DIR_ENV: device_lock.lock_dir()})
        try:
            child = subprocess.Popen(command, env=env)
        except FileNotFoundError:
            print(f"with_device_locks: {command[0]}: command not found", file=sys.stderr)
            return 127
        except OSError as exc:
            print(f"with_device_locks: {command[0]}: {exc.strerror}", file=sys.stderr)
            return 126
        previous = {sig: signal.signal(sig, lambda number, _frame: child.send_signal(number))
                    for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            status = child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 128 - status if status < 0 else status
    finally:
        for handle in reversed(handles):
            device_lock.release(handle)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
