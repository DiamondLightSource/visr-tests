"""A Channel Access server for the PV checks' tests, run as its own process.

It serves on the port in EPICS_CA_SERVER_PORT, prints READY when it is
serving, and stops when its stdin closes.
"""

import sys

from softioc import asyncio_dispatcher, builder, softioc

INVALID = 3
STATE_ALARM = 7


def flipper(prefix: str):
    builder.SetDeviceName(prefix)
    builder.boolOut("Position", ZNAM="False", ONAM="True", initial_value=0)


def main() -> None:
    # healthy
    flipper("BL01C-EA-FLIP-01")
    builder.boolIn("Position_RBV", ZNAM="False", ONAM="True", initial_value=1)
    # position out of range
    flipper("BL01C-EA-FLIP-02")
    builder.longIn("Position_RBV", initial_value=7)
    # position in INVALID alarm
    flipper("BL01C-EA-FLIP-03")
    invalid = builder.boolIn("Position_RBV", ZNAM="False", ONAM="True")
    builder.SetDeviceName("BL01C-EA-ALIGN-01")
    builder.aIn("Pitch:Acceleration_RBV", initial_value=1.5)
    builder.SetDeviceName("BL01C-DI-DCAM-01")
    builder.stringIn("UPTIME", initial_value="00:00:42")
    builder.LoadDatabase()
    softioc.iocInit(asyncio_dispatcher.AsyncioDispatcher())
    invalid.set(1, severity=INVALID, alarm=STATE_ALARM)
    print("READY", flush=True)
    sys.stdin.read()


if __name__ == "__main__":
    main()
