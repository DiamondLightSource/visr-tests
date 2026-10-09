"""Drive the ViSR web UI in headless Chromium, as the service account.

The UI sits behind oauth2-proxy, which normally holds a user's session in a
cookie. Here the browser instead sends the client_credentials token as
``Authorization: Bearer`` on every request to the UI's own origin, and on no
other: requests to other origins (the PV camera stream, the identity provider)
go out untouched, and so does any redirect from the UI to another origin. That
needs the UI's oauth2-proxy to accept bearer tokens (``skip_jwt_bearer_tokens``
and ``extra_jwt_issuers``, see the README).

Playwright is imported only when a browser is opened, so the other checks run
without it; it comes with the ``ui`` extra and the ``-browser`` image.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from .services import CheckError

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import (
        APIRequestContext,
        APIResponse,
        Locator,
        Page,
        Response,
        Route,
    )

log = logging.getLogger(__name__)

UI_TIMEOUT = 30.0  # seconds for a page to load or a control to become ready
USERINFO = "/oauth2/userinfo"
LOGIN_PATHS = ("/oauth2/start", "/oauth2/sign_in")
PROXY_FLAGS = (
    "skip_jwt_bearer_tokens = true and "
    "extra_jwt_issuers = https://identity.diamond.ac.uk/realms/dls=account"
)
# The key the UI keeps its selected instrument session under, JSON encoded
SESSION_KEY = "instrument-session-id"
DEFAULT_PORTS = {"http": 80, "https": 443}
REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 20  # as Chromium
SUBMIT = "/api/blueapi/tasks"  # POST: the UI submits a task
START = "/api/blueapi/worker/task"  # PUT {"task_id": ...}: the UI starts it
# Marks the requests of the page's event streams (see STREAMS)
STREAM_HEADER = "x-visr-tests-stream"

# An event stream never ends, so the route cannot fetch it whole and fulfil it,
# as it does every other request to the UI; it can only let the browser send it
# with the bearer added. Chromium would then send the bearer on through any
# redirect. So the page's EventSource for the UI's own origin is replaced by
# one built on fetch() with redirect: "error", which never follows a redirect,
# and its requests carry a per-browser marker, the only requests the route
# lets out that way. Any other EventSource request goes out without the bearer.
STREAMS = r"""
(() => {
  const Native = window.EventSource;
  if (!Native) return;
  const MARK = %(mark)s, NONCE = %(nonce)s;
  class UiEventSource extends EventTarget {
    constructor(url, init) {
      const href = new URL(url, location.href).href;
      if (new URL(href).origin !== location.origin) return new Native(url, init);
      super();
      this.url = href;
      this.withCredentials = Boolean(init && init.withCredentials);
      this.readyState = 0;
      this.onopen = this.onmessage = this.onerror = null;
      this._retry = 3000;
      this._lastId = "";
      this._abort = null;
      this._connect();
    }
    _fire(type, init) {
      const event = init ? new MessageEvent(type, init) : new Event(type);
      this.dispatchEvent(event);
      const handler = ["open", "message", "error"].includes(type)
        ? this["on" + type] : null;
      if (typeof handler === "function") handler.call(this, event);
    }
    _retryLater() {
      if (this.readyState === 2) return;
      this.readyState = 0;
      this._fire("error");
      setTimeout(() => { if (this.readyState !== 2) this._connect(); }, this._retry);
    }
    async _connect() {
      const abort = new AbortController();
      this._abort = abort;
      const headers = { Accept: "text/event-stream", [MARK]: NONCE };
      if (this._lastId) headers["Last-Event-ID"] = this._lastId;
      let response;
      try {
        response = await fetch(this.url, {
          headers, cache: "no-store", redirect: "error", signal: abort.signal,
          credentials: this.withCredentials ? "include" : "same-origin",
        });
      } catch (error) {
        return this._retryLater();
      }
      if (this.readyState === 2) return;
      const type = (response.headers.get("content-type") || "").toLowerCase();
      if (response.status !== 200 || !type.startsWith("text/event-stream")) {
        this.readyState = 2;
        this._fire("error");
        return;
      }
      this.readyState = 1;
      this._fire("open");
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "", data = "", name = "", id = this._lastId;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          // a "\r" ending a chunk waits, in case a "\n" follows it
          const lines = (buffer + value).split(/\r\n|\n|\r(?!$)/);
          buffer = lines.pop();
          for (const line of lines) {
            if (line === "") {
              this._lastId = id;
              if (data !== "") {
                this._fire(name || "message", {
                  data: data.slice(0, -1), lastEventId: id, origin: location.origin,
                });
              }
              data = name = "";
              continue;
            }
            if (line.startsWith(":")) continue;
            const colon = line.indexOf(":");
            const field = colon < 0 ? line : line.slice(0, colon);
            let text = colon < 0 ? "" : line.slice(colon + 1);
            if (text.startsWith(" ")) text = text.slice(1);
            if (field === "data") data += text + "\n";
            else if (field === "event") name = text;
            else if (field === "id" && !text.includes("\0")) id = text;
            else if (field === "retry" && /^\d+$/.test(text)) {
              this._retry = Number(text);
            }
          }
        }
      } catch (error) {
        // the stream broke, or close() aborted it
      }
      this._retryLater();
    }
    close() {
      this.readyState = 2;
      if (this._abort) this._abort.abort();
    }
  }
  for (const [name, value] of [["CONNECTING", 0], ["OPEN", 1], ["CLOSED", 2]]) {
    Object.defineProperty(UiEventSource, name, { value });
    Object.defineProperty(UiEventSource.prototype, name, { value });
  }
  window.EventSource = UiEventSource;
})();
"""


def playwright_missing() -> str:
    """Why the browser cannot run, or "" if Playwright is installed."""
    if importlib.util.find_spec("playwright") is None:
        return (
            "playwright not installed: run the UI checks in the "
            "visr-tests:<version>-browser image, or install the ui extra"
        )
    return ""


def origin(url: str) -> str:
    """``scheme://host:port`` of ``url``, with the default port made explicit."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or DEFAULT_PORTS.get(scheme)
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"


