"""Write staging credentials to a private local file, never stdout or Git."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def create(path: Path) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    values = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "REDIS_AUTH_TOKEN": secrets.token_hex(32),
        "REDIS_URL": "",
        "JWT_SIGNING_KEY": private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "JWT_VERIFYING_KEY": private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
        "MFA_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "PHONE_VERIFICATION_BACKEND": "",
    }
    # O_EXCL refuses overwrites and existing symlinks; initial mode is private.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(values, output, indent=2)
        output.write("\n")


def configure_redis(path: Path, host: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z]{2,}", host):
        raise ValueError("Expected a Redis DNS hostname, without scheme, port or credentials")
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r+") as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Secret file must be a regular file readable only by its owner")
        values = json.load(output)
        values["REDIS_URL"] = (
            f"rediss://:{values['REDIS_AUTH_TOKEN']}@{host}:6379/0?ssl_cert_reqs=required"
        )
        output.seek(0)
        json.dump(values, output, indent=2)
        output.write("\n")
        output.truncate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "configure-redis"])
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--redis-host")
    args = parser.parse_args()
    if args.action == "create":
        create(args.file)
    else:
        if not args.redis_host:
            parser.error("configure-redis requires --redis-host")
        configure_redis(args.file, args.redis_host)
    print("Private staging configuration saved. Upload via Secrets Manager; do not commit it.")


if __name__ == "__main__":
    main()
