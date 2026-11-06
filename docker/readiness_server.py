"""Internal ALB probe port; public requests continue through Django on port 8000."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from docker.healthcheck import main as check_readiness


class ReadinessHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health/ready/":
            self.send_error(404)
            return
        ready = check_readiness() == 0
        body = b'{"status":"ok","check":"ready"}' if ready else b'{"status":"unavailable"}'
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # No client-controlled paths/headers or application secrets in probe logs.
        pass


def main() -> None:
    # Security groups allow this port only from the ALB. Its listener targets 8000.
    with ThreadingHTTPServer(("0.0.0.0", 8001), ReadinessHandler) as server:  # noqa: S104
        server.serve_forever()


if __name__ == "__main__":
    main()
