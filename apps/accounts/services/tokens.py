"""RS256 access tokens with UUID claims and environment-supplied RSA keys."""

from __future__ import annotations

import uuid
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import UserSession
from apps.accounts.services.errors import AuthenticationUnavailable, InvalidAccessToken
from apps.accounts.services.sessions import ACCESS_TTL


def _verifying_key() -> RSAPublicKey:
    try:
        key = serialization.load_pem_public_key(settings.JWT_VERIFYING_KEY.encode("utf-8"))
        if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
            raise ValueError("RSA keys must contain at least 2048 bits.")
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise InvalidAccessToken() from None
    return key


def decode_access(token: str) -> dict[str, Any]:
    """Validate the signature, expiry, issuer and required claims before any lookup."""
    try:
        claims = jwt.decode(
            token,
            _verifying_key(),
            algorithms=["RS256"],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={"require": ["exp", "iat", "jti", "sub", "sid", "token_type"]},
        )
        if claims["token_type"] != "access":  # noqa: S105 -- public protocol label
            raise InvalidAccessToken()
    except (jwt.PyJWTError, ValueError, TypeError):
        raise InvalidAccessToken() from None
    return claims


def issue_access(session: UserSession) -> str:
    """Sign only with a matching RSA keypair; key failures never expose key material."""
    stamp = timezone.now()
    try:
        key = serialization.load_pem_private_key(
            settings.JWT_SIGNING_KEY.encode("utf-8"), password=None
        )
        public_key = _verifying_key()
        if (
            not isinstance(key, RSAPrivateKey)
            or key.key_size < 2048
            or key.public_key().public_numbers() != public_key.public_numbers()
        ):
            raise ValueError("The RSA signing and verifying keys must match.")
        token = jwt.encode(
            {
                "sub": str(session.user.public_id),
                "sid": str(session.public_id),
                "iat": int(stamp.timestamp()),
                "exp": int((stamp + ACCESS_TTL).timestamp()),
                "jti": str(uuid.uuid4()),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "token_type": "access",
            },
            key,
            algorithm="RS256",
        )
        decode_access(token)
    except (
        jwt.PyJWTError,
        ValueError,
        TypeError,
        UnsupportedAlgorithm,
        InvalidAccessToken,
    ):
        raise AuthenticationUnavailable() from None
    return token
