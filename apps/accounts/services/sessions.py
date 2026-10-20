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
that fact worth recording — a presented token whose row is already revoked is not
a retry, it is a copy, and the response is to revoke the whole family.

**Access tokens are not minted here.** SEC-JWT-001's access token is a
short-lived JWT signed with an asymmetric key, and that key is environment
configuration with a keypair behind it (SEC-SEC-001 forbids a default). Minting
one belongs where ``simplejwt`` is wired and the key is read — the API layer, S4.
This module owns the half of the lifecycle the database holds, which is the half
that has to be right.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

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


def _create_row(
    *,
    user: User,
    device: Device | None,
    ip_address: str | None,
    user_agent: str | None,
    now: datetime,
) -> tuple[UserSession, str]:
    """Insert a session row and return it with the plaintext token."""
    token = _new_token()
    session = UserSession.objects.create(
        user=user,
        device=device,
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
    # No `transaction.atomic()` here: issuing a session is one INSERT, and a
    # single statement is its own transaction. Rotation needs one because it
    # revokes and inserts together — see `rotate`.
    session, token = _create_row(
        user=user, device=device, ip_address=ip_address, user_agent=user_agent, now=now
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

    with transaction.atomic():
        # Locked before anything is read from it: two concurrent rotations of the
        # same token are exactly the reuse case, and without the lock both could
        # read `revoked_at is None` and both mint a successor.
        # `of=("self",)` is required, not cosmetic: `select_related("device")` is
        # a LEFT OUTER JOIN because `device` is nullable, and PostgreSQL refuses
        # `FOR UPDATE` on the nullable side of an outer join. Locking only
        # `user_sessions` is also the correct scope — the device row is read, not
        # modified, and a lock on it would serialise rotations across devices
        # sharing an account for no benefit.
        session = (
            UserSession.objects.select_for_update(of=("self",))
            .select_related("device", "user")
            .filter(refresh_token_hash=digest)
            .first()
        )

        if session is None:
            # No row: a guessed or fabricated token, or one from a wiped database.
            logger.warning("session.refresh_unknown")
            raise InvalidRefreshToken("That refresh token is not in use.")

        if session.revoked_at is not None:
            family_revoked = revoke_session_family(session, now=now)
            failure = RefreshTokenReuse(
                "A refresh token that had already been rotated was presented."
            )
        elif session.expires_at <= now:
            session.revoked_at = now
            session.save(update_fields=["revoked_at"])
            failure = InvalidRefreshToken("That refresh token has expired.")
        elif session.device is not None and session.device.is_revoked:
            # SEC-JWT-005 / FR-AUTH-012: the device was revoked, so its sessions
            # are over regardless of what the token says.
            session.revoked_at = now
            session.save(update_fields=["revoked_at"])
            failure = InvalidRefreshToken("That device has been revoked.")
        else:
            session.revoked_at = now
            session.save(update_fields=["revoked_at"])

            successor, token = _create_row(
                user=session.user,
                device=session.device,
                ip_address=ip_address,
                user_agent=user_agent,
                now=now,
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
        elif session.expires_at <= now:
            logger.info(
                "session.refresh_expired",
                extra={"session_public_id": str(session.public_id)},
            )
        else:
            logger.info(
                "session.refresh_device_revoked",
                extra={
                    "session_public_id": str(session.public_id),
                    "device_id": session.device_id,
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


def revoke(session: UserSession, *, now: datetime | None = None) -> bool:
    """End one session. Returns whether this call is what ended it.

    Idempotent, and the return value distinguishes the two cases so a caller can
    tell "signed out" from "was already signed out" without a second query — the
    difference matters in a logout endpoint's response, and does not matter
    anywhere else.
    """
    if session.revoked_at is not None:
        return False
    session.revoked_at = now or _now()
    session.save(update_fields=["revoked_at"])
    return True


def revoke_session_family(session: UserSession, *, now: datetime | None = None) -> int:
    """Revoke every session belonging to the same family. Returns the count.

    **This function is the only definition of "family" in the project**, and that
    is deliberate. SEC-JWT-004 says reuse detection revokes "the entire session
    family" and SEC-JWT-009 says logout, password change, PIN change and role
    revocation invalidate it, but 03's ``user_sessions`` has no lineage column —
    no ``family_id``, no ``replaced_by`` — so a family cannot be *represented*,
    only inferred. It is inferred here as **(user, device)**, justified by
    SEC-JWT-005's device binding: a family is what one device's token chain
    became.

    The inference is wrong in one real case, which is recorded as **C6**: a member
    who signs out on a device and then replays the app's cached refresh token
    looks exactly like a thief, because ``revoked_at`` cannot distinguish "rotated
    away" from "revoked on purpose". Resolving that needs a column (a revocation
    reason, or a lineage pointer), and needs the repository owner. When it is
    resolved, **this is the only function that changes** — which is the entire
    reason it exists as a function rather than as a query written at each call
    site.
    """
    stamp = now or _now()
    # A session with no device is still part of a family: the family key is
    # (user, device), and `None` is a device value like any other. Treating it as
    # "no family" would leave the successor of a device-less rotation live after
    # reuse was detected — which is the one outcome SEC-JWT-004 exists to
    # prevent. The web admin does not reach this path at all: it uses Django
    # sessions (02 §5.7), so device-less rows here are API sessions that never
    # supplied a device.
    if session.device_id is None:
        family = UserSession.objects.filter(
            user_id=session.user_id,
            device_id__isnull=True,
            revoked_at__isnull=True,
        )
    else:
        family = UserSession.objects.filter(
            user_id=session.user_id,
            device_id=session.device_id,
            revoked_at__isnull=True,
        )
    return family.update(revoked_at=stamp)


def revoke_all_for_user(user: User, *, now: datetime | None = None) -> int:
    """End every session this member holds. Returns the count.

    SEC-JWT-009: logout on every device, a password change and a PIN change all
    invalidate the session family. A credential change that left other sessions
    alive would leave whoever knew the old password signed in — which is the
    specific thing the requirement prevents.
    """
    count = UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now or _now()
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
    count = UserSession.objects.filter(device=device, revoked_at__isnull=True).update(
        revoked_at=now or _now()
    )
    logger.info(
        "session.device_revoked",
        extra={"device_id": device.pk, "sessions_revoked": count},
    )
    return count
