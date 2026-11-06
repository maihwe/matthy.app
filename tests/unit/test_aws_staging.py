"""AWS probe behavior and preservation of stable staging credentials."""

from __future__ import annotations

import json
import stat
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from docker import readiness_server
from scripts import aws_staging_secrets

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("healthy", "path", "expected"),
    [
        (True, "/health/ready/", 200),
        (False, "/health/ready/", 503),
        (True, "/health/live/", 404),
        (True, "/health/ready/?redirect=unexpected", 404),
        (True, "/admin/login/", 404),
    ],
)
def test_alb_probe_exposes_only_actual_readiness(
    monkeypatch: pytest.MonkeyPatch, healthy: bool, path: str, expected: int
) -> None:
    monkeypatch.setattr(readiness_server, "check_readiness", lambda: 0 if healthy else 1)
    server = ThreadingHTTPServer(("127.0.0.1", 0), readiness_server.ReadinessHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        client.request("GET", path, headers={"Host": "10.42.10.123:8001"})
        response = client.getresponse()
        assert response.status == expected
        if expected == 200:
            assert json.loads(response.read()) == {"status": "ok", "check": "ready"}
            assert response.getheader("Cache-Control") == "no-store"
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_credentials_are_private_valid_and_preserved_when_configuring_redis(tmp_path: Path) -> None:
    path = tmp_path / "configuration.json"
    aws_staging_secrets.create(path)
    before = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    Fernet(before["MFA_ENCRYPTION_KEYS"])
    private = serialization.load_pem_private_key(before["JWT_SIGNING_KEY"].encode(), password=None)
    public = serialization.load_pem_public_key(before["JWT_VERIFYING_KEY"].encode())
    assert isinstance(private, rsa.RSAPrivateKey)
    assert isinstance(public, rsa.RSAPublicKey)
    assert private.public_key().public_numbers() == public.public_numbers()
    with pytest.raises(FileExistsError):
        aws_staging_secrets.create(path)
    aws_staging_secrets.configure_redis(path, "test.cache.amazonaws.com")
    after = json.loads(path.read_text())
    assert after.pop("REDIS_URL") == (
        f"rediss://:{before['REDIS_AUTH_TOKEN']}@test.cache.amazonaws.com:6379/0?ssl_cert_reqs=required"
    )
    before.pop("REDIS_URL")
    assert after == before


@pytest.mark.parametrize(
    "host", ["", "https://test.example.com", "test.example.com:6379", "test.example.com/path"]
)
def test_redis_configuration_rejects_non_hostnames(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError):
        aws_staging_secrets.configure_redis(tmp_path / "missing.json", host)


def test_redis_configuration_refuses_publicly_readable_secret_files(tmp_path: Path) -> None:
    path = tmp_path / "configuration.json"
    aws_staging_secrets.create(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="readable only"):
        aws_staging_secrets.configure_redis(path, "test.cache.amazonaws.com")


def test_create_refuses_symlinks_and_does_not_overwrite_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("untouched")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        aws_staging_secrets.create(link)
    assert target.read_text() == "untouched"
