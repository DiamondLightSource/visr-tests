"""Clients for the services the checks talk to: Keycloak, blueapi and tiled.

Everything is plain HTTP with httpx. Configuration comes from the environment
(:meth:`Config.from_env`). The client secret and the bearer token are held only
in memory: they are never logged, and no message raised here contains them.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BLUEAPI_URL = "https://b01-1-blueapi.diamond.ac.uk"
TILED_URL = "http://b01-1-tiled:8000"
UI_URL = "https://visr.diamond.ac.uk"
TOKEN_URL = "https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/token"
SCAN_TIMEOUT = 300.0
PV_TIMEOUT = 5.0
POLL_INTERVAL = 2.0


class ConfigError(Exception):
    """The environment does not configure what a check needs."""


class CheckError(AssertionError):
    """A service did something wrong; the message is safe to log and report."""


@dataclass(frozen=True)
class Config:
    """Where the services are and how to authenticate, from the environment."""

    blueapi_url: str = BLUEAPI_URL
    tiled_url: str = TILED_URL
    token_url: str = TOKEN_URL
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    instrument_session: str = ""
    scan_timeout: float = SCAN_TIMEOUT
    pv_timeout: float = PV_TIMEOUT
    ui_url: str = UI_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        env = os.environ if environ is None else environ

        def get(name: str, default: str = "") -> str:
            return env.get(name, "").strip() or default

        def seconds(name: str, default: float) -> float:
            text = get(name, str(default))
            try:
                value = float(text)
            except ValueError:
                raise ConfigError(
                    f"{name} must be a number of seconds, not {text!r}"
                ) from None
            if value <= 0:
                raise ConfigError(f"{name} must be positive, not {text!r}")
            return value

        return cls(
            blueapi_url=get("BLUEAPI_URL", BLUEAPI_URL).rstrip("/"),
            tiled_url=get("TILED_URL", TILED_URL).rstrip("/"),
            ui_url=get("UI_URL", UI_URL).rstrip("/"),
            token_url=get("TOKEN_URL", TOKEN_URL),
            client_id=get("CLIENT_ID"),
            client_secret=get("CLIENT_SECRET"),
            instrument_session=get("INSTRUMENT_SESSION"),
            scan_timeout=seconds("SCAN_TIMEOUT", SCAN_TIMEOUT),
            pv_timeout=seconds("PV_TIMEOUT", PV_TIMEOUT),
        )

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("CLIENT_ID", self.client_id),
                ("CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"{' and '.join(missing)} not set: this check needs an OAuth2 "
                "client_credentials client for blueapi (see the README, "
                "'Provisioning the client secret')"
            )

    def require_instrument_session(self) -> str:
        if not self.instrument_session:
            raise ConfigError(
                "INSTRUMENT_SESSION not set: set it to an instrument session the "
                "service account may run plans in, e.g. INSTRUMENT_SESSION=cm12345-1"
            )
        return self.instrument_session


def ioc_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """The IOCs named in IOC_NAMES, separated by commas or whitespace."""
    env = os.environ if environ is None else environ
    return env.get("IOC_NAMES", "").replace(",", " ").split()


def _shorten(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _json(response: httpx.Response, what: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise CheckError(f"{what} did not return JSON") from None


def _detail(response: httpx.Response) -> str:
    """blueapi's (FastAPI's) error ``detail``, shortened, without echoed input."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return ""
    if isinstance(detail, list):
        parts = [
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg')}"
            for err in detail
            if isinstance(err, dict)
        ]
        detail = "; ".join(dict.fromkeys(parts))  # blueapi repeats some errors
    if not isinstance(detail, str) or not detail:
        return ""
    return ": " + _shorten(detail)


def fetch_token(client: httpx.Client, config: Config) -> str:
    """An access token from the OAuth2 client_credentials grant.

    The client authenticates with client_secret_basic (credentials in the
    Authorization header). Only the RFC 6749 ``error`` code of a refusal is
    reported, never the response body.
    """
    config.require_credentials()
    try:
        response = client.post(
            config.token_url,
            data={"grant_type": "client_credentials"},
            auth=(config.client_id, config.client_secret),
        )
    except httpx.HTTPError as error:
        raise CheckError(
            f"token endpoint {config.token_url} unreachable: {type(error).__name__}"
        ) from None
    if response.status_code != 200:
        try:
            code = response.json().get("error")
        except (ValueError, AttributeError):
            code = None
        reason = f": {code}" if isinstance(code, str) else ""
        raise CheckError(f"token endpoint returned HTTP {response.status_code}{reason}")
    try:
        token = response.json()["access_token"]
    except (ValueError, KeyError, TypeError):
        raise CheckError("token endpoint response has no access_token") from None
    if not isinstance(token, str) or not token:
        raise CheckError("token endpoint returned an empty access_token")
    log.info("obtained a client_credentials token for %s", config.client_id)
    return token


