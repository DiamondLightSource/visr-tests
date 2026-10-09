"""A fake Keycloak, blueapi and tiled behind one httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx

from visr_tests.services import Blueapi, Config, Tiled

TOKEN_URL = "https://keycloak.test/token"
BLUEAPI_URL = "https://blueapi.test"
TILED_URL = "http://tiled.test:8000"
CLIENT_ID = "b01-1-blueapi-test-runner"
CLIENT_SECRET = "s3cret-client-value"
TOKEN = "t0ken-bearer-value"
TASK_ID = "0b0b0b0b-1111-2222-3333-444444444444"
RUN = "run-abc"

CONFIG = Config(
    blueapi_url=BLUEAPI_URL,
    tiled_url=TILED_URL,
    token_url=TOKEN_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    instrument_session="cm12345-1",
    scan_timeout=10.0,
)


class Clock:
    """A fake monotonic clock that sleeping advances."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeServices:
    """Answers each request from attributes a test can change."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # Keycloak
        self.token_status = 200
        self.token_body: Any = {"access_token": TOKEN, "expires_in": 300}
        # blueapi
        self.healthz_status = 200
        self.submit_status = 201
        self.submit_body: Any = {"task_id": TASK_ID}
        self.start_status = 200
        self.polls_until_complete = 2
        self.outcome: Any = {"outcome": "success"}
        self.errors: list[Any] = []
        self.active_task: Any = {"task_id": TASK_ID}
        self.worker_state: Any = "IDLE"
        self.abort_status = 202
        # tiled
        self.tiled_healthz_status = 200
        self.count = 7
        self.searches_until_found = 1
        self.metadata_until_stopped = 1
        self.stop: dict[str, Any] = {
            "exit_status": "success",
            "num_events": {"primary": 4},
        }
        self.binned_body: Any = {
            "RedTotal": [[1, 2], [3, 4]],
            "GreenTotal": [[1, 2], [3, 4]],
            "BlueTotal": [[1, 2], [3, 4]],
        }
        self.unknown_binned_status = 422
        self._polls = 0
        self._searches = 0
        self._metadata = 0

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            return httpx.Response(self.token_status, json=self.token_body)
        if url.startswith(BLUEAPI_URL):
            return self._blueapi(request)
        if url.startswith(TILED_URL):
            return self._tiled(request)
        return httpx.Response(599)

    def _authorised(self, request: httpx.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _blueapi(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if path == "/healthz":
            return httpx.Response(self.healthz_status, json={"status": "ok"})
        if not self._authorised(request):
            return httpx.Response(401)
        body = json.loads(request.content) if request.content else None
        if (method, path) == ("POST", "/api/v1/tasks"):
            assert isinstance(body, dict)
            assert body.keys() == {"name", "params", "instrument_session"}
            assert body["name"] == "demo_spectroscopy"
            assert body["instrument_session"] == CONFIG.instrument_session
            return httpx.Response(self.submit_status, json=self.submit_body)
        if (method, path) == ("PUT", "/api/v1/worker/task"):
            assert body == {"task_id": TASK_ID}
            return httpx.Response(self.start_status, json={"task_id": TASK_ID})
        if (method, path) == ("GET", f"/api/v1/tasks/{TASK_ID}"):
            self._polls += 1
            complete = self._polls >= self.polls_until_complete
            return httpx.Response(
                200,
                json={
                    "task_id": TASK_ID,
                    "is_complete": complete,
                    "outcome": self.outcome if complete else None,
                    "errors": self.errors if complete else [],
                },
            )
        if (method, path) == ("DELETE", f"/api/v1/tasks/{TASK_ID}"):
            return httpx.Response(200)
        if (method, path) == ("GET", "/api/v1/worker/state"):
            return httpx.Response(200, json=self.worker_state)
        if (method, path) == ("GET", "/api/v1/worker/task"):
            return httpx.Response(200, json=self.active_task)
        if (method, path) == ("PUT", "/api/v1/worker/state"):
            return httpx.Response(self.abort_status, json="ABORTING")
        return httpx.Response(404)

    def _tiled(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/healthz":
            return httpx.Response(self.tiled_healthz_status)
        if path == "/api/v1/search/":
            if params.get("page[limit]") == "0":
                return httpx.Response(200, json={"meta": {"count": self.count}})
            assert params["filter[eq][condition][key]"] == "start.blueapi_task_id"
            assert params["filter[eq][condition][value]"] == f'"{TASK_ID}"'
            self._searches += 1
            found = self._searches >= self.searches_until_found
            return httpx.Response(200, json={"data": [{"id": RUN}] if found else []})
        if path == f"/api/v1/metadata/{RUN}":
            self._metadata += 1
            stopped = self._metadata >= self.metadata_until_stopped
            metadata = {"start": {}, "stop": self.stop if stopped else None}
            return httpx.Response(
                200, json={"data": {"attributes": {"metadata": metadata}}}
            )
        if path == f"/api/v1/binned/{RUN}":
            assert (params["width"], params["height"]) == ("2", "2")
            return httpx.Response(200, json=self.binned_body)
        if path.startswith("/api/v1/binned/"):
            return httpx.Response(self.unknown_binned_status)
        return httpx.Response(404)


def make(
    fake: FakeServices, clock: Clock | None = None
) -> tuple[httpx.Client, Blueapi, Tiled]:
    """A client on the fake, and blueapi and tiled clients that use it."""
    from visr_tests.services import fetch_token

    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(fake))
    blueapi = Blueapi(
        client,
        BLUEAPI_URL,
        lambda: fetch_token(client, CONFIG),
        clock=clock,
        sleep=clock.sleep,
    )
    tiled = Tiled(client, TILED_URL, clock=clock, sleep=clock.sleep)
    return client, blueapi, tiled
