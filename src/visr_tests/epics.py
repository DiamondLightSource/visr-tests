"""Read PVs over Channel Access (aioca) and PV Access (p4p). Nothing is written.

Which servers are searched comes from the standard EPICS environment, e.g. in
the cluster the beamline's gateway: ``EPICS_CA_NAME_SERVERS``,
``EPICS_PVA_NAME_SERVERS`` and ``EPICS_{CA,PVA}_AUTO_ADDR_LIST=NO``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aioca import FORMAT_TIME, CANothing, caget
from p4p.client.asyncio import Context

from .services import CheckError

log = logging.getLogger(__name__)

ATTEMPTS = 2  # a PV that does not answer in time gets one retry
INVALID = 3  # alarm severity: the value cannot be trusted


async def ca_read(pv: str, timeout: float) -> Any:
    """``pv``'s value with its timestamp and alarm, read over Channel Access."""
    failure: CANothing | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return await caget(pv, timeout=timeout, format=FORMAT_TIME)
        except CANothing as error:
            failure = error  # a timeout, or the channel dropped mid-read
        _retrying(pv, timeout, attempt)
    raise CheckError(f"{_no_answer('CA', pv, timeout)}: {failure}")


async def pva_read(pv: str, timeout: float) -> Any:
    """``pv``'s value, read over PV Access."""
    with Context("pva") as context:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(context.get(pv), timeout)
            except TimeoutError:
                _retrying(pv, timeout, attempt)
            except Exception as error:
                raise CheckError(
                    f"pvget {pv} failed: {type(error).__name__}: {error}"
                ) from None
    raise CheckError(_no_answer("PVA", pv, timeout))


def _retrying(pv: str, timeout: float, attempt: int) -> None:
    if attempt < ATTEMPTS:
        log.warning("%s did not answer within %g s, retrying", pv, timeout)


def _no_answer(protocol: str, pv: str, timeout: float) -> str:
    return (
        f"{pv} did not answer over {protocol} within {timeout:g} s "
        f"({ATTEMPTS} attempts)"
    )
