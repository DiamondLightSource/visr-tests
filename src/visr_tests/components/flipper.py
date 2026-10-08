"""Check a flipper's IOC serves its position. Read only: the flipper does not move.

Arguments: flipper PV prefixes, e.g. BL01C-EA-FLIP-01. fastcs-thorlabs-mff
serves ``Position``, the setpoint (a boolOut), and ``Position_RBV``, the
position read back from the device (a boolIn, ZNAM False, ONAM True).
"""

from visr_tests.epics import INVALID, ca_read


async def test_flipper_position_connects(check_arg: str, pv_timeout: float):
    await ca_read(f"{check_arg}:Position", pv_timeout)


async def test_flipper_position_valid(check_arg: str, pv_timeout: float):
    pv = f"{check_arg}:Position_RBV"
    value = await ca_read(pv, pv_timeout)
    assert value.severity < INVALID, f"{pv} is in INVALID alarm"
    assert value in (0, 1), f"{pv} is {value!r}, expected 0 (False) or 1 (True)"
