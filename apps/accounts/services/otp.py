"""One-time codes — SEC-OTP-001…005, FR-AUTH-006/008/009.

The threat this file is written against is arithmetic. A 6-digit code is 10⁶
possibilities; an attacker who gets unlimited guesses inside a 10-minute window
does not need to break anything, they need to send a million requests. So the
code is not defended by being secret — it is defended by the number of guesses
allowed against it and by how fast it stops being valid:

* **SEC-OTP-003** caps attempts at ``max_attempts`` per code, and reaching the
  cap *consumes* the code rather than merely rejecting the attempt.
* **SEC-OTP-004** caps issuance per destination, because an unbounded OTP
  endpoint is an SMS bill as much as an auth weakness.
* **SEC-OTP-002** expires it. 10 minutes by default; shorter where the code
  authorises something that moves money.

**The plaintext code exists in memory and nowhere else.** It is returned to the
caller so the notification layer can send it, and it is not logged, not stored
(only ``code_hash`` is), and not put in an API response (10 §5.2, and test
obligation S1). ``IssuedOTP`` overrides ``__repr__`` for that reason: a dataclass
that leaks its secret through the traceback of an unrelated error is a real way
for a code to reach a log file.

**This service does not write to ``audit_logs``.** SEC-AUD-001 asks for
authentication events to be logged as security events *distinct from* business
audit, and no separate sink is specified anywhere in the documents — that gap is
recorded as **C5** in ``docs/PHASE_1_PLAN.md``. Ordering a code is not a business
action (§44 rule 8), so nothing here belongs in the immutable business log.
Until C5 is answered the issuance is written to the structured logger instead,
which is the honest interim: not a trail, and not pretending to be one.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from apps.accounts.constants import OTPPurpose
from apps.accounts.models import OTPCode, User
from apps.accounts.services.errors import (
    InvalidOTP,
    OTPExpired,
    OTPNotFound,
    RateLimited,
)
from apps.accounts.services.phones import mask_phone
from config import ratelimit

__all__ = ["CODE_LENGTH", "DEFAULT_TTL", "TTL_BY_PURPOSE", "IssuedOTP", "issue", "verify"]

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
"""SEC-OTP-001: "OTPs are 6 digits"."""

_CODE_MODULUS = 10**CODE_LENGTH

DEFAULT_TTL = timedelta(minutes=10)
"""SEC-OTP-002: "expiring. 10 minutes for verification"."""

TTL_BY_PURPOSE: dict[str, timedelta] = {
    OTPPurpose.TRANSACTION: timedelta(minutes=5),
    OTPPurpose.PIN_RESET: timedelta(minutes=5),
}
"""SEC-OTP-002's "shorter for high-risk actions", and only those two.

``TRANSACTION`` authorises a payment and ``PIN_RESET`` re-keys the credential
that authorises payments, so a code for either is worth more to whoever steals
it than a code that only proves a phone number is reachable. Everything else
gets :data:`DEFAULT_TTL`.

