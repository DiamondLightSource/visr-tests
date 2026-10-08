import base64
import logging

import httpx
import pytest

from fake_services import (
    CLIENT_ID,
    CLIENT_SECRET,
    CONFIG,
    RUN,
    TASK_ID,
    TOKEN,
    Clock,
    FakeServices,
    make,
)
from visr_tests.components import blueapi as blueapi_check
from visr_tests.components import tiled as tiled_check
from visr_tests.experiments.spectroscopy import SCANS, check_scan
from visr_tests.services import (
    BLUEAPI_URL,
    SCAN_TIMEOUT,
    TILED_URL,
    TOKEN_URL,
    CheckError,
    Config,
    ConfigError,
    fetch_token,
)

STEP = SCANS["step"]
SESSION = CONFIG.instrument_session


# --- configuration


def test_config_defaults():
    config = Config.from_env({})
    assert config.blueapi_url == BLUEAPI_URL == "https://b01-1-blueapi.diamond.ac.uk"
    assert config.tiled_url == TILED_URL == "http://b01-1-tiled:8000"
    assert config.token_url == TOKEN_URL
    assert config.scan_timeout == SCAN_TIMEOUT == 300.0
    assert config.instrument_session == ""


def test_config_from_env():
    config = Config.from_env(
        {
            "BLUEAPI_URL": "https://b/",
            "TILED_URL": "http://t:1/",
            "TOKEN_URL": "https://k/token",
            "CLIENT_ID": "id",
            "CLIENT_SECRET": "secret\n",
            "INSTRUMENT_SESSION": "cm1-1",
            "SCAN_TIMEOUT": "60",
        }
    )
    assert config == Config("https://b", "http://t:1", "https://k/token", "id",
                            "secret", "cm1-1", 60.0)  # fmt: skip


@pytest.mark.parametrize("timeout", ["soon", "0", "-1"])
def test_config_bad_timeout(timeout: str):
    with pytest.raises(ConfigError, match="SCAN_TIMEOUT"):
        Config.from_env({"SCAN_TIMEOUT": timeout})


def test_missing_instrument_session_fails_clearly():
    with pytest.raises(ConfigError, match="INSTRUMENT_SESSION not set"):
        Config.from_env({}).require_instrument_session()


def test_config_repr_hides_secret():
    assert CLIENT_SECRET not in repr(CONFIG)
    assert CLIENT_SECRET not in str(CONFIG)


# --- token


def test_token_fetch():
    fake = FakeServices()
    client = httpx.Client(transport=httpx.MockTransport(fake))
    assert fetch_token(client, CONFIG) == TOKEN
    (request,) = fake.requests
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {basic}"
    assert request.content == b"grant_type=client_credentials"


@pytest.mark.parametrize("missing", ["client_id", "client_secret"])
def test_token_needs_credentials(missing: str):
    config = Config(**{**CONFIG.__dict__, missing: ""})
    with pytest.raises(ConfigError, match="not set"):
        fetch_token(httpx.Client(transport=httpx.MockTransport(FakeServices())), config)


@pytest.mark.parametrize(
    "status, body, message",
    [
        (401, {"error": "unauthorized_client", "error_description": CLIENT_SECRET},
         "HTTP 401: unauthorized_client$"),
        (500, "not json", "HTTP 500$"),
        (200, {"token_type": "Bearer"}, "no access_token"),
        (200, {"access_token": ""}, "empty access_token"),
    ],
)  # fmt: skip
def test_token_failure(status: int, body: object, message: str):
    fake = FakeServices()
    fake.token_status, fake.token_body = status, body
    client = httpx.Client(transport=httpx.MockTransport(fake))
    with pytest.raises(CheckError, match=message) as error:
        fetch_token(client, CONFIG)
    assert CLIENT_SECRET not in str(error.value)


def test_token_endpoint_unreachable():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(CheckError, match="unreachable: ConnectError$"):
        fetch_token(client, CONFIG)


# --- component checks


def test_components_pass():
    _, blueapi, tiled = make(fake := FakeServices())
    blueapi_check.test_blueapi_healthz(blueapi)
    tiled_check.test_tiled_healthz(tiled)
    tiled_check.test_tiled_anonymous_read(tiled)
    tiled_check.test_tiled_binned_route(tiled)
    # none of these needs a token
    assert not any(r.url.host == "keycloak.test" for r in fake.requests)
    assert "Authorization" not in fake.requests[0].headers


def test_blueapi_healthz_fails():
    fake = FakeServices()
    fake.healthz_status = 502
    _, blueapi, _ = make(fake)
    with pytest.raises(AssertionError, match="HTTP 502"):
        blueapi_check.test_blueapi_healthz(blueapi)


def test_tiled_healthz_fails():
    fake = FakeServices()
    fake.tiled_healthz_status = 503
    _, _, tiled = make(fake)
    with pytest.raises(AssertionError, match="HTTP 503"):
        tiled_check.test_tiled_healthz(tiled)


def test_tiled_anonymous_read_fails():
    fake = FakeServices()
    fake.count = None  # type: ignore[assignment]
    _, _, tiled = make(fake)
    with pytest.raises(CheckError, match="no meta.count"):
        tiled_check.test_tiled_anonymous_read(tiled)