def with_bearer(
    url: str, headers: Mapping[str, str], ui_origin: str, token: str
) -> dict[str, str] | None:
    """``headers`` plus the bearer token if ``url`` is on the UI's origin.

    None for a request to any other origin: it must go out unchanged.
    """
    if origin(url) != ui_origin:
        return None
    kept = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    return {**kept, "authorization": f"Bearer {token}"}


class PlanTimeoutError(Exception):
    """The UI submitted a plan but did not report its outcome in time."""

    def __init__(self, task_id: str):
        super().__init__(task_id)
        self.task_id = task_id


class TaskStarts:
    """The tasks the page asked blueapi to start, and a stop to any more."""

    def __init__(self) -> None:
        self.task_ids: list[str] = []
        self.stopped = False

    def allow(self, method: str, path: str, body: Callable[[], Any]) -> bool:
        """Whether a request from the page may go out; records any task start.

        A start is recorded before it is sent, so a task this page started is
        always known, even if the page never sees blueapi's answer.
        """
        if (method, path) != ("PUT", START):
            return True
        if self.stopped:
            return False
        sent = body()
        task_id = sent.get("task_id") if isinstance(sent, dict) else None
        if isinstance(task_id, str):
            self.task_ids.append(task_id)
        return True

    def stop(self) -> list[str]:
        """Let the page start no more tasks: the ones it did start."""
        self.stopped = True
        return list(dict.fromkeys(self.task_ids))


def logged_out(why: str) -> CheckError:
    return CheckError(
        f"the UI shows logged out with the service account's bearer token ({why}): "
        f"the UI's oauth2-proxy must accept bearer tokens, with {PROXY_FLAGS}"
    )


