"""The UI check: its helpers, and the check itself in a real browser.

The browser tests run the check, as the CLI does, against a built ViSR SPA
served by :mod:`fake_ui`. They need the ui extra and Chromium, and a build of
the SPA at the tag the deployment uses, named by VISR_UI_DIST; without those
they are skipped, as they are in CI. To run them::

    VISR_UI_DIST=<atlas>/apps/visr/dist uv run --extra ui pytest tests/test_ui.py

The tests of the browser's token handling need only the ui extra and Chromium.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from fake_services import TASK_ID, FakeServices, make
from fake_ui import CLIENT_ID, CLIENT_SECRET, TOKEN, FakeVisr
from visr_tests.browser import Ui, open_ui, origin, playwright_missing, with_bearer
from visr_tests.services import UI_URL, CheckError, Config, check_succeeded

# --- helpers, no browser


def test_ui_url_default_and_env():
    assert Config.from_env({}).ui_url == UI_URL
    assert Config.from_env({"UI_URL": "http://ui.test:8080/"}).ui_url == (
        "http://ui.test:8080"
    )


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://visr.diamond.ac.uk/api/x?y=1", "https://visr.diamond.ac.uk:443"),
        ("HTTPS://VISR.diamond.ac.uk:443/", "https://visr.diamond.ac.uk:443"),
        ("http://127.0.0.1:8080/a", "http://127.0.0.1:8080"),
        ("http://localhost/a", "http://localhost:80"),
    ],
)
def test_origin(url: str, expected: str):
    assert origin(url) == expected


UI = origin("https://visr.diamond.ac.uk")


@pytest.mark.parametrize(
    "url",
    [
        "https://visr.diamond.ac.uk/",
        "https://visr.diamond.ac.uk/api/blueapi/tasks",
        "https://visr.diamond.ac.uk:443/oauth2/userinfo",
    ],
)
def test_bearer_added_on_ui_origin(url: str):
    headers = with_bearer(url, {"accept": "*/*", "Authorization": "old"}, UI, "tok")
    assert headers == {"accept": "*/*", "authorization": "Bearer tok"}


@pytest.mark.parametrize(
    "url",
    [
        "https://visr-pvws.diamond.ac.uk/mjpg/BL01C-DI-DCAM-05:PVA:OUTPUT",
        "https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/auth",
        "http://visr.diamond.ac.uk/",  # another scheme is another origin
        "https://visr.diamond.ac.uk:8443/",
        "https://visr.diamond.ac.uk.evil.test/",
    ],
)
def test_bearer_not_sent_to_other_origins(url: str):
    assert with_bearer(url, {"accept": "*/*"}, UI, "tok") is None


def test_worker_state_and_task():
    fake = FakeServices()
    _, blueapi, _ = make(fake)
    fake.polls_until_complete = 1
    assert blueapi.worker_state() == "IDLE"
    fake.worker_state = "RUNNING"
    assert blueapi.worker_state() == "RUNNING"
    assert blueapi.task(TASK_ID)["is_complete"] is True


def test_worker_state_unexpected_json():
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json={"state": "IDLE"})

    from visr_tests.services import Blueapi

    client = httpx.Client(transport=httpx.MockTransport(answer))
    blueapi = Blueapi(client, "https://blueapi.test", lambda: "t")
    with pytest.raises(CheckError, match="unexpected JSON"):
        blueapi.worker_state()


@pytest.mark.parametrize(
    "task, message",
    [
        ({"outcome": {"outcome": "success"}, "errors": []}, None),
        ({"outcome": {"outcome": "error", "type": "E", "message": "m"}}, "E: m"),
        ({"outcome": None}, "no success outcome"),
        ({"outcome": {"outcome": "success"}, "errors": ["bad"]}, "1 error"),
    ],
)
def test_check_succeeded(task: dict, message: str | None):
    if message is None:
        check_succeeded("t", "plan", task)
    else:
        with pytest.raises(CheckError, match=message):
            check_succeeded("t", "plan", task)


def run_check(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    keep = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "BLUEAPI_URL",
            "UI_URL",
            "TOKEN_URL",
            "CLIENT_ID",
            "CLIENT_SECRET",
            "INSTRUMENT_SESSION",
            "SCAN_TIMEOUT",
        }
    }
    return subprocess.run(
        [sys.executable, "-m", "visr_tests", "run", "ui/spectroscopy"],
        env={**keep, "REPORT_DIR": str(tmp_path), **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is not None,
    reason="playwright is installed",
)
def test_ui_check_without_playwright_fails_clearly(tmp_path: Path):
    result = run_check(tmp_path, INSTRUMENT_SESSION="cm1-1")
    assert result.returncode == 1
    assert "ConfigError: playwright not installed" in result.stdout
    assert "-browser image" in playwright_missing()


# --- the browser's token handling, against the fake servers

needs_playwright = pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="needs the ui extra (and Chromium)",
)


@pytest.fixture
def servers() -> Iterator[FakeVisr]:
    with FakeVisr(None) as fake:
        yield fake


@pytest.fixture
def browser(servers: FakeVisr) -> Iterator[Ui]:
    with open_ui(servers.ui_url, TOKEN, timeout=10) as ui:
        ui.page.goto(f"{servers.ui_url}/blank")
        yield ui


def redirect(fake: FakeVisr, to: str, code: int = 302) -> str:
    return f"{fake.ui_url}/redirect?code={code}&to={to.replace('&', '%26')}"


def settle(ui: Ui, fake: FakeVisr, path: str, seconds: float = 5.0) -> None:
    """Let the page run until the other origin has had ``path``, or ``seconds``.

    The route runs only while Playwright is called, so wait in the page.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(p == path for _, p, _ in fake.other_requests):
            return
        ui.page.wait_for_timeout(100)


