"""Check each IOC in IOC_NAMES answers over Channel Access.

Reads ``<IOC name in upper case>:UPTIME``, which every IOC that loads
devIocStats (``devIocStats.iocAdminSoft`` in its ioc.yaml) serves.
"""

from visr_tests.epics import ca_read


async def test_ioc_uptime(ioc: str, pv_timeout: float):
    await ca_read(f"{ioc.upper()}:UPTIME", pv_timeout)