def check_succeeded(task_id: str, plan: str, task: Mapping[str, Any]) -> None:
    """Raise unless the complete ``task`` succeeded with no errors."""
    outcome = task.get("outcome")
    errors = task.get("errors") or []
    if not isinstance(outcome, dict) or outcome.get("outcome") != "success":
        if isinstance(outcome, dict) and outcome.get("outcome") == "error":
            why = f"{outcome.get('type')}: {outcome.get('message')}"
        else:
            why = f"no success outcome ({outcome!r})"
        raise CheckError(f"task {task_id} ({plan}) failed: {_shorten(why)}")
    if errors:
        raise CheckError(
            f"task {task_id} ({plan}) reported {len(errors)} error(s): "
            f"{_shorten(str(errors[0]))}"
        )


class Blueapi:
    """blueapi's REST API. Requests under /api/v1 carry the bearer token.

    ``token`` is called only when an authenticated request is first made, so a
    check that only reads /healthz needs no credentials.
    """

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        token: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.client = client
        self.url = url
        self._token = token
        self.clock = clock
        self.sleep = sleep
        self.poll_interval = poll_interval

    def request(
        self, method: str, path: str, json: Any = None, authenticated: bool = True
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token()}"
        try:
            response = self.client.request(
                method, f"{self.url}{path}", headers=headers, json=json
            )
        except httpx.HTTPError as error:
            raise CheckError(
                f"{method} {self.url}{path} failed: {type(error).__name__}: {error}"
            ) from None
        if response.status_code in (401, 403):
            raise CheckError(
                f"{method} {path} not authorised (HTTP {response.status_code})"
            )
        return response

    def get_json(self, path: str) -> Any:
        response = self.request("GET", path)
        if response.status_code != 200:
            raise CheckError(f"GET {path} returned HTTP {response.status_code}")
        return _json(response, f"GET {path}")

    def healthz(self) -> int:
        """The HTTP status of the unauthenticated /healthz."""
        return self.request("GET", "/healthz", authenticated=False).status_code

    def run_plan(
        self,
        plan: str,
        params: Mapping[str, Any],
        instrument_session: str,
        deadline: float,
    ) -> str:
        """Run a plan on the worker and wait for it to succeed; its task id.

        POST /api/v1/tasks, PUT /api/v1/worker/task, then poll
        GET /api/v1/tasks/{id} until complete. A task submitted but not
        started is deleted; one still running at ``deadline`` (on ``clock``)
        is aborted, if it is still the worker's active task.
        """
        response = self.request(
            "POST",
            "/api/v1/tasks",
            json={
                "name": plan,
                "params": dict(params),
                "instrument_session": instrument_session,
            },
        )
        if response.status_code == 404:
            raise CheckError(f"plan {plan!r} not found (HTTP 404)")
        if response.status_code == 422:
            raise CheckError(
                f"plan {plan!r} rejected its parameters{_detail(response)}"
            )
        if response.status_code != 201:
            raise CheckError(
                f"submitting plan {plan!r} returned HTTP {response.status_code}"
                f"{_detail(response)}"
            )
        body = _json(response, "POST /api/v1/tasks")
        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise CheckError("POST /api/v1/tasks response has no task_id")
        log.info("submitted %s as task %s", plan, task_id)

        response = self.request("PUT", "/api/v1/worker/task", json={"task_id": task_id})
        if response.status_code != 200:
            self._forget(task_id)
            if response.status_code == 409:
                raise CheckError(f"worker busy, could not start {plan!r} (HTTP 409)")
            raise CheckError(
                f"starting task {task_id} returned HTTP {response.status_code}"
                f"{_detail(response)}"
            )
        log.info("started task %s", task_id)

        while True:
            task = self.task(task_id)
            if task.get("is_complete") is True:
                break
            if self.clock() >= deadline:
                raise CheckError(
                    f"task {task_id} ({plan}) did not complete in time"
                    f"{self.abort(task_id)}"
                )
            self.sleep(self.poll_interval)

        check_succeeded(task_id, plan, task)
        log.info("task %s completed successfully", task_id)
        return task_id

    def _forget(self, task_id: str) -> None:
        """Best effort: delete a task that was submitted but not started."""
        try:
            self.request("DELETE", f"/api/v1/tasks/{task_id}")
        except CheckError as error:
            log.warning("could not delete unstarted task %s: %s", task_id, error)

    def worker_state(self) -> str:
        """The worker's state, e.g. ``IDLE`` or ``RUNNING``."""
        state = self.get_json("/api/v1/worker/state")
        if not isinstance(state, str):
            raise CheckError("GET /api/v1/worker/state returned unexpected JSON")
        return state

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.get_json(f"/api/v1/tasks/{task_id}")
        if not isinstance(task, dict):
            raise CheckError(f"GET /api/v1/tasks/{task_id} returned unexpected JSON")
        return task

    def abort(self, task_id: str) -> str:
        """Abort the worker's active task, only if it is ``task_id``.

        Returns what happened, as a clause to append to an error message.
        """
        try:
            active = self.get_json("/api/v1/worker/task")
            if not isinstance(active, dict) or active.get("task_id") != task_id:
                return "; it is no longer the active task, so not aborted"
            response = self.request(
                "PUT",
                "/api/v1/worker/state",
                json={"new_state": "ABORTING", "reason": "visr-tests scan timeout"},
            )
        except CheckError as error:
            return f"; abort failed: {error}"
        if response.status_code != 202:
            return f"; abort returned HTTP {response.status_code}{_detail(response)}"
        return "; aborted it"