@needs_playwright
def test_event_stream_never_takes_bearer_through_redirect(
    servers: FakeVisr, browser: Ui
):
    other = f"{servers.other_url}/events"
    chain = redirect(servers, redirect(servers, other))
    browser.page.evaluate(
        "([a, b, c]) => { new EventSource(a); new EventSource(b);"
        " fetch(c).catch(() => 0); }",
        [redirect(servers, other), chain, redirect(servers, f"{other}-fetch")],
    )
    settle(browser, servers, "/events-fetch")
    browser.page.wait_for_timeout(1000)  # time for the streams to be (wrongly) sent
    assert ("GET", "/events-fetch", None) in servers.other_requests
    assert all(auth is None for _, _, auth in servers.other_requests), (
        servers.other_requests
    )
    redirects = [r for r in servers.ui_requests if r[1] == "/redirect"]
    assert redirects and all(bearer for _, _, bearer in redirects), redirects


@needs_playwright
def test_event_stream_on_ui_gets_bearer_and_events(servers: FakeVisr, browser: Ui):
    data = browser.page.evaluate(
        """url => new Promise((resolve, reject) => {
            const source = new EventSource(url);
            source.onmessage = event => { source.close(); resolve(event.data); };
            setTimeout(() => reject(new Error("no event")), 5000);
        })""",
        "/stream",
    )
    assert data == '{"hello": "world"}'
    assert ("GET", "/stream", True) in servers.ui_requests


@needs_playwright
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_within_ui_keeps_bearer(servers: FakeVisr, browser: Ui, code: int):
    target = f"{servers.ui_url}/blank"
    status = browser.page.evaluate(
        "url => fetch(url, {method: 'POST', body: 'x'}).then(r => r.status)",
        redirect(servers, redirect(servers, target, code), code),
    )
    assert status == 200
    hops = [r for r in servers.ui_requests if r[1] in ("/redirect", "/blank")]
    assert len(hops) == 4, hops  # the page, two redirects, the target
    assert all(bearer for _, _, bearer in hops), hops
    method = "POST" if code in (307, 308) else "GET"
    assert hops[-1][:2] == (method, "/blank")


@needs_playwright
def test_page_redirect_within_ui_keeps_bearer(servers: FakeVisr, browser: Ui):
    browser.page.goto(redirect(servers, f"{servers.ui_url}/oauth2/sign_in"))
    pages = [r for r in servers.ui_requests if r[1] != "/blank"]
    assert pages == [("GET", "/redirect", True), ("GET", "/oauth2/sign_in", True)]
    with pytest.raises(CheckError, match="went to the login page .*/oauth2/sign_in"):
        browser._check_logged_in()


@needs_playwright
def test_no_task_starts_once_stopped(servers: FakeVisr, browser: Ui):
    start = """id => fetch("/api/blueapi/worker/task", {method: "PUT",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({task_id: id})}).then(r => r.status, () => "failed")"""
    assert browser.page.evaluate(start, "mine") == 404  # sent: no such task
    assert browser.stop_tasks() == ["mine"]
    assert browser.page.evaluate(start, "later") == "failed"
    starts = [r for r in servers.ui_requests if r[1] == "/api/blueapi/worker/task"]
    assert starts == [("PUT", "/api/blueapi/worker/task", True)]


# --- the check in a real browser, against the SPA and fake backends

DIST = os.environ.get("VISR_UI_DIST", "")
SESSION = "cm12345-1"

needs_browser = pytest.mark.skipif(
    not DIST or importlib.util.find_spec("playwright") is None,
    reason="needs the ui extra and VISR_UI_DIST (a built ViSR SPA)",
)


@pytest.fixture
def visr() -> Iterator[FakeVisr]:
    dist = Path(DIST)
    assert (dist / "index.html").is_file(), f"no index.html in {dist}"
    with FakeVisr(dist) as fake:
        yield fake


