"""Probe readiness over loopback with the configured host and HTTPS proxy marker."""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def probe(url: str, host: str) -> bool:
    if not host or "*" in host or host.startswith("."):
        return False
    try:
        target = urlsplit(url)
        if (
            target.scheme != "http"
            or target.hostname != "127.0.0.1"
            or target.path != "/health/ready/"
            or target.query
            or target.fragment
        ):
            return False
        # Only an HTTP loopback readiness URL is permitted above.
        request = Request(  # noqa: S310
            url, headers={"Host": host, "X-Forwarded-Proto": "https"}
        )
        # Container-level HTTP proxy variables must not route loopback elsewhere.
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            if response.status != 200 or response.geturl() != url:
                return False
            payload = json.load(response)
    except (OSError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("check") == "ready"
    )


def main() -> int:
    host = os.environ.get("HEALTHCHECK_HOST", "").strip()
    if not host:
        host = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",", 1)[0].strip()
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        return 1
    if not 1 <= port <= 65535:
        return 1
    return 0 if probe(f"http://127.0.0.1:{port}/health/ready/", host) else 1


if __name__ == "__main__":
    sys.exit(main())
