#!/usr/bin/env python3
"""Press a SwitchBot Bot once over Bluetooth LE.

Used as the power button actuator for an Ultimate 64 Elite (MK1), whose power
latch only switches on through its MultiButton:

    python3 tooling/switchbot_press.py --mac AA:BB:CC:DD:EE:FF [--hold 0]

--hold sets the Bot's hold time first (pySwitchbot set_long_press). Keep it 0
for power-on: a press of about 1 s resets the C64 and 4 s switches the U64 off.
A hold of 5 followed by a short press is a full power cycle while the FPGA runs.

Needs `pip install PySwitchbot` (it brings bleak and bleak-retry-connector) and
a powered Bluetooth controller (`bluetoothctl power on`).
"""

import argparse
import asyncio
import sys


async def press(mac, hold, timeout):
    from bleak import BleakScanner
    from switchbot import Switchbot

    device = await BleakScanner.find_device_by_address(mac, timeout=timeout)
    if device is None:
        print(f"[switchbot] {mac} not found within {timeout} s", file=sys.stderr)
        return 2
    bot = Switchbot(device)
    if hold is not None and not await bot.set_long_press(hold):
        print("[switchbot] set_long_press was not acknowledged", file=sys.stderr)
        return 3
    if not await bot.press():
        print("[switchbot] press was not acknowledged", file=sys.stderr)
        return 4
    print(f"[switchbot] pressed {mac}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Press a SwitchBot Bot once.")
    parser.add_argument("--mac", required=True)
    parser.add_argument("--hold", type=int, default=None, help="hold time to set before pressing")
    parser.add_argument("--timeout", type=float, default=20.0, help="scan timeout in seconds")
    args = parser.parse_args()
    return asyncio.run(press(args.mac, args.hold, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
