"""Session and refresh-token lifecycle — SEC-JWT-001…005, 009; FR-AUTH-011/012.

**The refresh token is an opaque random string, not a JWT.** SEC-JWT-002 requires
it to be "stored hashed in ``user_sessions``", and the schema decides the rest:
``refresh_token_hash`` is the only column that identifies a session, so it must be
*deterministic* — otherwise a presented token cannot be looked up at all, only
compared against every row in the table. A salted password hash cannot do that. So
the digest is SHA-256 with a domain-separation prefix.

SHA-256 rather than Argon2 here, unlike passwords, PINs and OTP codes, and the
reason is entropy rather than convenience. The token is 384 bits from ``secrets``:
there is no dictionary to run, so a work factor buys nothing, and Argon2 on an
unauthenticated endpoint would let anyone spend the server's CPU a few hundred
milliseconds at a time. The three values that *are* low-entropy — a password, a
4-digit PIN, a 6-digit code — take the slow hash. This one does not need it.

**Rotation and reuse detection are one mechanism.** SEC-JWT-003 revokes the old
row as it issues a new one, which leaves a fact in the database that would
otherwise be unrecorded: *this token was already spent*. SEC-JWT-004 is what makes
that fact worth recording. Only a token revoked by rotation triggers reuse
detection. Explicit logout, expiry and device revocation remain terminal
rejections. Each login has its own family UUID, retained across rotations, so
an old token cannot revoke a later login on the same device (C6).

**Access tokens are not minted here.** SEC-JWT-001's access token is a
short-lived JWT signed with an asymmetric key, and that key is environment
configuration with a keypair behind it (SEC-SEC-001 forbids a default). Minting
one belongs in ``services/tokens.py``, where the RSA keypair is validated (S4).
This module owns the half of the lifecycle the database holds, which is the half
that has to be right.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.accounts.constants import SessionKind, SessionRevocationReason
from apps.accounts.models import Device, User, UserSession
from apps.accounts.services.errors import InvalidRefreshToken, RefreshTokenReuse

__all__ = [
    "ACCESS_TTL",
    "REFRESH_TTL",
    "IssuedSession",
    "issue",
    "revoke",
    "revoke_all_for_user",
    "revoke_device_sessions",
    "revoke_session_family",
    "rotate",
]

logger = logging.getLogger(__name__)

REFRESH_TTL = timedelta(days=30)
"""SEC-JWT-002: "Long-lived refresh token (30 days)"."""

ACCESS_TTL = timedelta(minutes=15)
"""SEC-JWT-001: "Short-lived access token (15 min default)".

Declared here so the two windows are read together and cannot drift apart; the
token itself is minted in S4, where the signing key lives.
"""

_TOKEN_BYTES = 48
"""48 bytes → 64 URL-safe characters, 384 bits of entropy.

