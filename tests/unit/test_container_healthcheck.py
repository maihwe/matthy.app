"""Container readiness must reject redirects, dependency failures and liveness."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from docker import healthcheck

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("status", "body", "redirect", "expected"),
    [
        (200, b'{"status":"ok","check":"ready"}', False, True),
        (503, b'{"status":"unavailable","check":"ready"}', False, False),
        (200, b'{"status":"ok","check":"live"}', False, False),
        (200, b'{"status":"ok","check":"ready"}', True, False),
        (200, b"<html>sign in</html>", False, False),
        (200, b'{"status":"unavailable","check":"ready"}', False, False),
    ],
)
def test_readiness_uses_actual_response_and_ignores_http_proxy(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    redirect: bool,
    expected: bool,
) -> None:
    requests: list[tuple[str | None, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.headers.get("Host"), self.headers.get("X-Forwarded-Proto")))
            if redirect and self.path == "/health/ready/":
                self.send_response(302)
                self.send_header("Location", "/other/")
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/health/ready/"
        assert healthcheck.probe(url, "staging.example.test") is expected
        assert requests[0] == ("staging.example.test", "https")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://example.com/health/ready/",
        "http://127.0.0.1:8000/health/live/",
        "http://127.0.0.1:8000/health/ready/?token=unexpected",
    ],
)
def test_probe_refuses_non_loopback_or_non_readiness_urls(url: str) -> None:
    assert not healthcheck.probe(url, "staging.example.test")


@pytest.mark.parametrize("host", ["", "*", ".example.test"])
def test_probe_requires_a_concrete_allowed_host(host: str) -> None:
    assert not healthcheck.probe("http://127.0.0.1:8000/health/ready/", host)


@pytest.mark.parametrize("port", ["invalid", "0", "65536"])
def test_main_refuses_invalid_ports(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("PORT", port)
    assert healthcheck.main() == 1
