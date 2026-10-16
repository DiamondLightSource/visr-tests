"""Check a PandA's IOC answers over PV Access.

Arguments: PandA PV prefixes, e.g. BL01C-EA-PANDA-01 BL01C-MO-PPANDA-01.
Reads the fastcs-pandablocks ``Pcap:Active``.
"""

from visr_tests.epics import pva_read


async def test_panda_answers(check_arg: str, pv_timeout: float):
    await pva_read(f"{check_arg}:Pcap:Active", pv_timeout)
