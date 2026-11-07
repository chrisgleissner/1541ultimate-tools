"""Per-device locks shared by the tools in this directory.

One physical device (the C64 Ultimate's FT232H adapter, the U64's USB-Blaster)
is used by one tool at a time. Each device has a lock file

    $DEVICE_LOCK_DIR/<device>.lock        (default /tmp/1541ultimate-device-locks)

that is held with flock(2) while the device is in use. The holder writes one
line into the file, for example

    pid=4711 device=c64u cmd=u64ii_jtag.py probe since=2026-11-07T10:00:00

so that a refused tool can say who holds the lock. The lock is released, and
the line cleared, when the holder exits.

A tool started by a lock holder does not wait for its parent. A lock counts as
held by an ancestor of the current process in two cases:

  * the holder line names the pid of an ancestor (tooling/with_device_locks.py
    and every tool in this directory write that line), or
  * the kernel lists the lock (/proc/locks) as owned by an ancestor. This
    covers `flock(1) FILE COMMAND`, which writes no holder line.

Other users of the lock file, such as a second tool or a different user's
session, are refused, or waited for when the caller passes a wait time.
"""

import contextlib
import datetime
import fcntl
import os
import re
import time

DEFAULT_DIR = "/tmp/1541ultimate-device-locks"
LOCK_DIR_ENV = "DEVICE_LOCK_DIR"


class Busy(Exception):
    """The lock is held by a process that is not an ancestor of this one."""

    def __init__(self, holder):
        super().__init__(holder or "held")
        self.holder = holder


def lock_dir():
    return os.environ.get(LOCK_DIR_ENV) or DEFAULT_DIR


def lock_path(device):
    return os.path.join(lock_dir(), device + ".lock")


def ancestors():
    """The pids of this process's parent, grandparent, and so on, up to init."""
    pids, pid = set(), os.getppid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as handle:
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def holder_line(device, cmd):
    since = datetime.datetime.now().isoformat(timespec="seconds")
    return f"pid={os.getpid()} device={device} cmd={cmd} since={since}\n"


def _holder_pids(text):
    return {int(pid) for pid in re.findall(r"(?<![A-Za-z0-9_])pid=(\d+)", text)}


def _kernel_owners(inode):
    """The pids that /proc/locks lists as holding a flock on this inode."""
    owners = set()
    try:
        with open("/proc/locks") as handle:
            for line in handle:
                fields = line.split()
                # "1: FLOCK ADVISORY WRITE <pid> <major>:<minor>:<inode> 0 EOF";
                # a waiter has "->" after the number
                if len(fields) >= 6 and fields[1] == "FLOCK" and fields[5].rsplit(":", 1)[-1] == str(inode):
                    owners.add(int(fields[4]))
    except (OSError, ValueError):
        pass
    return owners


def held_by_ancestor(handle, holder_text):
    """True when an ancestor of this process holds the lock that `handle` failed to take."""
    mine = ancestors()
    if mine & _holder_pids(holder_text):
        return True
    try:
        inode = os.fstat(handle.fileno()).st_ino
    except (OSError, ValueError):
        return False
    return bool(mine & _kernel_owners(inode))


def acquire(path, wait=0.0, device="", cmd="", on_wait=None):
    """Take the lock file at `path`; returns the open handle that holds it.

    Returns None when an ancestor of this process holds the lock (nothing is
    taken and nothing needs releasing). Raises Busy when someone else holds it
    for longer than `wait` seconds. `on_wait` is called once when the wait
    starts.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    deadline = time.monotonic() + wait
    announced = False
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip()
            if held_by_ancestor(handle, holder):
                handle.close()
                return None
            if time.monotonic() >= deadline:
                handle.close()
                raise Busy(holder)
            if not announced and wait and on_wait:
                on_wait()
                announced = True
            time.sleep(0.2)
    handle.seek(0)
    handle.truncate()
    handle.write(holder_line(device or os.path.basename(path)[:-5], cmd))
    handle.flush()
    return handle


def release(handle):
    """Clear the holder line and drop the lock."""
    if handle is not None:
        handle.seek(0)
        handle.truncate()
        handle.close()


@contextlib.contextmanager
def held(path, wait=0.0, device="", cmd="", on_wait=None):
    """`acquire` as a context manager; yields the handle, or None under an ancestor's lock."""
    handle = acquire(path, wait, device, cmd, on_wait)
    try:
        yield handle
    finally:
        release(handle)
