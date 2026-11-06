"""Run a spectroscopy scan through the ViSR web UI, as a user would.

Opens the Spectroscopy page (UI_URL) in headless Chromium, authenticated by the
service account's bearer token, presses Run with the form's default
parameters, and waits for the UI to report "Plan succeeded". blueapi's task API
then confirms the task completed successfully.

The scan is real: it moves the sample stage. When it finishes, the UI submits
the visr-reconstruction workflow for the instrument session, as it does for any
scan. The check never queues behind another scan: if the blueapi worker is not
IDLE it fails without pressing Run. If it fails once the UI may have started
the scan, it aborts the scan the UI started, and only that.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from visr_tests.browser import PlanTimeoutError, Ui
from visr_tests.services import Blueapi, CheckError, Config, check_succeeded

log = logging.getLogger(__name__)

PLAN = "demo_spectroscopy"
PAGE = "/acquisition/spectroscopy"


def test_spectroscopy_ui(
    config: Config,
    instrument_session: str,
    blueapi: Blueapi,
    start_ui: Callable[[], Ui],
):
    started = time.monotonic()
    state = blueapi.worker_state()
    if state != "IDLE":
        raise CheckError(
            f"the blueapi worker is {state}, not IDLE: another scan is running, "
            "so this check did not submit one"
        )

    ui = start_ui()
    ui.open(PAGE, instrument_session)
    remaining = config.scan_timeout - (time.monotonic() - started)
    try:
        task_id, request = ui.run_plan(PLAN, max(remaining, 1.0) * 1000)
        check_task(blueapi, instrument_session, task_id, request)
    except PlanTimeoutError as timeout:
        raise CheckError(
            f"the UI did not report task {timeout.task_id} ({PLAN}) finished within "
            f"SCAN_TIMEOUT ({config.scan_timeout:g}s){abort_own(ui, blueapi)}"
        ) from None
    except CheckError as error:
        raise CheckError(f"{error}{abort_own(ui, blueapi)}") from None
    except BaseException as error:
        # not a CheckError: keep it as it is, its message unchanged
        log.warning("after %s%s", type(error).__name__, abort_own(ui, blueapi))
        raise
    log.info("UI scan passed in %.1fs", time.monotonic() - started)


def abort_own(ui: Ui, blueapi: Blueapi) -> str:
    """Abort what the UI started, as a clause for an error message.

    The page may have started the task before the check failed (a slow answer
    to the submission, an error the UI showed after starting it): stop it
    starting any more, then abort each task it started that is still the
    worker's active task. A task the page did not start is never aborted.
    """
    started = ui.stop_tasks()
    if not started:
        return "; the UI started no task"
    return "".join(blueapi.abort(task_id) for task_id in started)


def check_task(
    blueapi: Blueapi, instrument_session: str, task_id: str, request: dict[str, Any]
) -> None:
    """blueapi agrees the task the UI ran succeeded, in the right session."""
    session = request.get("instrument_session")
    assert session == instrument_session, (
        f"the UI ran {PLAN} in instrument session {session!r}, "
        f"not {instrument_session!r}"
    )
    task = blueapi.task(task_id)
    if task.get("is_complete") is not True:
        raise CheckError(
            f"the UI reports task {task_id} succeeded but blueapi says it is "
            "not complete"
        )
    check_succeeded(task_id, PLAN, task)