class Tiled:
    """tiled's REST API, read anonymously."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.client = client
        self.url = url
        self.clock = clock
        self.sleep = sleep
        self.poll_interval = poll_interval

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        try:
            return self.client.get(f"{self.url}{path}", params=params)
        except httpx.HTTPError as error:
            raise CheckError(
                f"GET {self.url}{path} failed: {type(error).__name__}: {error}"
            ) from None

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        response = self.get(path, params)
        if response.status_code != 200:
            raise CheckError(f"GET {path} returned HTTP {response.status_code}")
        return _json(response, f"GET {path}")

    def healthz(self) -> int:
        return self.get("/healthz").status_code

    def count(self) -> int:
        """How many runs an anonymous search of the root finds."""
        body = self.get_json("/api/v1/search/", {"page[limit]": 0})
        count = body.get("meta", {}).get("count") if isinstance(body, dict) else None
        if not isinstance(count, int):
            raise CheckError("GET /api/v1/search/ response has no meta.count")
        return count

    def binned_status(self, run: str) -> int:
        return self.get(f"/api/v1/binned/{run}").status_code

    def find_run(self, task_id: str) -> str | None:
        """The run whose start document has ``blueapi_task_id == task_id``."""
        body = self.get_json(
            "/api/v1/search/",
            {
                "filter[eq][condition][key]": "start.blueapi_task_id",
                "filter[eq][condition][value]": f'"{task_id}"',
            },
        )
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            run = data[0].get("id")
            return run if isinstance(run, str) else None
        return None

    def stop(self, run: str) -> dict[str, Any] | None:
        """The run's stop document, or None while it is still running."""
        body = self.get_json(f"/api/v1/metadata/{run}")
        try:
            stop = body["data"]["attributes"]["metadata"].get("stop")
        except (KeyError, TypeError, AttributeError):
            raise CheckError(
                f"GET /api/v1/metadata/{run} has no data.attributes.metadata"
            ) from None
        return stop if isinstance(stop, dict) else None

    def wait_for_run(self, task_id: str, deadline: float) -> tuple[str, dict[str, Any]]:
        """Poll until the task's run is in tiled and stopped: (run, stop)."""
        run: str | None = None
        while True:
            run = run or self.find_run(task_id)
            if run:
                stop = self.stop(run)
                if stop is not None:
                    log.info("run %s for task %s is in tiled", run, task_id)
                    return run, stop
            if self.clock() >= deadline:
                if run is None:
                    raise CheckError(f"no run in tiled for task {task_id}")
                raise CheckError(f"run {run} for task {task_id} did not stop in time")
            self.sleep(self.poll_interval)

    def binned(self, run: str, width: int, height: int) -> dict[str, Any]:
        body = self.get_json(
            f"/api/v1/binned/{run}", {"width": width, "height": height}
        )
        if not isinstance(body, dict):
            raise CheckError(f"GET /api/v1/binned/{run} returned unexpected JSON")
        return body
