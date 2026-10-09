"""A fake ViSR deployment for the UI check: the built SPA plus fake backends.

One HTTP server stands in for the UI's origin, as oauth2-proxy would present
it: it serves a built ViSR SPA (``dist``) and answers /oauth2/userinfo,
/auth/me, /api/blueapi, /api/supergraph and /api/data/events. Like oauth2-proxy
with ``skip_jwt_bearer_tokens``, it wants ``Authorization: Bearer <TOKEN>`` on
/oauth2/userinfo, /auth/me and /api/*. It also answers the token endpoint and
blueapi's /api/v1 API, under /blueapi, for the check's own HTTP client.

A second server is "another origin" (the identity provider, the PV camera
stream): /oauth2/start redirects to it, and the SPA's index.html is given an
image from it. It records any Authorization header it is sent.

For the browser's own tests the UI also has /redirect (302, or ``code``, to
``to``), /blank (an empty page) and /stream (an event stream of one "hello").
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

TOKEN = "t0ken-for-the-ui-check"
CLIENT_ID = "visr-tests-ui"
CLIENT_SECRET = "s3cret-ui-client"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class FakeVisr:
    """Both fake servers and the state of the fake blueapi worker."""

    def __init__(self, dist: Path | None, scan_seconds: float = 2.0):
        self.dist = dist
        self.scan_seconds = scan_seconds
        self.lock = threading.Lock()
        # what the test can change
        self.accept_bearer = True  # False: as oauth2-proxy without the flags
        self.worker = "IDLE"
        self.complete_scans = True  # False: a scan never finishes
        self.fail_state_when_running = False  # True: the UI's polls get 500
        self.foreign_task_on_submit = False  # True: another task starts, 500
        # what happened
        self.ui_requests: list[tuple[str, str, bool]] = []  # method, path, bearer
        self.other_requests: list[tuple[str, str, str | None]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.workflows: list[dict[str, Any]] = []
        self.aborts: list[Any] = []
        self.events: list[dict[str, Any]] = []
        self._running: str | None = None
        self._started = 0.0

        self.ui = Server(("127.0.0.1", 0), self._handler(self._ui))
        self.other = Server(("127.0.0.1", 0), self._handler(self._other))
        self.ui_url = f"http://127.0.0.1:{self.ui.server_address[1]}"
        self.other_url = f"http://localhost:{self.other.server_address[1]}"
        self._threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (self.ui, self.other)
        ]

    def __enter__(self) -> FakeVisr:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for server in (self.ui, self.other):
            server.shutdown()
            server.server_close()

    # --- the worker

    def _tick(self) -> None:
        """Finish the running scan once it has run for scan_seconds."""
        with self.lock:
            task_id = self._running
            if task_id is None or not self.complete_scans:
                return
            if time.monotonic() - self._started < self.scan_seconds:
                return
            task = self.tasks[task_id]
            task.update(is_complete=True, is_pending=False)
            task["outcome"] = {"outcome": "success", "result": None, "type": None}
            self._running = None
            self.worker = "IDLE"
            self.events.append(self._event("finished", task_id))

    def _event(self, status: str, task_id: str) -> dict[str, Any]:
        return {
            "status": status,
            "filepath": f"/data/{task_id}.nxs",
            "snake": False,
            "uuid": task_id,
        }

    def _submit(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        with self.lock:
            self.submitted.append(body)
            self.tasks[task_id] = {
                "task_id": task_id,
                "task": {"name": body.get("name"), "params": body.get("params")},
                "request_id": None,
                "is_complete": False,
                "is_pending": True,
                "errors": [],
                "outcome": None,
            }
        return {"task_id": task_id}

    def _start(self, task_id: str) -> tuple[int, Any]:
        with self.lock:
            if task_id not in self.tasks:
                return 404, {"detail": "no such task"}
            if self.worker != "IDLE":
                return 409, {"detail": "worker busy"}
            self.worker = "RUNNING"
            self._running = task_id
            self._started = time.monotonic()
            self.tasks[task_id]["is_pending"] = False
            self.events.append(self._event("running", task_id))
        return 200, {"task_id": task_id}

    def _abort(self, body: Any) -> tuple[int, Any]:
        with self.lock:
            self.aborts.append(body)
            if self._running:
                task = self.tasks[self._running]
                task.update(is_complete=True, is_pending=False)
                task["outcome"] = {"outcome": "error", "type": "Abort", "message": ""}
                self._running = None
            self.worker = "IDLE"
        return 202, "ABORTING"

    def _blueapi(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        """blueapi's API; ``path`` is relative to its root, without /api/v1."""
        self._tick()
        if (method, path) == ("GET", "/worker/state"):
            if self.fail_state_when_running and self.worker == "RUNNING":
                return 500, {"detail": "worker state unavailable"}
            return 200, self.worker
        if (method, path) == ("PUT", "/worker/state"):
            return self._abort(body)
        if (method, path) == ("GET", "/worker/task"):
            return 200, {"task_id": self._running}
        if (method, path) == ("PUT", "/worker/task"):
            return self._start(body["task_id"])
        if (method, path) == ("POST", "/tasks"):
            if self.foreign_task_on_submit:
                foreign = self._submit({"name": "someone_elses_plan"})["task_id"]
                self.submitted.pop()
                self._start(foreign)
                return 500, {"detail": "submission failed"}
            return 201, self._submit(body)
        if method == "GET" and path.startswith("/tasks/"):
            task = self.tasks.get(path.removeprefix("/tasks/"))
            return (200, task) if task else (404, {"detail": "no such task"})
        if (method, path) == ("GET", "/plans"):
            return 200, {"plans": []}
        return 404, {"detail": "not found"}

    def _graphql(self, body: dict[str, Any]) -> tuple[int, Any]:
        query = body.get("query", "")
        if "submitWorkflowTemplate" in query:
            with self.lock:
                self.workflows.append(body.get("variables", {}))
            return 200, {"data": {"submitWorkflowTemplate": {"name": "visr-recon-1"}}}
        if "instrumentByKey" in query:
            node = {"instrumentSessionReference": "CM99999-9"}
            return 200, {
                "data": {
                    "instrumentByKey": {
                        "instrumentSessions": {"edges": [{"node": node}]}
                    }
                }
            }
        return 200, {"data": None, "errors": [{"message": "unknown query"}]}

    # --- HTTP

    def _handler(self, route: Any) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:  # noqa: N802
                route(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                route(self, "POST")

            def do_PUT(self) -> None:  # noqa: N802
                route(self, "PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                route(self, "DELETE")

        return Handler

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    def _json(self, handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
        self._send(handler, status, json.dumps(body).encode(), "application/json")

    def _other(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(handler.path).path
        with self.lock:
            self.other_requests.append(
                (method, path, handler.headers.get("Authorization"))
            )
        if path == "/pixel.gif":
            gif = bytes.fromhex(
                "47494638396101000100800000000000ffffff21f90401000000002c00000000"
                "010001000002024401003b"
            )
            self._send(handler, 200, gif, "image/gif")
        else:
            page = b"<html><body><h1>Sign in to Diamond</h1></body></html>"
            self._send(handler, 200, page, "text/html")

    def _ui(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(handler.path)
        path = parts.path
        authorization = handler.headers.get("Authorization")
        bearer = authorization == f"Bearer {TOKEN}"
        with self.lock:
            self.ui_requests.append((method, path, bearer))
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw) if raw.startswith((b"{", b"[")) else None

        if path == "/redirect":
            query = parse_qs(parts.query)
            code = int(query.get("code", ["302"])[0])
            location = {"Location": query["to"][0]}
            return self._send(handler, code, b"", "text/plain", location)
        if path == "/blank":
            return self._send(handler, 200, b"<html><body></body></html>", "text/html")
        if path == "/stream":
            return self._sse(handler, [{"hello": "world"}])
        if path == "/token" and method == "POST":
            return self._json(handler, 200, {"access_token": TOKEN})
        if path.startswith("/blueapi/api/v1/"):
            # blueapi's own proxy, for the check's HTTP client
            if not bearer:
                return self._json(handler, 401, {"detail": "not authenticated"})
            status, answer = self._blueapi(
                method, path.removeprefix("/blueapi/api/v1"), body
            )
            return self._json(handler, status, answer)
        if path == "/oauth2/start":
            return self._send(
                handler, 302, b"", "text/plain", {"Location": f"{self.other_url}/"}
            )
        protected = path.startswith(("/api/", "/oauth2/", "/auth/"))
        if protected and not (bearer and self.accept_bearer):
            return self._json(handler, 401, {"error": "unauthorized"})
        if path == "/oauth2/userinfo":
            return self._json(
                handler,
                200,
                {
                    "user": "service-account-visr-tests",
                    "email": "visr-tests@example.invalid",
                    "preferred_username": "service-account-visr-tests",
                },
            )
        if path == "/oauth2/sign_in":
            page = b"<html><body><h1>Sign in</h1></body></html>"
            return self._send(handler, 200, page, "text/html")
        if path == "/auth/me":
            return self._send(handler, 200, b"", "text/plain")
        if path.startswith("/api/blueapi/"):
            status, answer = self._blueapi(
                method, path.removeprefix("/api/blueapi"), body
            )
            return self._json(handler, status, answer)
        if path == "/api/supergraph":
            status, answer = self._graphql(body or {})
            return self._json(handler, status, answer)
        if path == "/api/data/events":
            return self._sse(handler)
        if path.startswith("/api/"):
            return self._json(handler, 404, {"detail": "not found"})
        return self._static(handler, path)

    def _sse(
        self, handler: BaseHTTPRequestHandler, first: list[Any] | None = None
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        with self.lock:
            sent = len(self.events)  # only events from now on
        try:
            for event in first or []:
                handler.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            while True:
                self._tick()
                with self.lock:
                    new = self.events[sent:]
                    sent = len(self.events)
                for event in new:
                    handler.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                time.sleep(0.2)
        except OSError:
            return

    def _static(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        assert self.dist is not None, "no built SPA to serve"
        file = (self.dist / path.lstrip("/")).resolve()
        if self.dist.resolve() in file.parents and file.is_file():
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self._send(handler, 200, file.read_bytes(), kind)
        # the SPA's routes, as nginx try_files does; plus an image from another
        # origin, as the tomography page's PV camera stream is
        page = (self.dist / "index.html").read_text()
        image = f'<img alt="" src="{self.other_url}/pixel.gif" hidden>'
        page = page.replace("</body>", f"{image}</body>")
        self._send(handler, 200, page.encode(), "text/html")