A mapping rather than a branch inside the function, so that the next purpose
that needs a different window is a data change and is visible in one place.
"""


@dataclass(frozen=True, slots=True)
class IssuedOTP:
    """A code that has been stored, and the plaintext to deliver.

    ``code`` is excluded from ``repr`` deliberately. The default dataclass repr
    would print it, and a repr is what appears in the traceback of any exception
    raised while this object is in scope — a log line nobody meant to write,
    containing a live credential (S1).
    """

    code: str = field(repr=False)
    record: OTPCode
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"IssuedOTP(purpose={self.record.purpose!r}, "
            f"expires_at={self.expires_at!r}, code=<redacted>)"
        )


def _ttl_for(purpose: str) -> timedelta:
    return TTL_BY_PURPOSE.get(purpose, DEFAULT_TTL)


def _generate_code() -> str:
    """SEC-OTP-001: from a CSPRNG, ``secrets`` and never ``random``.

    ``secrets.randbelow`` is used rather than ``randint`` because it is
    documented to be free of modulo bias; ``random`` is seeded from the clock and
    is predictable from a handful of outputs, which for a value that is a
    credential is the whole game.

    The value is zero-padded to :data:`CODE_LENGTH`, and ``randbelow(10**6)``
    spans ``000000``-``999999`` uniformly — so leading zeros are real codes and
    not a shorter one. A generator that produced ``100000``-``999999`` would
    quietly cut the space by 10%.
    """
    return f"{secrets.randbelow(_CODE_MODULUS):0{CODE_LENGTH}d}"


def _masked(destination: str) -> str:
    """A destination safe to put in a log line.

    ``destination`` is a phone or an email depending on the purpose, so the mask
    follows the shape rather than the purpose: an ``@`` means the local part is
    what identifies the person, and it is the part that gets hidden.

    This is for the operational log, and it is a compromise rather than a clean
    answer — recording *something* about which destination a code went to is what
    makes an abuse report readable, and recording the destination in full would
    put PII in a log with no retention policy (SEC-PII-002). The clean answer
    arrives with C5, when security events get a sink of their own.
    """
    if "@" in destination:
        local, _, domain = destination.partition("@")
        return f"{local[:1]}***@{domain}"
    return mask_phone(destination)


def _enforce_issuance_limits(destination: str) -> None:
    """SEC-OTP-004, in both of the windows it names.

    Keyed on the destination rather than on the member, because the cost being
    bounded is an SMS to that number, whether or not an account is attached to it
    — and because issuing to an unknown destination must behave identically to
    issuing to a known one (S10).

    Raises:
        RateLimited: if either window is exhausted. The daily window has more
            headroom than the 15-minute one, so the shorter window is what
            normally fires first.
        config.ratelimit.RateLimiterUnavailable: if the limiter itself cannot
            answer. This propagates rather than being converted: it is an
            operator's P1, not a client's 429.
    """
    # Both are consumed, and a refusal from the first does not stop the second
    # from being counted. That direction is chosen: an attempt that was refused
    # is still an attempt, and under-counting is the failure that matters.
    refusals: list[tuple[str, int]] = []
    for scope in ("otp_issue", "otp_issue_daily"):
        decision = ratelimit.consume(scope, destination)
        if not decision.allowed:
            refusals.append((scope, decision.retry_after))

    if refusals:
        scope, retry_after = refusals[0]
        raise RateLimited(scope=scope, retry_after=retry_after)


def issue(*, destination: str, purpose: str, user: User | None = None) -> IssuedOTP:
    """Mint, store and return a code for ``destination``.

    ``user`` is optional because a password reset is requested by someone who is
    not authenticated, and for an unknown destination there is no user to attach
    the row to. The row is written either way (FR-AUTH-009): an issuance attempt
    against a number with no account is exactly the abuse an operator needs to be
    able to see, and refusing to record it would mean the enumeration attempts
    left no trace.

    **Any previous live code for the same destination and purpose is consumed.**
    Without that, requesting three codes leaves three valid ones, and the guess
    space an attacker faces is three times what SEC-OTP-003 budgets for. The
    purge and the insert share a transaction, so a concurrent issuance cannot
    slip a second live code between them.

    The caller is responsible for sending :attr:`IssuedOTP.code` and for
    discarding it immediately afterwards. Nothing here sends anything — delivery
    is the notification layer's job (09), which keeps this service callable from
    a task without a request or a mail server.

    Raises:
        RateLimited: SEC-OTP-004's windows are exhausted.
        config.ratelimit.RateLimiterUnavailable: the limiter is down.
    """
    _enforce_issuance_limits(destination)

    now = timezone.now()
    expires_at = now + _ttl_for(purpose)

    # Generated once, used twice: hashed into the row, and handed back to the
    # caller to deliver. Regenerating it after the insert would store the hash of
    # one code and deliver a different one — a member whose correct code is
    # always rejected, and no error anywhere to say why.
    code = _generate_code()

    with transaction.atomic():
        OTPCode.objects.filter(
            destination=destination,
            purpose=purpose,
            consumed_at__isnull=True,
        ).update(consumed_at=now)

        record = OTPCode.objects.create(
            user=user,
            destination=destination,
            purpose=purpose,
            code_hash=make_password(code),
            # `max_attempts` is not passed: the column's default is SEC-OTP-003's
            # 3 and is the single source of that number. `verify` reads it from
            # the row, so a purpose that needs a different cap sets it on the row
            # rather than requiring a change here.
            expires_at=expires_at,
        )

    logger.info(
        "otp.issued",
        extra={
            "purpose": purpose,
            "destination": _masked(destination),
            "otp_id": record.pk,
            "user_public_id": str(user.public_id) if user else None,
        },
    )
    return IssuedOTP(code=code, record=record, expires_at=expires_at)


def _record_failed_attempt(record: OTPCode, now: datetime) -> None:
    """Count a wrong guess, and kill the code if that was the last one allowed.

    SEC-OTP-003: "Attempts are capped (3) per code; exceeding it consumes the
    code." Consuming rather than rejecting matters because a rejected-but-live
    code can be tried again; a consumed one makes the next attempt land on
    :class:`OTPNotFound` and tells the member to request a new code, which is the
    only way forward anyway.

    ``attempts`` is never set above ``max_attempts`` — the model's
    ``otp_codes_attempts_within_max`` CHECK constraint refuses it, so a "fourth
    attempt" would be a database error rather than a security outcome.
    """
    record.attempts += 1
    if record.attempts >= record.max_attempts:
        record.consumed_at = now
    record.save(update_fields=["attempts", "consumed_at"])


def verify(*, destination: str, purpose: str, code: str) -> OTPCode:
    """Check a code and consume it. Returns the record it belonged to.

    The row is locked for the duration (``select_for_update``), because the
    attempt counter is what SEC-OTP-003's cap is built on: two requests that both
    read ``attempts=2`` and both write ``3`` would allow a fourth and a fifth
    guess, and the cap would be a comment rather than a control.

    Order of checks, and why:

    1. **Find the row.** No row at all is :class:`OTPNotFound`.
    2. **Consumed, expired or out of attempts** — all three mean "this code can
       no longer be used", and each gets the message that tells the member what
       to do next. Checked before the limiter because a dead code cannot be
       guessed at: charging the per-code budget for it would throttle a member
       who is retrying a code they have already used, while protecting nothing.
    3. **Rate limit, per code** (§6.4's ``otp_verify``). Counted before the code
       is compared, so an attempt that fails the comparison still spends a unit.
    4. **Compare.** ``check_password`` is constant-time in the comparison
       (SEC-OTP-005) and is the same machinery that protects a password, so a
       leaked ``otp_codes`` table is not a set of usable codes (SEC-OTP-002).

    The failure path at step 1 is measurably faster than a wrong code at step 4,
    which is an oracle for "does a live code exist for this destination". It is
    accepted rather than padded away: the caller had to know the destination to
    ask, and asking is itself rate-limited by ``otp_issue``, so the only thing
    learned is something the attacker already arranged.

    Raises:
        OTPNotFound: no code, an already-consumed one, or one out of attempts.
        OTPExpired: the code existed and its window closed.
        InvalidOTP: the code did not match. The attempt has been counted.
        RateLimited: §6.4's ``otp_verify`` ceiling for this code.
    """
    now = timezone.now()

    with transaction.atomic():
        record = (
            OTPCode.objects.select_for_update()
            .filter(destination=destination, purpose=purpose)
            # Sorted by `expires_at` rather than `created_at` to use
            # `otp_codes_lookup_idx` (destination, purpose, expires_at) as a
            # backwards index scan instead of a sort over every code ever issued
            # to this destination. The two orderings agree because the TTL is a
            # function of the purpose alone: within one purpose, the newest row
            # always has the furthest expiry. If a purpose ever gains a
            # per-attempt TTL, this line stops being equivalent and has to change.
            .order_by("-expires_at")
            .first()
        )

        if record is None:
            raise OTPNotFound(f"No {purpose} code has been issued to this destination.")

        if record.consumed_at is not None:
            raise OTPNotFound("That code has already been used.")
        if record.expires_at <= now:
            raise OTPExpired("That code has expired.")
        if record.attempts >= record.max_attempts:
            raise OTPNotFound("That code has no attempts left.")

        # Keyed on (destination, row), not on the row alone: primary keys are
        # reused after a rollback, so a counter keyed on the id would let one
        # code's spent budget throttle a later code that happened to be assigned
        # the same id. In production the pair is stable for the life of a code;
        # in tests it is what keeps two tests' counters apart.
        decision = ratelimit.consume(
            "otp_verify",
            ratelimit.composite("otp", destination, str(record.pk)),
        )
        if not decision.allowed:
            raise RateLimited(scope="otp_verify", retry_after=decision.retry_after)

        if check_password(code.strip(), record.code_hash):
            record.consumed_at = now
            record.save(update_fields=["consumed_at"])
            logger.info(
                "otp.verified",
                extra={
                    "purpose": purpose,
                    "destination": _masked(destination),
                    "otp_id": record.pk,
                },
            )
            return record

        # The increment must commit. Raising inside the ``atomic()`` block would
        # roll it back, so every wrong guess would read ``attempts == 1`` and the
        # cap would never be reached — the control would exist on paper only.
        _record_failed_attempt(record, now)

    logger.info(
        "otp.rejected",
        extra={
            "purpose": purpose,
            "destination": _masked(destination),
            "otp_id": record.pk,
            "attempts": record.attempts,
            "max_attempts": record.max_attempts,
        },
    )
    raise InvalidOTP("That code is not correct.")
