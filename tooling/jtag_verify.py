#!/usr/bin/env python3
"""Bounded, non-disruptive JTAG transport verification; see docs/jtag-verify.md."""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time

import u64ii_jtag as jt

VERSION = 1
# Only these single-device, normally configured boards have reviewed semantics.
PROFILES = {
    "u64": (10, 0x006, (0x02B050DD,)),
    "c64u": (6, jt.IR_IDCODE, tuple(jt.SUPPORTED)),
}
SUPPORTED = {(0x09FB, 0x6001): "blaster", (0x0403, 0x6014): "ft232h"}
LENGTHS = (1, 2, 7, 8, 9, 15, 16, 31, 32, 63, 64, 65, 503, 504, 505,
           511, 512, 513, 4095, 4096, 4097, 65535, 65536, 65537)


class PolicyError(jt.JtagError):
    pass


@dataclass
class Adapter:
    identifier: str
    kind: str
    vid: int
    pid: int
    bus: int
    address: int
    ports: list
    manufacturer: str | None
    product: str | None
    serial: str | None
    speed: str
    descriptor_errors: list

    @property
    def url(self):
        # Never select by serial: clones can share it. Numeric bus/address is
        # PyFtdi's unambiguous location selector, interpreted as hexadecimal.
        return f"ftdi://0x{self.vid:x}:0x{self.pid:x}:{self.bus:x}:{self.address:x}/1"


def discover(extra=None):
    import usb.core
    import usb.util
    devices = []
    supported = dict(SUPPORTED)
    supported.update(extra or {})
    for dev in usb.core.find(find_all=True):
        kind = supported.get((dev.idVendor, dev.idProduct))
        if kind is None:
            continue
        errors = []
        def string(index):
            try:
                return usb.util.get_string(dev, index) if index else None
            except Exception as exc:
                errors.append(str(exc))
                return None
        ports = list(dev.port_numbers or ())
        location = f"{dev.bus}-" + (".".join(map(str, ports)) if ports else f"addr{dev.address}")
        devices.append(Adapter(
            f"{dev.idVendor:04x}:{dev.idProduct:04x}@{location}", kind,
            dev.idVendor, dev.idProduct, dev.bus, dev.address, ports,
            string(dev.iManufacturer), string(dev.iProduct), string(dev.iSerialNumber),
            {1: "low", 2: "full", 3: "high", 4: "super", 5: "super+"}.get(
                dev.speed, "unknown"), errors))
    return sorted(devices, key=lambda a: (a.bus, a.ports, a.address))


class SafeTap:
    """Default-deny scan boundary. No USER, configuration or debug instructions.

    Tests receive this interface, never a Board, UserChain or raw cable. The
    transport and unrestricted scan implementation stay private.
    """
    def __init__(self, cable, profile):
        if profile not in PROFILES:
            raise PolicyError("unreviewed target profile")
        self.__tap = jt.Tap(cable)
        self.instruction = None
        self.width, self.id_instruction, self.ids = PROFILES[profile]
        self.confirmed = False
        self.healthy = True

    def reset(self):
        if not self.healthy:
            raise PolicyError("transport state is uncertain; no more clocks permitted")
        try:
            self.__tap.reset()  # TAP reset only; neither CPU nor FPGA configuration reset
            self.instruction = self.id_instruction
        except BaseException:
            self.healthy = False
            raise

    def ir(self, instruction, read=False):
        if not self.healthy or not self.confirmed:
            raise PolicyError("identity/chain not confirmed or transport uncertain")
        if instruction not in (self.id_instruction, (1 << self.width) - 1):
            raise PolicyError("instruction is prohibited")
        try:
            self.__tap.m.tms(0b0011, 4)
            captured = self.__tap._shift(instruction, self.width, read)
            if read and captured & 3 != 1:
                raise jt.JtagError(f"IR capture low bits are {captured & 3:02b}, expected 01")
            self.instruction = instruction
            return captured
        except BaseException:
            self.healthy = False
            raise

    def dr(self, tdi, nbits, read=False):
        if not self.healthy or self.instruction not in (
                self.id_instruction, (1 << self.width) - 1):
            raise PolicyError("DR instruction is not permitted")
        if type(nbits) is not int or not 1 <= nbits <= 65537:
            raise PolicyError("scan length outside reviewed bounds")
        if type(tdi) is not int or not 0 <= tdi < (1 << nbits) or read is not True:
            raise PolicyError("only bounded readback scans are permitted")
        if self.instruction == self.id_instruction and nbits not in (32, 96):
            raise PolicyError("invalid IDCODE scan")
        if not self.confirmed and (nbits != 96 or tdi != (1 << 96) - 1):
            raise PolicyError("only the identity/chain check is permitted initially")
        try:
            return self.__tap.dr(tdi, nbits, True)
        except BaseException:
            self.healthy = False
            raise

    def identify(self):
        self.reset()
        value = self.dr((1 << 96) - 1, 96, True)
        code = value & 0xFFFFFFFF
        if code & 0x0FFFFFFF not in self.ids or value >> 32 != (1 << 64) - 1:
            self.healthy = False
            raise PolicyError(f"unexpected target or multi-device chain: 0x{value:024x}")
        self.confirmed = True
        return code

    def idle(self, clocks):
        if not self.healthy or not self.confirmed or type(clocks) is not int or not 0 <= clocks <= 64:
            raise PolicyError("invalid idle clock request")
        try:
            self.__tap.idle(clocks)
        except BaseException:
            self.healthy = False
            raise

    def dr_bitstream(self, data):
        raise PolicyError("configuration is prohibited")

    def idcode(self):
        return self.identify()