Not a length that needs arguing about: ``secrets.token_urlsafe`` output is
uniform, and the only requirement is that guessing is not a strategy.
"""

_HASH_DOMAIN = b"ihweza.refresh.v1:"
"""Domain separation, so that a refresh token and any other value this project
ever hashes cannot produce the same digest for the same input string. Cheap now,
impossible to retrofit once a digest is in a production table.
"""

USER_AGENT_MAX_LENGTH = 512
"""Mirrors the column. Django does not truncate on save — PostgreSQL raises — so a
600-character User-Agent would abort a login at the last statement, which is
exactly the failure ``apps.audit`` truncates to avoid."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new refresh token and the row that records it.

    ``refresh_token`` is excluded from ``repr``: this object is created inside an
    authentication request, which is precisely where an unexpected exception
    produces a traceback containing it. A token in a log file is a session
    anybody with log access can resume (10 §5.2, S1).
    """

    refresh_token: str = field(repr=False)
    session: UserSession
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"IssuedSession(public_id={self.session.public_id!r}, "
            f"expires_at={self.expires_at!r}, refresh_token=<redacted>)"
        )


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _digest(token: str) -> str:
    """The lookup value for a refresh token. Deterministic by necessity."""
    return hashlib.sha256(_HASH_DOMAIN + token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return timezone.now()


def _lock_user(user_id: int) -> User:
    """Serialize issuance, rotation and revocation in the same lock order.

    Locking different predecessor rows first lets two replay requests deadlock
    while each tries to revoke the other's successor. The user row comes first
    in every write path; refresh frequency makes this a small per-user cost.
    """
    try:
        return User.objects.select_for_update().get(pk=user_id)
    except User.DoesNotExist:
        raise InvalidRefreshToken() from None


def _create_row(
    *,
    user: User,
    device: Device | None,
    ip_address: str | None,
    user_agent: str | None,
    now: datetime,
    family_id: uuid.UUID | None = None,
    mfa_verified: bool = False,
    mfa_version: int | None = None,
) -> tuple[UserSession, str]:
    """Insert a session row and return it with the plaintext token."""
    token = _new_token()
    session = UserSession.objects.create(
        user=user,
        device=device,
        family_id=family_id or uuid.uuid4(),
        mfa_verified=mfa_verified,
        mfa_version=user.mfa_version if mfa_version is None else mfa_version,
        authorization_version=user.authorization_version,
        refresh_token_hash=_digest(token),
        issued_at=now,
        expires_at=now + REFRESH_TTL,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:USER_AGENT_MAX_LENGTH],
    )
    return session, token


def issue(
    *,
    user: User,
    device: Device | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    mfa_verified: bool = False,
) -> IssuedSession:
    """Start a session: mint a refresh token and record it hashed.

    ``device`` is optional because the web admin has no device record and uses
    Django sessions instead (02 §5.7). It is *not* optional in practice for
    mobile: SEC-JWT-005 binds tokens to a device so that revoking the device
    revokes its sessions, and a session issued without one cannot be revoked that
    way. A caller on the mobile path that passes nothing here has lost a control,
    not saved a step.

    Returns the plaintext token exactly once. Nothing stores it, and no later call
    can recover it — a lost token is a re-authentication, not a lookup.
    """
    now = _now()
    with transaction.atomic():
        current_user = _lock_user(user.pk)
        if not current_user.is_active:
            raise InvalidRefreshToken("This account cannot start a session.")
        if device is not None:
            current_device = Device.objects.filter(pk=device.pk, user=current_user).first()
            if current_device is None or current_device.is_revoked:
                raise InvalidRefreshToken("This device cannot start a session.")
            device = current_device
        session, token = _create_row(
            user=current_user,
            device=device,
            ip_address=ip_address,
            user_agent=user_agent,
            now=now,
            mfa_verified=mfa_verified,
        )

    logger.info(
        "session.issued",
        extra={
            "session_public_id": str(session.public_id),
            "user_public_id": str(user.public_id),
            "device_id": device.pk if device else None,
        },
    )
    return IssuedSession(refresh_token=token, session=session, expires_at=session.expires_at)


def rotate(
    *,
    refresh_token: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IssuedSession:
    """Exchange a refresh token for a new one (SEC-JWT-003/004).

    The old row is revoked and a new one written in the same transaction, so
    there is no instant at which a crash leaves two live tokens for one session.
    The new row **inherits the device** from the old one: SEC-JWT-005 binds a
    session to a device, and a rotation that let the binding follow the caller
    would let a stolen token re-bind itself to the thief's device and survive the
    victim revoking the original.

    ``ip_address`` and ``user_agent`` are recorded fresh rather than inherited —
    they describe *this* request, and a change of address across rotations is a
    signal SEC-LOCK-004 wants to be able to see.

    Raises:
        RefreshTokenReuse: the token was already spent. The session family has
            been revoked by the time this is raised.
        InvalidRefreshToken: the token is unknown, expired, or its device has
            been revoked.
    """
    now = _now()
    digest = _digest(refresh_token)

    # The refusal is raised *after* the transaction commits, never inside it.
    # Three of the four branches below write before they refuse — the family
    # revocation and the two "mark this session revoked" updates — and raising
    # inside ``atomic()`` would roll exactly those writes back, leaving a
    # detected theft's tokens live while telling the caller it was detected.
    failure: InvalidRefreshToken | None = None
    family_revoked = 0
    successor: UserSession | None = None
    token = ""

    user_id = (
        UserSession.objects.filter(refresh_token_hash=digest)
        .values_list("user_id", flat=True)
        .first()
    )
    if user_id is None:
        logger.warning("session.refresh_unknown")
        raise InvalidRefreshToken("That refresh token is not in use.")

    with transaction.atomic():
        current_user = _lock_user(user_id)
        # `of=("self",)` avoids locking the nullable side of the device join.
        session = (
            UserSession.objects.select_for_update(of=("self",))
            .select_related("device", "user")
            .filter(refresh_token_hash=digest, user=current_user)
            .first()
        )

        if session is None:
            # No row: a guessed or fabricated token, or one from a wiped database.
            logger.warning("session.refresh_unknown")
            raise InvalidRefreshToken("That refresh token is not in use.")

        if session.kind != SessionKind.TOKEN:
            failure = InvalidRefreshToken()
        elif session.authorization_version != current_user.authorization_version:
            failure = InvalidRefreshToken()
        elif session.revoked_at is not None:
            if session.revoked_reason in ("", SessionRevocationReason.ROTATED):
                # Empty reasons preserve the pre-migration replay policy.
                family_revoked = _revoke_rows(
                    UserSession.objects.filter(user=current_user, family_id=session.family_id),
                    now=now,
                    reason=SessionRevocationReason.TOKEN_REUSE,
                    preserve_rotated=True,
                )
                failure = RefreshTokenReuse(
                    "A refresh token that had already been rotated was presented."
                )
            else:
                failure = InvalidRefreshToken()
        elif not current_user.is_active:
            session.revoked_at = now
            session.revoked_reason = SessionRevocationReason.ACCOUNT_INACTIVE
            session.save(update_fields=["revoked_at", "revoked_reason"])
            failure = InvalidRefreshToken()
        elif session.expires_at <= now:
            session.revoked_at = now
            session.revoked_reason = SessionRevocationReason.EXPIRED
            session.save(update_fields=["revoked_at", "revoked_reason"])
            failure = InvalidRefreshToken("That refresh token has expired.")
        elif session.device is not None and session.device.is_revoked:
            # SEC-JWT-005 / FR-AUTH-012: the device was revoked, so its sessions
            # are over regardless of what the token says.
            session.revoked_at = now
            session.revoked_reason = SessionRevocationReason.DEVICE_REVOKED
            session.save(update_fields=["revoked_at", "revoked_reason"])
            failure = InvalidRefreshToken("That device has been revoked.")
        else:
            session.revoked_at = now
            session.revoked_reason = SessionRevocationReason.ROTATED
            session.save(update_fields=["revoked_at", "revoked_reason"])

            successor, token = _create_row(
                user=session.user,
                device=session.device,
                ip_address=ip_address,
                user_agent=user_agent,
                now=now,
                family_id=session.family_id,
                mfa_verified=session.mfa_verified,
                mfa_version=session.mfa_version,
            )

    if failure is not None:
        if isinstance(failure, RefreshTokenReuse):
            # This event is the one SEC-AUD-005 alerts on. C5 has no separate
            # security sink yet, so this line is the interim — see PHASE_1_PLAN.
            logger.warning(
                "session.refresh_reuse_detected",
                extra={
                    "session_public_id": str(session.public_id),
                    "user_public_id": str(session.user.public_id),
                    "device_id": session.device_id,
                    "family_sessions_revoked": family_revoked,
                },
            )
        else:
            logger.info(
                "session.refresh_refused",
                extra={
                    "session_public_id": str(session.public_id),
                    "reason": session.revoked_reason,
                },
            )
        raise failure

    assert successor is not None  # the success branch is the only one without a failure
    logger.info(
        "session.rotated",
        extra={
            "session_public_id": str(successor.public_id),
            "predecessor_public_id": str(session.public_id),
            "user_public_id": str(session.user.public_id),
        },
    )
    return IssuedSession(refresh_token=token, session=successor, expires_at=successor.expires_at)


def _revoke_rows(
    rows: QuerySet[UserSession],
    *,
    now: datetime,
    reason: str,
    preserve_rotated: bool = False,
) -> int:
    """Caller holds the user lock. Count live rows and retain original timestamps."""
    count = rows.filter(revoked_at__isnull=True).update(revoked_at=now, revoked_reason=reason)
    if not preserve_rotated:
        rows.filter(
            Q(revoked_reason=SessionRevocationReason.ROTATED)
            | Q(revoked_reason="", revoked_at__isnull=False)
        ).update(revoked_reason=reason)
    return count


def revoke(session: UserSession, *, now: datetime | None = None) -> bool:
    """End one session. Returns whether this call is what ended it.

    Idempotent, and the return value distinguishes the two cases so a caller can
    tell "signed out" from "was already signed out" without a second query — the
    difference matters in a logout endpoint's response, and does not matter
    anywhere else.
    """
    with transaction.atomic():
        _lock_user(session.user_id)
        count = _revoke_rows(
            UserSession.objects.filter(pk=session.pk, user_id=session.user_id),
            now=now or _now(),
            reason=SessionRevocationReason.REVOKED,
        )
        session.refresh_from_db()
    return bool(count)


def revoke_session_family(
    session: UserSession,
    *,
    now: datetime | None = None,
    reason: str = SessionRevocationReason.REVOKED,
) -> int:
    """End this login's rotation chain, including its old replay markers.

    A later login on the same device has another family UUID and survives.
    Clearing old ROTATED reasons prevents logout from raising a theft alert.
    """
    with transaction.atomic():
        _lock_user(session.user_id)
        return _revoke_rows(
            UserSession.objects.filter(user_id=session.user_id, family_id=session.family_id),
            now=now or _now(),
            reason=reason,
        )


def revoke_all_for_user(user: User, *, now: datetime | None = None) -> int:
    """End every session this member holds. Returns the count.

    SEC-JWT-009: logout on every device, a password change and a PIN change all
    invalidate the session family. A credential change that left other sessions
    alive would leave whoever knew the old password signed in — which is the
    specific thing the requirement prevents.
    """
    with transaction.atomic():
        _lock_user(user.pk)
        count = _revoke_rows(
            UserSession.objects.filter(user=user),
            now=now or _now(),
            reason=SessionRevocationReason.CREDENTIAL_CHANGED,
        )
    logger.info(
        "session.all_revoked",
        extra={"user_public_id": str(user.public_id), "sessions_revoked": count},
    )
    return count


def revoke_device_sessions(device: Device, *, now: datetime | None = None) -> int:
    """End every session bound to a device. Returns the count.

    FR-AUTH-012 / SEC-JWT-005. Separate from :func:`revoke_all_for_user` because
    losing a phone is not a reason to sign the member out everywhere — the whole
    value of device binding is that the response can be proportionate.
    """
    with transaction.atomic():
        _lock_user(device.user_id)
        count = _revoke_rows(
            UserSession.objects.filter(device=device, user_id=device.user_id),
            now=now or _now(),
            reason=SessionRevocationReason.DEVICE_REVOKED,
        )
    logger.info(
        "session.device_revoked",
        extra={"device_id": device.pk, "sessions_revoked": count},
    )
    return count