def test_tiled_binned_route_fails():
    fake = FakeServices()
    fake.unknown_binned_status = 404
    _, _, tiled = make(fake)
    with pytest.raises(AssertionError, match="HTTP 404 for an unknown run"):
        tiled_check.test_tiled_binned_route(tiled)


def test_unreachable_service_fails_clearly():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _, blueapi, _ = make(FakeServices())
    blueapi.client = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(CheckError, match="GET https://blueapi.test/healthz failed"):
        blueapi.healthz()


# --- the scan


def test_scan_passes():
    fake = FakeServices()
    fake.searches_until_found = 2
    fake.metadata_until_stopped = 2
    _, blueapi, tiled = make(fake)
    check_scan(blueapi, tiled, STEP, SESSION, 10.0)
    assert fake.paths()[:4] == [
        "POST /token",
        "POST /api/v1/tasks",
        "POST /token",
        "PUT /api/v1/worker/task",
    ]
    assert fake.paths()[-1] == f"GET /api/v1/binned/{RUN}"


def test_scan_sends_plan_and_params():
    fake = FakeServices()
    _, blueapi, tiled = make(fake)
    check_scan(blueapi, tiled, SCANS["fly"], SESSION, 10.0)
    submit = next(r for r in fake.requests if r.url.path == "/api/v1/tasks")
    assert b'"exposure_time":0.2' in submit.content.replace(b" ", b"")


@pytest.mark.parametrize(
    "change, message",
    [
        ({"submit_status": 422}, "rejected its parameters"),
        ({"submit_status": 404}, "not found"),
        ({"submit_body": {}}, "no task_id"),
        ({"start_status": 409}, "worker busy"),
        ({"outcome": {"outcome": "error", "type": "ValueError", "message": "bad"}},
         "failed: ValueError: bad"),
        ({"errors": ["motor stalled"]}, "1 error"),
        ({"stop": {"exit_status": "abort", "reason": "stopped",
                   "num_events": {"primary": 4}}}, "exit status 'abort': stopped"),
        ({"stop": {"exit_status": "success", "num_events": {"primary": 3}}},
         "3 events, expected 4"),
        ({"binned_body": {"RedTotal": [[1, 2, 3]]}}, r"RedTotal is \(1, 3\)"),
        ({"binned_body": {}}, "RedTotal is None"),
    ],
)  # fmt: skip
def test_scan_fails(change: dict, message: str):
    fake = FakeServices()
    fake.__dict__.update(change)
    _, blueapi, tiled = make(fake)
    with pytest.raises(AssertionError, match=message):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)


def test_unstarted_task_is_deleted():
    fake = FakeServices()
    fake.start_status = 409
    _, blueapi, tiled = make(fake)
    with pytest.raises(CheckError):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)
    assert f"DELETE /api/v1/tasks/{TASK_ID}" in fake.paths()


def test_scan_timeout_aborts_our_task():
    fake = FakeServices()
    fake.polls_until_complete = 1000
    clock = Clock()
    _, blueapi, tiled = make(fake, clock)
    with pytest.raises(CheckError, match="did not complete in time; aborted it"):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)
    assert clock.now == 10.0
    assert fake.paths()[-1] == "PUT /api/v1/worker/state"


def test_scan_timeout_leaves_other_task():
    fake = FakeServices()
    fake.polls_until_complete = 1000
    fake.active_task = {"task_id": "someone-else"}
    _, blueapi, tiled = make(fake)
    with pytest.raises(CheckError, match="no longer the active task, so not aborted"):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)
    assert "PUT /api/v1/worker/state" not in fake.paths()


def test_scan_timeout_no_run_in_tiled():
    fake = FakeServices()
    fake.searches_until_found = 1000
    _, blueapi, tiled = make(fake)
    with pytest.raises(CheckError, match=f"no run in tiled for task {TASK_ID}"):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)


def test_scan_timeout_run_not_stopped():
    fake = FakeServices()
    fake.metadata_until_stopped = 1000
    _, blueapi, tiled = make(fake)
    with pytest.raises(CheckError, match=f"run {RUN} .* did not stop in time"):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)


def test_blueapi_unauthorised():
    fake = FakeServices()
    _, blueapi, tiled = make(fake)
    blueapi._token = lambda: "wrong"
    with pytest.raises(CheckError, match="not authorised"):
        check_scan(blueapi, tiled, STEP, SESSION, 10.0)


# --- secrets


def test_secret_never_logged(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    messages = []

    fake = FakeServices()
    _, blueapi, tiled = make(fake)
    check_scan(blueapi, tiled, STEP, SESSION, 10.0)
    for change in ({"token_status": 401}, {"submit_status": 422}):
        fake = FakeServices()
        fake.__dict__.update(change)
        _, blueapi, tiled = make(fake)
        with pytest.raises(CheckError) as error:
            check_scan(blueapi, tiled, STEP, SESSION, 10.0)
        messages.append(str(error.value))

    assert caplog.text  # something was logged
    for secret in (CLIENT_SECRET, TOKEN, basic):
        assert secret not in caplog.text
        assert not any(secret in message for message in messages)
