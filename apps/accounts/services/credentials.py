"""Credential verification — FR-AUTH-002, FR-AUTH-013, SEC-AUTH-005, SEC-LOCK-001/002.

The one place an unauthenticated caller's password is checked. Everything on
that path is ordered deliberately, and the order is the security property rather
than an implementation detail:

1. **The rate limit, before anything is read.** 10 §6.4 keys login on
   "IP+account", so the bucket is ``composite(source_ip, identifier)``. A
   refused attempt never reaches the database, which is the point: the limiter
   exists to bound load as well as guesses.
2. **The lookup.** ``identifier`` is a phone number *or* an email address
   (FR-AUTH-002); which of the two it can be is decided by ``_read``.
3. **The lockout, before the password is evaluated.** SEC-LOCK-001's window
   would be a delay rather than a control if the password were checked first:
   an attacker inside the window would still be running one Argon2id hash per
   guess, and one of those guesses could be the right one. Refusing before the
   check is what makes a lockout a lockout.
4. **The password.** Only now.
5. **The account status, after the credentials.** FR-AUTH-013. The order is
   SEC-AUTH-005's: telling an unauthenticated caller that an account is
   suspended tells them that it exists. A caller who has proved the password has
   already learned that much, so the status becomes sayable here and nowhere
   earlier.

**One class for four failures.** An unknown identifier, an unparseable
identifier, a wrong password and a locked account all raise
:class:`~apps.accounts.services.errors.InvalidCredentials` with one message,
because SEC-AUTH-005 requires identical failure responses and a distinct
exception is a distinct response the moment an API layer maps it to a status
code. The distinguishable subclasses in ``.errors`` are reachable only after the
caller has proved possession of the password.

**The response is collapsed; the log is not.** A log is not a response, and an
operator investigating credential stuffing wants the opposite of what the
attacker wants: which addresses are grinding, and whether they are guessing
passwords or enumerating identifiers. The lines below distinguish what the
response does not. No line carries the identifier — a phone number or an email
address in a log file is PII in a place with no access control (SEC-PII-001,
10 §5.2).

**Timing is part of "identical".** An unknown identifier and a locked account
both return before any verification runs, so those responses come back in the
time a dictionary lookup takes while every other response takes the time an
Argon2id hash takes — tens of milliseconds against well under one. That
difference is measurable over a network and is an account-existence oracle: a
response *body* that is identical does not prevent it. Both paths therefore
spend one hash; see ``_spend_a_hash``.

**Why not ``django.contrib.auth.authenticate()``.** It returns ``None`` for
every failure, which cannot express SEC-LOCK-001's counter, §6.4's limit, or
FR-AUTH-013's two distinguishable statuses; and ``USERNAME_FIELD`` is ``phone``,
so it could not accept an email address at all. Not using it is also what makes
``ModelBackend.user_can_authenticate`` irrelevant here: ``is_active`` is never
consulted on this path, so the status branch below is the *only* enforcement of
FR-AUTH-013 and must stay. The two agree by construction — ``User.is_active`` is
derived from the same ``account_status`` column this function branches on —
which is asserted in the tests rather than assumed.

**What this module does not do.** It mints no token and touches no device (S4
owns both), sets no PIN (SEC-AUTH-006), and raises no HTTP error. Two halves of
the security requirements around login are not implemented anywhere, and neither
is overlooked:

* SEC-LOCK-002 asks for lockout per-account *and* per-source-IP. The account
  half is :mod:`.lockout`; the IP half has no column and §6.4 defines no scope
  for it, which is **C3**. The composite rate-limit key below is not a
  substitute: it bounds attempts per (IP, account) pair, not per IP, so one
  address still receives a fresh allowance for every account it names.
* SEC-LOCK-004/005 ask for a step-up challenge on a new device or an
  impossible-travel signal, and a review of unusual withdrawal patterns. Neither
  signal has a source in the schema — nothing records where a login came from
  geographically — and the notification SEC-LOCK-004 requires is absent from
  09's trigger catalogue, which is **C9**. Both are recorded as **C11**.
* The ``login.succeeded`` and ``login.identifier_unknown`` lines below are the
  first two of SEC-AUD-001's authentication events, and they go to the structured
  logger rather than to a security sink, because the design specifies none. That
  gap is **C5**, and this module is the fourth call site to reach the same
  conclusion as ``sessions.py``, ``lockout.py`` and ``otp.py`` — each of which
  says so in its own docstring. They deliberately do **not** go through
  ``record()``: SEC-AUD-001 requires security events "distinct from business
  audit", and ``record()`` writes the financial trail, which is permanent and
  cannot be anonymised (C4). A log line is the honest interim — not a trail, and
  not pretending to be one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from django.db.models.functions import Lower
from django.utils import timezone

from apps.accounts.constants import AccountStatus
from apps.accounts.models import User
from apps.accounts.services import lockout, phones
from apps.accounts.services.errors import (
    AccountClosed,
    AccountSuspended,
    InvalidCredentials,
    InvalidPhoneNumber,
    RateLimited,
)
from config import ratelimit

__all__ = ["authenticate"]

logger = logging.getLogger(__name__)

Method = Literal["phone", "email"]
"""Which of the two things ``identifier`` turned out to be (FR-AUTH-002)."""

_PHONE: Final[Method] = "phone"
_EMAIL: Final[Method] = "email"

_LOGIN_SCOPE: Final = "login"
"""10 §6.4's "Login 10/15 min per IP+account". Named once, because a typo in a
scope is not a refusal — ``ratelimit.limit_for`` raises ``ImproperlyConfigured``
for an unknown scope, so this would fail loudly rather than quietly removing the
limit. Naming it anyway is what lets a test assert against the same string."""


@dataclass(frozen=True, slots=True)
class _Identifier:
    """A submitted identifier, read once and used twice.

    Both uses go through the same value on purpose. The ``normalised`` form is
    what the query matches *and* what the rate limiter counts, so
    ``08031234567`` and ``+2348031234567`` cannot occupy two buckets — and
    neither can ``Ada@Example.test`` and ``ada@example.test``. A limit keyed on a
    spelling is a limit an attacker multiplies by respelling, which is a subtle
    way for a control to be weaker than the number in the table it came from.
    """

    method: Method
    normalised: str
    parseable: bool
    """False when a phone-shaped value is not a number in any allocated range.

    Carried rather than raised so that the refusal happens at exactly one place —
    ``_find`` returning ``None`` — which is the same place an unknown number
    refuses. Anything else would give an unparseable value its own branch, and a
    branch is something a response can be made to differ on.
    """


def _read(raw: str) -> _Identifier:
    """Decide whether ``raw`` is a phone number or an email address.

    The discriminator is the ``@``. A member's identifier arrives from one form
    field, so something has to choose, and the two candidate interpretations are
    mutually exclusive in practice: no E.164 number and nothing the numbering
    plans allocate contains ``@``, and an email address that contains none is
    not an email address. Anything with no ``@`` is therefore treated as a phone
    number, including values that are neither — those end up unparseable and are
    refused, which is the correct answer for them anyway.

    Emails are lowercased *here* and not in storage: the column's uniqueness
    comes from a functional index on ``Lower(email)`` (P1-2) and the member's own
    spelling is what gets echoed back to them, so nothing else in the project
    lowercases an address. This value is the exception because of the two jobs it
    does, both of which need case to be irrelevant.
    """
    candidate = raw.strip()
    if "@" in candidate:
        return _Identifier(method=_EMAIL, normalised=candidate.lower(), parseable=True)

    try:
        return _Identifier(
            method=_PHONE,
            normalised=phones.normalise_phone(candidate),
            parseable=True,
        )
    except InvalidPhoneNumber:
        return _Identifier(method=_PHONE, normalised=candidate, parseable=False)


def _find(identifier: _Identifier) -> User | None:
    """Look the member up, or return ``None``. Never raises.

    An :class:`~apps.accounts.services.errors.InvalidPhoneNumber` from
    normalisation is deliberately *not* propagated: it carries the code
    ``invalid_phone_number``, and a client able to branch on that code has been
    told something about the input that a wrong password does not tell it. The
    response for "that is not a number" and the response for "wrong password"
    have to be the same response (SEC-AUTH-005).
    """
    if not identifier.parseable:
        return None

    if identifier.method == _EMAIL:
        # The same expression the functional unique index is built on. That is
        # the correctness argument first and the performance one second:
        # `email__iexact` compiles to `UPPER(email)`, and an index on
        # `LOWER(email)` does not enforce the same equality relation. Where the
        # two disagree — `LOWER` and `UPPER` are not inverses in every locale —
        # the index would permit two rows that `iexact` matches, and `.first()`
        # would then pick one of a member's two accounts at random. Matching
        # `Lower` makes this lookup's equality identical to the constraint's, so
        # at most one row can satisfy it. The index is then usable as a
        # side-effect, not as the goal.
        return (
            User.objects.alias(_email_lower=Lower("email"))
            .filter(_email_lower=identifier.normalised)
            .first()
        )

    return User.objects.filter(phone=identifier.normalised).first()


def _spend_a_hash(password: str) -> None:
    """Do the work a real verification would have done, and throw it away.

    Every path that refuses *without* verifying — an identifier no account
    matches, and an account inside its lockout window — would otherwise answer in
    the time a primary-key lookup takes, while every other answer takes the time
    an Argon2id hash takes. The gap is tens of milliseconds and is trivially
    measurable, which makes it an account-existence oracle; SEC-AUTH-005's
    "identical responses" is not satisfied by identical response bodies alone.

    Hashing a submitted password against a throwaway instance is also how
    Django's own ``ModelBackend`` closes the same hole for an unknown username,
    so the mechanism is the framework's rather than this project's invention.

    Costs one hash per refusal, bounded by the rate limit above it. Paid
    deliberately: the alternative is a refusal that is faster than a real check,
    which tells an attacker their target is real — and the lockout path is the
    worse of the two, because a lockout is *proof* the account exists.
    """
    User().set_password(password)


def _bucket(identifier: _Identifier, source_ip: str | None) -> str:
    """The 10 §6.4 login key: IP+account, combined so neither half can be forged.

    An absent ``source_ip`` becomes the empty string rather than a placeholder.
    That is the fail-closed direction: every caller whose address could not be
    determined shares one bucket per account, so the limit is tighter for them,
    not looser. A literal ``"unknown"`` would be worse, because an address is not
    the string ``unknown`` and a caller cannot collide with it — whereas an empty
    part cannot be produced by any real address either, and the composite
    separator rules out the ambiguity that would otherwise allow.
    """
    return ratelimit.composite(source_ip or "", identifier.normalised)


def authenticate(
    *,
    identifier: str,
    password: str,
    source_ip: str | None = None,
    now: datetime | None = None,
) -> User:
    """Verify a credential pair, or refuse without saying which half was wrong.

    Args:
        identifier: A phone number or an email address, as typed (FR-AUTH-002).
        password: The submitted password. Never logged, never returned, and
            never placed on the returned instance.
        source_ip: The caller's address, for §6.4's per-IP half of the login
            key. ``None`` when it could not be determined; see ``_bucket``.
        now: Pins the lockout comparison and the timestamp a failure is recorded
            against, so that a test can assert an exact window instead of
            asserting approximately. It does **not** pin the rate limiter: that
            window is Redis's own clock, read from the cache's TTL.

    Returns:
        The member, having proved possession of the password and having an
        account that is permitted to authenticate.

    Raises:
        RateLimited: §6.4's login scope refused this attempt.
        InvalidCredentials: unknown identifier, unparseable identifier, wrong
            password, or an account inside its lockout window — one class for all
            four, and the same message for each.
        AccountSuspended: the credentials verified and the account is under an
            administrative hold (FR-AUTH-013).
        AccountClosed: the credentials verified and the account is closed.
        config.ratelimit.RateLimiterUnavailable: the limiter itself is down and
            the scope denies when it cannot answer (P1-16). Distinct from
            :class:`RateLimited` on purpose — one is an operator's P1, the other
            is routine — and the caller is expected to let it reach the API
            layer's 503 rather than catch it here.
    """
    stamp = now or timezone.now()
    identifier_read = _read(identifier)
    bucket = _bucket(identifier_read, source_ip)

    # Step 1. Before the lookup, so a flood of guesses costs a Redis round trip
    # rather than a query, and so a refused attempt leaves no trace in the
    # account's counter — see below.
    decision = ratelimit.consume(_LOGIN_SCOPE, bucket)
    if not decision.allowed:
        logger.warning(
            "login.rate_limited",
            extra={
                "scope": decision.scope,
                "retry_after": decision.retry_after,
                # The address is logged on refusals and never on a success: S4
                # records the successful login's address on the session row, so
                # repeating it here would be a second copy of the same PII with
                # no retention policy. On a refusal there is no such row, and
                # SEC-LOCK-002's per-IP half is unimplemented (C3), which makes
                # this line the only per-IP evidence that exists.
                "source_ip": source_ip,
            },
        )
        raise RateLimited(decision.scope, decision.retry_after)

    # Step 2.
    user = _find(identifier_read)

    if user is None:
        # Step 3's equalisation, for the other early return. Logged at warning
        # rather than info because this is the credential-stuffing signal and it
        # leaves no other record: unlike a wrong password for a real account,
        # nothing in the database counts it.
        _spend_a_hash(password)
        logger.warning(
            "login.identifier_unknown",
            extra={
                "method": identifier_read.method,
                "parseable": identifier_read.parseable,
                "source_ip": source_ip,
            },
        )
        raise InvalidCredentials()

    # Step 3. `is_locked_at` rather than `is_locked` so that this decision and
    # the window `record_failure` below compares against are made by one clock
    # reading rather than two that can disagree.
    if user.is_locked_at(stamp):
        _spend_a_hash(password)
        logger.warning(
            "login.refused_locked",
            extra={
                "user_public_id": str(user.public_id),
                "source_ip": source_ip,
            },
        )
        raise InvalidCredentials()

    # Step 4.
    if not user.check_password(password):
        # The attempt is counted here and nowhere else: a refusal by the rate
        # limiter above is not a *credential* failure, because it never reached
        # the credential — and if it were counted, the limiter would be
        # escalating the lockout with numbers SEC-LOCK-001 does not specify.
        #
        # No log line. `failed_login_attempts` is the record, and
        # `lockout.record_failure` announces the escalation with
        # `account.locked`; a line per mistyped password would be one line per
        # typo in the country, which is what that module already reasoned about.
        lockout.record_failure(user, now=stamp)
        raise InvalidCredentials()

    # The credential was correct, so SEC-LOCK-001's streak of *consecutive
    # failures* ends here — before the status branch, because the status branch
    # is not a credential failure and counting it as one would lock out a
    # suspended member for having the right password.
    lockout.clear(user)

    # Step 5. Anything that is not ACTIVE is refused; the refusal names CLOSED
    # when the row says so, and says SUSPENDED otherwise. Writing it as a
    # negative test rather than two positive ones is deliberate: a status this
    # build does not recognise — a row written by a newer deploy, or by hand —
    # then fails closed instead of authenticating.
    if user.account_status != AccountStatus.ACTIVE:
        if user.account_status == AccountStatus.CLOSED:
            raise AccountClosed()
        raise AccountSuspended()

    # Only a completed authentication clears the window's counter, which is
    # `ratelimit.reset`'s stated condition ("a member who gets their password
    # right should not stay throttled by their earlier mistakes"). A suspended
    # account's credentials were right but the request still failed, and leaving
    # the limit standing bounds the load an account that cannot act may generate.
    ratelimit.reset(_LOGIN_SCOPE, bucket)
    logger.info(
        "login.succeeded",
        extra={"user_public_id": str(user.public_id), "method": identifier_read.method},
    )
    return user
