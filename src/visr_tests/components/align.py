"""Check the alignment stage's IOC answers over Channel Access.

Arguments: stage PV prefixes, e.g. BL01C-EA-ALIGN-01. Reads the fastcs-ximc
``Pitch:Acceleration_RBV``.
"""

from visr_tests.epics import ca_read


async def test_align_answers(check_arg: str, pv_timeout: float):
    await ca_read(f"{check_arg}:Pitch:Acceleration_RBV", pv_timeout)