def check(tmp_path: Path, fake: FakeVisr, **env: str) -> subprocess.CompletedProcess:
    result = run_check(
        tmp_path,
        UI_URL=fake.ui_url,
        BLUEAPI_URL=f"{fake.ui_url}/blueapi",
        TOKEN_URL=f"{fake.ui_url}/token",
        CLIENT_ID=CLIENT_ID,
        CLIENT_SECRET=CLIENT_SECRET,
        INSTRUMENT_SESSION=SESSION,
        **env,
    )
    print(result.stdout[-4000:], result.stderr[-2000:])
    output = result.stdout + result.stderr + reports(tmp_path)
    assert TOKEN not in output and CLIENT_SECRET not in output
    return result


def reports(tmp_path: Path) -> str:
    return "".join(path.read_text() for path in tmp_path.rglob("*.xml"))


def assert_bearer_only_on_ui_origin(fake: FakeVisr) -> None:
    ui = [r for r in fake.ui_requests if not r[1].startswith(("/token", "/blueapi"))]
    assert ui, "the browser made no requests to the UI"
    assert all(bearer for _, _, bearer in ui), [r for r in ui if not r[2]]
    assert fake.other_requests, "the browser made no requests to the other origin"
    assert all(auth is None for _, _, auth in fake.other_requests), fake.other_requests


@needs_browser
def test_ui_scan_passes(tmp_path: Path, visr: FakeVisr):
    result = check(tmp_path, visr)
    assert result.returncode == 0
    assert len(visr.submitted) == 1
    submitted = visr.submitted[0]
    assert submitted["name"] == "demo_spectroscopy"
    assert submitted["instrument_session"] == SESSION
    # the form's defaults at visr@0.5.4-auth-test-10
    assert submitted["params"] == {
        "total_number_of_scan_points": 25,
        "grid_size": 5.0,
        "grid_origin_x": 0.0,
        "grid_origin_y": 0.0,
        "exposure_time": 0.1,
    }
    (task,) = visr.tasks.values()
    assert task["is_complete"] and task["outcome"]["outcome"] == "success"
    assert ("GET", "/blueapi/api/v1/worker/state", True) in visr.ui_requests
    assert ("GET", f"/blueapi/api/v1/tasks/{task['task_id']}", True) in (
        visr.ui_requests
    )
    assert not visr.aborts
    # the scan's events reached the UI, which then submitted its workflow
    assert ("GET", "/api/data/events", True) in visr.ui_requests
    assert len(visr.workflows) == 1
    assert_bearer_only_on_ui_origin(visr)
    assert not list(tmp_path.rglob("*.png")), "screenshot taken of a passing check"


@needs_browser
def test_ui_worker_busy_fails_without_submitting(tmp_path: Path, visr: FakeVisr):
    visr.worker = "RUNNING"
    result = check(tmp_path, visr)
    assert result.returncode == 1
    assert "another scan is running" in result.stdout
    assert "worker is RUNNING, not IDLE" in reports(tmp_path)
    assert not visr.submitted
    assert not any(
        path == "/" or path.startswith("/acquisition")
        for _, path, _ in visr.ui_requests
    )


@needs_browser
def test_ui_logged_out_fails_clearly(tmp_path: Path, visr: FakeVisr):
    visr.accept_bearer = False
    result = check(tmp_path, visr)
    assert result.returncode == 1
    report = reports(tmp_path)
    assert "the UI shows logged out with the service account's bearer token" in report
    assert "skip_jwt_bearer_tokens = true" in report
    assert not visr.submitted
    assert list(tmp_path.rglob("spectroscopy.png")), "no screenshot of the failure"
    assert_bearer_only_on_ui_origin(visr)


@needs_browser
def test_ui_scan_timeout_aborts(tmp_path: Path, visr: FakeVisr):
    visr.complete_scans = False
    result = check(tmp_path, visr, SCAN_TIMEOUT="8")
    assert result.returncode == 1
    report = reports(tmp_path)
    assert "did not report task" in report
    assert "within SCAN_TIMEOUT (8s); aborted it" in report
    assert len(visr.aborts) == 1
    assert list(tmp_path.rglob("spectroscopy.png")), "no screenshot of the failure"


@needs_browser
def test_ui_error_after_start_aborts_the_task(tmp_path: Path, visr: FakeVisr):
    visr.fail_state_when_running = True
    result = check(tmp_path, visr)
    assert result.returncode == 1
    report = reports(tmp_path)
    assert "the UI reported 'Failed to run plan" in report
    assert "; aborted it" in report
    assert len(visr.aborts) == 1
    (task,) = visr.tasks.values()
    assert task["outcome"]["type"] == "Abort"


@needs_browser
def test_ui_failure_never_aborts_a_task_it_did_not_start(
    tmp_path: Path, visr: FakeVisr
):
    visr.foreign_task_on_submit = True
    result = check(tmp_path, visr)
    assert result.returncode == 1
    report = reports(tmp_path)
    assert "submission of demo_spectroscopy got HTTP 500" in report
    assert "the UI started no task" in report
    assert not visr.aborts
    assert visr.worker == "RUNNING"