class Ui:
    """The ViSR UI open in one browser page, authenticated by bearer token."""

    def __init__(
        self,
        page: Page,
        url: str,
        timeout: float = UI_TIMEOUT,
        starts: TaskStarts | None = None,
        followed: list[str] | None = None,
    ):
        self.page = page
        self.url = url
        self.origin = origin(url)
        self.timeout_ms = timeout * 1000
        self.starts = starts or TaskStarts()
        # pages the UI redirected to, within its origin, followed by the route
        self.followed = followed if followed is not None else []
        self.userinfo_status: int | None = None
        self.failed: list[str] = []  # same-origin responses with an error status
        page.on("response", self._saw)

    def _saw(self, response: Response) -> None:
        if origin(response.url) != self.origin:
            return
        path = urlsplit(response.url).path
        if path == USERINFO:
            self.userinfo_status = response.status
        if response.status >= 400:
            self.failed.append(f"{response.request.method} {path} {response.status}")

    def _diagnosis(self) -> str:
        """Same-origin requests that failed, to explain a page that is stuck."""
        if not self.failed:
            return ""
        return "; failed requests: " + ", ".join(dict.fromkeys(self.failed))

    def _login_page(self) -> str:
        """The login page the browser went to, or ""."""
        for url in (*self.followed, self.page.url):
            parts = urlsplit(url)
            if origin(url) != self.origin or parts.path.startswith(LOGIN_PATHS):
                return f"{parts.netloc}{parts.path}"
        return ""

    def _check_logged_in(self) -> None:
        if self.userinfo_status == 401:
            raise logged_out(f"{USERINFO} answered HTTP 401")
        login = self._login_page()
        if login:
            raise logged_out(f"it went to the login page {login}")

    def _wait(self, locator: Locator, what: str, timeout_ms: float) -> None:
        from playwright.sync_api import Error

        try:
            locator.first.wait_for(state="visible", timeout=timeout_ms)
        except Error:
            self._check_logged_in()
            raise CheckError(
                f"{what} within {timeout_ms / 1000:g}s{self._diagnosis()}"
            ) from None

    def open(self, path: str, instrument_session: str) -> None:
        """Open a page of the UI in ``instrument_session`` and check it is logged in."""
        self.page.add_init_script(
            f"window.localStorage.setItem({json.dumps(SESSION_KEY)}, "
            f"{json.dumps(json.dumps(instrument_session))});"
        )
        response = self.page.goto(
            f"{self.url}{path}", timeout=self.timeout_ms, wait_until="load"
        )
        self._check_logged_in()
        if response is not None and response.status in (401, 403):
            raise logged_out(f"GET {path} answered HTTP {response.status}")
        if response is not None and response.status >= 400:
            raise CheckError(f"GET {path} on the UI returned HTTP {response.status}")

        login = self.page.get_by_role("button", name="Login", exact=True)
        user = self.page.get_by_role("button", name="User Avatar")
        self._wait(
            user.or_(login), "the UI did not show who is logged in", self.timeout_ms
        )
        if login.is_visible():
            raise logged_out(
                f"it offers a Login button; {USERINFO} answered "
                f"HTTP {self.userinfo_status}"
            )
        self._check_logged_in()
        log.info("UI %s is logged in", path)

        session = self.page.get_by_role("button", name=instrument_session, exact=True)
        self._wait(
            session,
            f"the UI did not select instrument session {instrument_session}",
            self.timeout_ms,
        )

    def run_plan(self, plan: str, deadline_ms: float) -> tuple[str, dict[str, Any]]:
        """Press Run, wait for the UI to say the plan finished: (task id, request).

        ``deadline_ms`` is how long the UI may take to report the outcome.
        """
        from playwright.sync_api import Error, expect

        run = self.page.get_by_role("button", name="Run", exact=True)
        self._wait(run, "the UI did not show its Run button", self.timeout_ms)
        try:
            expect(run).to_be_enabled(timeout=self.timeout_ms)
        except AssertionError:
            self._check_logged_in()
            raise CheckError(
                f"the Run button stayed disabled for {self.timeout_ms / 1000:g}s: "
                f"the UI does not see the blueapi worker IDLE{self._diagnosis()}"
            ) from None

        def submission(response: Response) -> bool:
            return (
                response.request.method == "POST"
                and origin(response.url) == self.origin
                and urlsplit(response.url).path == SUBMIT
            )

        try:
            with self.page.expect_response(
                submission, timeout=self.timeout_ms
            ) as submitted:
                run.click()
        except Error:
            self._check_logged_in()
            raise CheckError(
                f"pressing Run did not submit {plan} to blueapi{self._diagnosis()}"
            ) from None
        response = submitted.value
        request = response.request.post_data_json or {}
        if response.status != 201:
            self._check_logged_in()
            raise CheckError(
                f"the UI's submission of {plan} got HTTP {response.status}"
                f"{self._diagnosis()}"
            )
        try:
            task_id = response.json()["task_id"]
        except (Error, ValueError, KeyError, TypeError):
            raise CheckError(
                "the UI's POST /api/blueapi/tasks has no task_id"
            ) from None
        if request.get("name") != plan:
            raise CheckError(f"the UI submitted {request.get('name')!r}, not {plan!r}")
        log.info(
            "UI submitted %s as task %s with %s in %s",
            plan,
            task_id,
            request.get("params"),
            request.get("instrument_session"),
        )

        succeeded = self.page.get_by_text("Plan succeeded", exact=True)
        failed = self.page.get_by_text(
            re.compile(r"^(Failed to run plan|Plan submission failed)")
        )
        try:
            succeeded.or_(failed).first.wait_for(state="visible", timeout=deadline_ms)
        except Error:
            raise PlanTimeoutError(task_id) from None
        if not succeeded.is_visible():
            raise CheckError(
                f"the UI reported {failed.first.inner_text()!r} for task {task_id}"
            )
        log.info("UI reports task %s succeeded", task_id)
        return task_id, request

    def stop_tasks(self) -> list[str]:
        """Stop the page starting any more tasks: the ones it started."""
        return self.starts.stop()

    def screenshot(self, path: Path) -> None:
        """Best effort: the page as it is now, to explain a failure."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=path, full_page=True)
            log.info("saved a screenshot of the UI to %s", path)
        except Exception as error:  # noqa: BLE001 - never mask the real failure
            log.warning("could not save a screenshot: %s", error)


def _redirected(
    response: APIResponse, url: str, method: str, body: bytes | None
) -> tuple[str, str, bytes | None] | None:
    """Where a browser goes next after ``response``: (url, method, body)."""
    location = response.headers.get("location")
    if response.status not in REDIRECTS or not location:
        return None
    if (response.status == 303 and method != "HEAD") or (
        response.status in (301, 302) and method == "POST"
    ):
        method, body = "GET", None
    return urljoin(url, location), method, body


def _fetch_on_ui(
    route: Route,
    api: APIRequestContext,
    ui_origin: str,
    headers: dict[str, str],
    followed: list[str],
) -> APIResponse:
    """The UI's response to the routed request, sent with ``headers``.

    ``headers`` carry the bearer, so they go to the UI's origin only.

    Redirects within the UI's origin are followed here, each hop with the
    bearer, and the browser gets the final response. A redirect to any other
    origin is handed to the browser, which follows it without the bearer: the
    route never sees that hop, and the browser itself never had the token.
    The page keeps the URL it asked for, so a page's redirects are recorded in
    ``followed``.
    """
    request = route.request
    url, method, body = request.url, request.method, request.post_data_buffer
    response = route.fetch(headers=headers, max_redirects=0)
    for _ in range(MAX_REDIRECTS):
        hop = _redirected(response, url, method, body)
        if hop is None or origin(hop[0]) != ui_origin:
            break
        if hop[1] != method:  # the body is dropped: so are its headers
            headers = {
                k: v
                for k, v in headers.items()
                if k.lower() not in ("content-type", "content-length")
            }
        url, method, body = hop
        if request.is_navigation_request():
            followed.append(url)
        response = api.fetch(
            url, method=method, headers=headers, data=body, max_redirects=0
        )
    return response


def _authoriser(
    api: APIRequestContext,
    ui_origin: str,
    token: str,
    nonce: str,
    starts: TaskStarts,
    followed: list[str],
) -> Callable[[Route], None]:
    """The route for every request of the page: adds the bearer on the UI only."""
    from playwright.sync_api import Error

    def authorise(route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        try:
            headers = with_bearer(request.url, request.headers, ui_origin, token)
            if headers is None:
                route.continue_()
            elif not starts.allow(request.method, path, lambda: _json(request)):
                route.abort()
            elif headers.pop(STREAM_HEADER, None) == nonce:
                # The page's own event stream (STREAMS): fetch() with redirect
                # "error", so the browser follows no redirect with the bearer
                route.continue_(headers=headers)
            elif request.resource_type == "eventsource":
                # Any other event stream: the browser would follow a redirect
                # with the bearer, so it goes without
                route.continue_()
            else:
                route.fulfill(
                    response=_fetch_on_ui(route, api, ui_origin, headers, followed)
                )
        except Error as error:
            # Playwright's message logs the request headers, so the token:
            # report only what failed
            log.warning(
                "request %s %s failed in the browser: %s",
                request.method,
                path,
                type(error).__name__,
            )
            try:
                route.abort()
            except Error:
                pass  # the page or browser is already gone

    return authorise


def _json(request: Any) -> Any:
    """The request's JSON body, or None."""
    try:
        return request.post_data_json
    except Exception:  # noqa: BLE001 - not JSON
        return None


@contextmanager
def open_ui(url: str, token: str, timeout: float = UI_TIMEOUT) -> Iterator[Ui]:
    """A fresh headless Chromium page on the UI, sending ``token`` to it only."""
    from playwright.sync_api import sync_playwright

    nonce = secrets.token_urlsafe(16)
    starts = TaskStarts()
    followed: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            # Service workers could answer requests without passing the route.
            # Pages fulfilled by the route count as public to Chromium, so
            # their requests to private or loopback addresses (Diamond hosts)
            # need the local network permission a user would be asked for.
            context = browser.new_context(
                service_workers="block", permissions=["local-network-access"]
            )
            context.add_init_script(
                STREAMS
                % {"mark": json.dumps(STREAM_HEADER), "nonce": json.dumps(nonce)}
            )
            context.route(
                "**/*",
                _authoriser(
                    context.request, origin(url), token, nonce, starts, followed
                ),
            )
            try:
                yield Ui(context.new_page(), url, timeout, starts, followed)
            finally:
                # Requests still in flight (polls, the event stream) must not
                # fail into Playwright's error output, which logs headers
                context.unroute_all(behavior="ignoreErrors")
        finally:
            browser.close()
