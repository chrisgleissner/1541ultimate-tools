#!/usr/bin/env python3
"""Sample UII+L SLOT_RSTn through ECP5 JTAG; optionally refresh from SPI first.

Recovered from u2l_slot_rst.py. SAMPLE observes pins without driving them.
An unconfigured FPGA also releases reset: calibrate against known states.
This tool does not establish that the firmware or its network is functional.
See docs/u2pl-jtag.md for recovery and diagnostic procedures.
"""

import argparse
import sys
import time

from u64ii_jtag import DeviceLock, JtagError, Mpsse, Tap
from u2pl_jtag import EXPECTED_IDCODE

IR_BITS = 8
IR_IDCODE = 0xE0
IR_SAMPLE = 0x1C
IR_REFRESH = 0x79
BOUNDARY_BITS = 409
SLOT_RSTN_CELL = 352  # LFE5U-25F CABGA256 ball R3, u2p_ecp5.lpf SLOT_RSTn


class Ecp5Tap(Tap):
    """ECP5 framing on the existing cable/scan implementation."""

    def ir(self, instruction, read=False):
        self.m.tms(0b0011, 4)
        captured = self._shift(instruction, IR_BITS, read)
        self.instruction = instruction
        return captured

    def sample_reset(self):
        self.ir(IR_SAMPLE)
        try:
            boundary = self.dr(0, BOUNDARY_BITS, read=True)
        finally:
            self.ir(IR_IDCODE)
        return (boundary >> SLOT_RSTN_CELL) & 1, boundary

    def refresh(self):
        self.reset()
        self.ir(IR_REFRESH)
        self.idle(1)
        time.sleep(0.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="reload FPGA from SPI before sampling; interrupts cartridge")
    parser.add_argument("--wait", type=float, default=12, help="seconds after refresh before sampling (default: 12)")
    parser.add_argument("--url", default="ftdi://ftdi:232h/1", help="pyftdi FT232H URL")
    parser.add_argument("--frequency", type=float, default=1e6, help="JTAG clock in Hz (default: 1000000)")
    parser.add_argument("--label", default="", help="label to include in the result")
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= 60 or not 0 < args.frequency <= 30e6:
        parser.error("--wait must be 0..60 and --frequency must be >0 and <=30000000")

    try:
        with DeviceLock():
            cable = Mpsse.open(args.url, args.frequency)
            try:
                tap = Ecp5Tap(cable)
                idcode = tap.idcode()
                if idcode != EXPECTED_IDCODE:
                    raise JtagError(f"IDCODE 0x{idcode:08x} is not UII+L LFE5U-25 (0x{EXPECTED_IDCODE:08x})")
                if args.refresh:
                    tap.refresh()
                    time.sleep(args.wait)
                    tap.reset()
                value, boundary = tap.sample_reset()
            finally:
                cable.release()
        label = f"{args.label}: " if args.label else ""
        state = "released" if value else "held in reset"
        print(f"{label}IDCODE=0x{idcode:08x} SLOT_RSTn={value} (C64 {state})")
        print(f"Boundary register: {BOUNDARY_BITS} bits, {boundary.bit_count()} set")
        return 0 if value else 1
    except (JtagError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