@contextlib.contextmanager
def target_lock(profile, adapter):
    # Share the deployed tool's target lock without changing its module global.
    # DeviceLock handles an ancestor with-device-locks owner on c64u.
    if profile == "c64u":
        if os.environ.get("U64II_JTAG_LOCK") == "off":
            raise PolicyError("verifier refuses disabled device locking")
        with jt.DeviceLock(wait=0):
            yield
        return
    directory = Path(os.environ.get("DEVICE_LOCK_DIR", "/tmp/1541ultimate-device-locks"))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{profile}.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PolicyError(f"{profile} device lock is busy") from exc
        yield


@contextlib.contextmanager
def safe_cable(adapter, frequency):
    from pyftdi.ftdi import Ftdi
    # Blaster.open's recovery padding assumes a potentially unfinished shift
    # command. On a live board that recovery is not proven safe. Require a
    # quiescent command boundary instead, and do not send that padding here.
    try:
        Ftdi.add_custom_vendor(adapter.vid, f"verify_{adapter.vid:x}")
    except ValueError:
        pass
    try:
        Ftdi.add_custom_product(adapter.vid, adapter.pid, f"verify_{adapter.pid:x}")
    except ValueError:
        pass
    if adapter.kind == "ft232h":
        cable = jt.Mpsse.open(adapter.url, frequency)
    else:
        ftdi = Ftdi()
        try:
            ftdi.open_from_url(adapter.url)
            ftdi.set_latency_timer(2)
            ftdi.purge_buffers()
            cable = jt.Blaster(ftdi)
        except BaseException:
            ftdi.close()
            raise
    completed = False
    try:
        # Bound individual libusb transfers as well as the scan count.
        cable.ftdi.usb_read_timeout = 250
        cable.ftdi.usb_write_timeout = 1000
        yield cable
        completed = True
    finally:
        if completed:
            if cable.release() is False:
                raise RuntimeError("adapter output-disable failed during cleanup")
        else:
            # A partial command may interpret even output-disable as shift data.
            # Close without further writes, retaining pin levels; no recovery.
            cable.ftdi.close(freeze=True)


def mpsse_sync(cable):
    """AN_108 bad-command echo checks the parser without emitting JTAG clocks."""
    if cable.ftdi.write_data(b"\xaa\x87") != 2:
        raise jt.JtagError("short MPSSE synchronization write")
    response = bytes(cable.ftdi.read_data_bytes(2, attempt=20))
    if response != b"\xfa\xaa":
        raise jt.JtagError(f"MPSSE command echo mismatch: {response.hex()}")


def result(req, status, detail, **metrics):
    return dict(requirement=req, status=status, safety="SAFE", detail=detail, **metrics)


