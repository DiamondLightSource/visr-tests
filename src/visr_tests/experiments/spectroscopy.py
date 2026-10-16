"""Run the spectroscopy experiment end to end and check the data it wrote.

Each scan runs demo_spectroscopy through blueapi, then finds its run in tiled by
the blueapi task id and checks the run's stop document and /binned image.
The scans move the sample stage; the fly scan also needs the PandA and the PMAC.
"""

import logging
import time
from enum import StrEnum
from typing import Any

import pytest

from visr_tests.services import Blueapi, Config, Tiled


class ScanType(StrEnum):
    """The type of scan to perform."""

    FLY = "fly"
    STEP = "step"


log = logging.getLogger(__name__)

PLAN = "spectroscopy"
POINTS = 4
SIDE = 2  # bins along each side of the /binned image: sqrt(POINTS)
# demo_spectroscopy flies when its stage velocity, grid_size /
# (sqrt(points) * exposure_time), is at most 9.8 mm/s, and steps otherwise
SCANS: dict[str, dict[str, Any]] = {
    "step": {
        "scan_type": ScanType.STEP,
        "total_number_of_scan_points": POINTS,
        "grid_size": 2.0,
        "exposure_time": 0.2,
    },
    "fly": {
        "scan_type": ScanType.FLY,
        "total_number_of_scan_points": POINTS,
        "grid_size": 2.0,
        "exposure_time": 0.2,
    },
}


def check_scan(
    blueapi: Blueapi, tiled: Tiled, params: dict[str, Any], session: str, timeout: float
) -> None:
    """Run one scan and check its run in tiled, all within ``timeout`` seconds."""
    deadline = blueapi.clock() + timeout
    task_id = blueapi.run_plan(PLAN, params, session, deadline)
    run, stop = tiled.wait_for_run(task_id, deadline)

    exit_status = stop.get("exit_status")
    assert exit_status == "success", (
        f"run {run} exit status {exit_status!r}: {stop.get('reason', '')}"
    )
    events = (stop.get("num_events") or {}).get("primary", 0)
    assert events == POINTS, f"run {run} has {events} events, expected {POINTS}"

    binned = tiled.binned(run, SIDE, SIDE)
    red = binned.get("RedTotal")
    shape = (
        (len(red), len(red[0]))
        if isinstance(red, list) and red and isinstance(red[0], list)
        else None
    )
    assert shape == (SIDE, SIDE), (
        f"run {run} /binned RedTotal is {shape}, expected {(SIDE, SIDE)}"
    )
    log.info(
        "run %s: %s",
        run,
        {key: binned.get(key) for key in ("RedTotal", "GreenTotal", "BlueTotal")},
    )


@pytest.mark.parametrize("params", SCANS.values(), ids=SCANS.keys())
def test_spectroscopy(
    config: Config,
    instrument_session: str,
    token: str,  # fail at setup, before anything runs, without credentials
    blueapi: Blueapi,
    tiled: Tiled,
    params: dict[str, Any],
):
    started = time.monotonic()
    check_scan(blueapi, tiled, params, instrument_session, config.scan_timeout)
    log.info("scan passed in %.1fs", time.monotonic() - started)
