"""A real HTTP server on localhost, scriptable per test."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from harvester.engine import Settings

Response = tuple[int, dict[str, str], bytes]
Handler = Callable[["Hit"], Response]


@dataclass
class Hit:
    path: str
    headers: dict[str, str]


@dataclass
class Site:
    base: str
    routes: dict[str, Handler] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)

    def route(self, path: str, body: str | bytes = "", status: int = 200, **headers: str) -> None:
        data = body.encode() if isinstance(body, str) else body
        hdrs = {"Content-Type": "text/html; charset=utf-8", **headers}
        self.routes[path] = lambda hit: (status, hdrs, data)

    def handler(self, path: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.routes[path] = fn
            return fn

        return register

    def url(self, path: str) -> str:
        return self.base + path

    def paths(self) -> list[str]:
        return [h.path for h in self.hits]


@pytest.fixture
def site() -> Iterator[Site]:
    state = Site(base="")

    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hit = Hit(self.path, {k.lower(): v for k, v in self.headers.items()})
            state.hits.append(hit)
            fn = state.routes.get(self.path)
            status, headers, body = fn(hit) if fn else (404, {}, b"not found")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", concurrency=2, delay=0.0, max_attempts=4)


FIXTURES = Path(__file__).parent / "fixtures"