def exercise(tap, cycles, seed, deadline):
    """Sustained, exactly reproducible traffic with a one-bit BYPASS oracle."""
    rng = random.Random(seed)
    code = tap.identify()
    scans = bits = 0
    latencies = []
    short_latencies = []
    digest = hashlib.sha256()
    started = time.monotonic()
    for cycle in range(cycles):
        for length in LENGTHS:
            if time.monotonic() >= deadline:
                raise jt.JtagError(f"suite deadline reached after {scans} scans")
            # Repeated mode switching exercises interactive short transactions
            # between bulk transfers, rather than only a long streaming scan.
            tap.ir((1 << tap.width) - 1, True)
            for pattern in (0, (1 << length) - 1, rng.getrandbits(length)):
                before = time.monotonic()
                got = tap.dr(pattern, length, True)
                latency = time.monotonic() - before
                latencies.append(latency)
                if length <= 64:
                    short_latencies.append(latency)
                expected = (pattern << 1) & ((1 << length) - 1)
                if got != expected:
                    raise jt.JtagError(f"BYPASS mismatch cycle={cycle} bits={length} "
                                       f"scan={scans} differing_bits={(got ^ expected).bit_count()}")
                digest.update(got.to_bytes((length + 7) // 8, "little"))
                scans += 1
                bits += length
            tap.idle(cycle % 9)
            tap.ir(tap.id_instruction)
            if tap.dr(0xFFFFFFFF, 32, True) != code:
                raise jt.JtagError(f"IDCODE changed cycle={cycle} after bits={length}")
    elapsed = time.monotonic() - started
    return result("JTAG-SCAN", "PASS", "IR capture, IDCODE and one-bit BYPASS agree",
                  covers=["TAP-ID", "TAP-IR", "TAP-BYPASS", "SHIFT-BITS", "SHIFT-BYTES", "SHIFT-TMS"],
                  idcode=f"0x{code:08x}", bypass_scans=scans, compared_bits=bits, observed_bit_errors=0,
                  elapsed_s=elapsed, bits_per_s=bits / max(elapsed, 1e-9),
                  latency_ms=dict(median=statistics.median(latencies) * 1000,
                                  maximum=max(latencies) * 1000,
                                  p95=sorted(latencies)[int((len(latencies) - 1) * .95)] * 1000),
                  short_scan_latency_ms=dict(median=statistics.median(short_latencies) * 1000,
                                             maximum=max(short_latencies) * 1000),
                  readback_sha256=digest.hexdigest())


def verify(adapter, profile, args):
    report = dict(adapter=asdict(adapter), target=profile, backend="PyFtdi/libusb",
                  capabilities_basis="inferred from declared compatible type, not proof of authenticity",
                  capabilities=(["fixed cable clock", "pin/byte shifts up to 63 bytes"]
                                if adapter.kind == "blaster" else
                                ["MPSSE", "programmable clock", "bit/byte/TMS shifts"]),
                  results=[result("USB-ID", "PASS", "USB descriptors read; authenticity not established")])
    checks = report["results"]
    if adapter.descriptor_errors:
        checks[0] = result("USB-ID", "UNVERIFIED", "; ".join(adapter.descriptor_errors))
    if profile is None or args.list:
        checks.append(result("JTAG-SCAN", "UNTESTABLE", "no verified wiring profile; USB was not opened"))
    else:
        stage = "ADAPTER-OPEN"
        try:
            with target_lock(profile, adapter), safe_cable(adapter, args.frequency) as cable:
                if adapter.kind == "ft232h":
                    stage = "FT-SYNC"
                    mpsse_sync(cable)
                    checks.append(result("FT-SYNC", "PASS", "documented bad-command echo; no target clocks"))
                stage = "JTAG-SCAN"
                tap = SafeTap(cable, profile)
                deadline = time.monotonic() + args.timeout
                try:
                    frequencies = tuple(f for f in (100000, 1000000, args.frequency)
                                        if f <= args.frequency) if adapter.kind == "ft232h" else (None,)
                    for frequency in dict.fromkeys(frequencies):
                        stage = "FT-CLOCK" if frequency else "JTAG-SCAN"
                        try:
                            actual = cable.ftdi.set_frequency(frequency) if frequency else None
                        except BaseException:
                            tap.healthy = False
                            raise
                        stage = "JTAG-SCAN"
                        check = exercise(tap, args.cycles, args.seed, deadline)
                        check.update(requested_hz=frequency, configured_hz=actual,
                                     physical_clock="UNVERIFIED: no instrument measurement")
                        checks.append(check)
                        if frequency:
                            checks.append(result("FT-CLOCK", "PASS",
                                                 "driver clock configuration accepted; scans checked at this setting",
                                                 requested_hz=frequency, configured_hz=actual))
                    checks.append(result("TRANSPORT-SOAK", "PASS", "all scheduled scans completed without retries"))
                except BaseException:
                    # A semantic mismatch also makes the target/transport state
                    # uncertain; do not clock cleanup through an untrusted path.
                    tap.healthy = False
                    raise
                finally:
                    if tap.healthy:
                        # Only reset the TAP, selecting inert IDCODE, never the CPU.
                        tap.reset()
        except Exception as exc:
            status = ("UNTESTABLE" if isinstance(exc, PolicyError) else
                      "FAIL" if isinstance(exc, jt.JtagError) and stage == "JTAG-SCAN" else "ERROR")
            checks.append(result(stage, status, f"{type(exc).__name__}: {exc}"))
    debug = dict(requirement="DEBUG-SESSION", status="UNVERIFIED", safety="DISRUPTIVE",
                 detail="requires opt-in, confirmed idle target and Intel tool/health configuration")
    if profile == "c64u":
        debug.update(status="UNSUPPORTED", detail="c64u permits SAFE tests only; reviewed tooling provides no CPU halt/step support")
    elif args.allow_disruptive and profile == "u64" and not args.list:
        if not args.debug_idle or args.intel_root is None or args.debug_health_host is None:
            debug.update(status="UNTESTABLE", detail="missing idle assertion, Intel tool root or matching health host")
        elif adapter.kind != "blaster" or (adapter.vid, adapter.pid) != (0x09FB, 0x6001):
            debug.update(status="UNSUPPORTED", detail="Intel debug backend requires a 09fb:6001 USB-Blaster")
        elif (not any(c["requirement"] == "TRANSPORT-SOAK" for c in checks)
              or any(c["status"] in ("FAIL", "ERROR") for c in checks)):
            debug.update(status="UNTESTABLE", detail="transport suite did not pass; debug attach prohibited")
        else:
            import jtag_verify_debug
            try:
                with target_lock(profile, adapter):
                    debug = jtag_verify_debug.run(adapter, args.intel_root, args.debug_health_host,
                                                  args.debug_cycles, args.debug_steps)
            except Exception as exc:
                debug.update(status="ERROR", detail=f"{type(exc).__name__}: {exc}")
                if isinstance(exc, jtag_verify_debug.DebugRunError):
                    debug.update(exc.evidence)
    checks.extend([debug, result("ELECTRICAL", "UNTESTABLE",
                                "voltage, edge timing and signal integrity require external instruments")])
    report["debugging_suitability"] = (
        "DEBUG_SESSION_FAILED" if debug["status"] == "ERROR" else
        "DEBUG_SESSION_PASSED" if debug["status"] == "PASS" else
        "TRANSPORT_FAILED" if any(c["status"] == "FAIL" for c in checks) else
        "TRANSPORT_UNTESTABLE" if any(c["status"] == "ERROR" for c in checks) else
        "TRANSPORT_PASSED_DEBUG_UNVERIFIED" if any(c["requirement"] == "TRANSPORT-SOAK" for c in checks) else
        "UNVERIFIED")
    return report


def parser():
    p = argparse.ArgumentParser(description=__doc__, epilog=(
        "--target asserts a powered, normally configured, single-device board, correct wiring, "
        "a quiescent adapter at a complete command boundary, and a TAP known to be in Idle or Test-Logic-Reset. Never run concurrently with Quartus/debuggers."))
    p.add_argument("--adapter", action="append", default=[], help="exact stable identifier (repeatable)")
    p.add_argument("--type", choices=("blaster", "ft232h"))
    p.add_argument("--target", action="append", default=[], metavar="ID=PROFILE", help="PROFILE is u64 or c64u; repeatable")
    p.add_argument("--usb-id", action="append", default=[], metavar="VID:PID=TYPE", help="explicit compatible third-party USB identity")
    p.add_argument("--list", action="store_true", help="enumerate only; no adapter open or clocks")
    p.add_argument("--allow-disruptive", action="store_true", help="enable constrained u64 debug checks; c64u remains SAFE only")
    p.add_argument("--debug-idle", action="store_true", help="assert u64 has no storage/recording/transfer activity")
    p.add_argument("--intel-root", type=Path, help="reviewed Intel FPGA installation for u64 debug backend")
    p.add_argument("--debug-health-host", help="hostname of the mapped u64, for read-only resume verification")
    p.add_argument("--debug-cycles", type=int, default=10, help="bounded debug cycles (1..10000)")
    p.add_argument("--debug-steps", type=int, default=8, help="minimum actual steps for debug PASS; live stepping currently disabled")
    p.add_argument("--cycles", type=int, default=10, help="complete deterministic pattern cycles (default 10)")
    p.add_argument("--seed", type=int, default=1541)
    p.add_argument("--frequency", type=float, default=3000000, help="FT232H maximum sweep clock, Hz (100k..3M)")
    p.add_argument("--timeout", type=float, default=180, help="suite budget per adapter in seconds")
    p.add_argument("--json", type=Path, help="write full machine-readable report")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not 1 <= args.cycles <= 10000 or not 1 <= args.timeout <= 3600 or not 100000 <= args.frequency <= 3000000:
        p.error("cycles 1..10000, timeout 1..3600, frequency 100000..3000000 required")
    if not 1 <= args.debug_cycles <= 10000 or not 1 <= args.debug_steps <= 80000:
        p.error("debug cycles 1..10000 and steps 1..80000 required")
    try:
        extra = {}
        for entry in args.usb_id:
            identity, kind = entry.split("=")
            vid, pid = (int(x, 16) for x in identity.split(":"))
            if kind not in ("blaster", "ft232h") or not 1 <= vid <= 65535 or not 1 <= pid <= 65535:
                raise ValueError("invalid USB identity/type")
            if (vid, pid) in SUPPORTED and SUPPORTED[vid, pid] != kind:
                raise ValueError("cannot override a known adapter type")
            extra[vid, pid] = kind
        adapters = discover(extra)
        known = {a.identifier for a in adapters}
        if set(args.adapter) - known:
            raise ValueError("selected adapter not found")
        targets = {}
        for entry in args.target:
            identifier, profile = entry.rsplit("=", 1)
            if identifier not in known or profile not in PROFILES or identifier in targets:
                raise ValueError("target needs a discovered identifier and a unique reviewed profile")
            targets[identifier] = profile
        selected = [a for a in adapters if (not args.adapter or a.identifier in args.adapter)
                    and (not args.type or a.kind == args.type)]
        if not selected:
            raise ValueError("no supported adapters selected/found")
        started_utc = datetime.now(timezone.utc).isoformat()
        source_sha256 = {Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                         for path in (__file__, jt.__file__, str(Path(__file__).with_name("jtag_verify_debug.py")))}
        reports = []
        for adapter in selected:
            print(f"Testing {adapter.identifier} ({adapter.kind})", flush=True)
            report = verify(adapter, targets.get(adapter.identifier), args)
            reports.append(report)
            for check in report["results"]:
                print(f"  {check['status']:10} {check['requirement']}: {check['detail']}", flush=True)
                if "compared_bits" in check:
                    print(f"    {check['bypass_scans']} scans, {check['compared_bits']} compared bits, "
                          f"{check['elapsed_s']:.2f}s; short latency median/max "
                          f"{check['short_scan_latency_ms']['median']:.3f}/"
                          f"{check['short_scan_latency_ms']['maximum']:.3f} ms", flush=True)
            print(f"  Debugging suitability: {report['debugging_suitability']}", flush=True)
        output = dict(schema_version=VERSION, started_utc=started_utc,
                      pyftdi=importlib.metadata.version("pyftdi"), python=sys.version,
                      source_sha256=source_sha256,
                      settings={k: str(v) if isinstance(v, Path) else v
                                for k, v in vars(args).items() if k != "json"}, adapters=reports)
        if args.json:
            args.json.write_text(json.dumps(output, indent=2) + "\n")
        if any(c["status"] in ("FAIL", "ERROR") for r in reports for c in r["results"]):
            return 1
        if args.allow_disruptive and any(r.get("target") == "u64" and
                any(c["requirement"] == "DEBUG-SESSION" and c["status"] != "PASS"
                    for c in r["results"]) for r in reports):
            return 2
        return 0 if all(r["debugging_suitability"] in ("TRANSPORT_PASSED_DEBUG_UNVERIFIED", "DEBUG_SESSION_PASSED") for r in reports) else 2
    except (ValueError, ImportError, OSError, jt.JtagError) as exc:
        print(f"jtag-verify: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
