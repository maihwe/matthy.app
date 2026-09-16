"""Domain errors raised by the ``accounts`` services.

These are not HTTP errors and carry no status code. The API layer maps each one
to a response; a Celery task maps the same one to a retry or a failure; a test
asserts on the class. Naming the failure in the domain is what lets all three do
that without the service knowing which of them is calling.

**A ``code`` is a contract, a ``message`` is not.** Every class carries a stable
``code`` for the client to branch on and does not carry user-facing prose in the
class itself. Wording changes; ``invalid_credentials`` does not, and a mobile
build shipped six months ago is still matching on it.

**Which failures are distinguishable is a security decision, not a UX one.**
SEC-AUTH-005 forbids responses that reveal whether an account exists, so the
login failures an unauthenticated caller can trigger are deliberately collapsed
onto one class — see :class:`InvalidCredentials`. The subclasses that *are*
distinguishable below are distinguishable only after the caller has already
proved possession of something, or has already proven they know where the code
was sent.
"""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "AccountClosed",
    "AccountError",
    "AccountSuspended",
    "InvalidCredentials",
    "InvalidOTP",
    "InvalidPhoneNumber",
    "InvalidPin",
    "InvalidRefreshToken",
    "OTPExpired",
    "OTPNotFound",
    "PhoneAlreadyRegistered",
    "RateLimited",
    "RefreshTokenReuse",
    "RegistrationNotAllowed",
    "WeakPassword",
]


class AccountError(Exception):
    """Base class for every failure these services raise.

    Catching this catches everything the module decides, which is what a view's
    error handler or a task's failure branch wants. It deliberately does not
    derive from anything in DRF.
    """

    code: ClassVar[str] = "account_error"
    """Stable, machine-readable, and never reworded. Sent to clients."""

    message: ClassVar[str] = "The request could not be completed."
    """A default for developers and logs. Never sent to a client as-is."""

    def __init__(self, message: str | None = None) -> None:
        self.detail = message or self.message
        super().__init__(self.detail)


class InvalidPhoneNumber(AccountError):
    """The value is not a phone number that can be reached or delivered to.

    Raised by normalisation, so that no unnormalised number ever reaches the
    unique column on ``users.phone``. Two spellings of one number inserting two
    rows is a duplicate account that no query will find, because nothing
    downstream knows the two are the same identity.
    """

    code: ClassVar[str] = "invalid_phone_number"
    message: ClassVar[str] = "That is not a valid phone number."


class InvalidCredentials(AccountError):
    """Login failed, for any of the reasons that must look identical.

    SEC-AUTH-005: "Failure responses are **identical** whether the account
    exists or the password was wrong, to prevent account enumeration." That
    requirement is met by this class existing and by *nothing else being raised
    on that path* — an unknown phone, a wrong password, and a locked account all
    raise this one, with the same message.

    That last one is a real trade and not an oversight. Telling a locked-out
    member why they are locked out also tells a stranger that the account
    exists, and the second cost is the one SEC-AUTH-005 was written to avoid.
    The member is told another way: SEC-LOCK-004 requires a notification to the
    registered phone on suspicious activity, and a lockout is exactly that. The
    alternative — a distinguishable ``AccountLocked`` on a failure path — would
    hand an attacker a free oracle for "does this phone have an account here?"

    A *suspended* account is a different case and is deliberately not this
    class: see :class:`AccountSuspended`.
    """

    code: ClassVar[str] = "invalid_credentials"
    message: ClassVar[str] = "The phone number or password is incorrect."


class AccountSuspended(AccountError):
    """The account is under an administrative hold (``AccountStatus.SUSPENDED``).

    Raised only *after* the credentials verified, which is why it is allowed to
    be distinguishable. By that point the caller has proved they hold the
    password, so there is no account existence left to leak — and a member who
    is suspended and told only "invalid credentials" will keep trying, then
    contact support to report a bug that is not one.
    """

    code: ClassVar[str] = "account_suspended"
    message: ClassVar[str] = "This account is suspended. Contact support."


class AccountClosed(AccountError):
    """The account is closed (``AccountStatus.CLOSED``), which is terminal.

    Accounts are closed, never deleted: §44 rule 10 requires the audit trail to
    survive, and a deleted member is a ledger entry pointing at nothing.
    """

    code: ClassVar[str] = "account_closed"
    message: ClassVar[str] = "This account is closed."


class WeakPassword(AccountError):
    """SEC-AUTH-004: too short, too common, or too like the member's own details.

    Carries the validator's messages, because a member choosing a password needs
    to know which rule they missed. This is not an enumeration risk: they are
    setting the password on an account they are already authenticated to.
    """

    code: ClassVar[str] = "weak_password"
    message: ClassVar[str] = "That password does not meet the minimum requirements."


class InvalidPin(AccountError):
    """The transaction PIN was wrong or has not been set (SEC-AUTH-006).

    One class for both, for the same reason :class:`InvalidCredentials` collapses
    its cases: FR-AUTH-005 gives a PIN ~10⁴ possibilities, so an attacker who can
    tell "no PIN set" from "wrong PIN" learns something for free. The defence is
    the ``pin_entry`` rate limit and the lockout, not the error message.
    """

    code: ClassVar[str] = "invalid_pin"
    message: ClassVar[str] = "The transaction PIN is incorrect."


class OTPNotFound(AccountError):
    """No live code exists for this destination and purpose.

    Distinguishable from :class:`InvalidOTP` because reaching this point already
    required knowing the destination the code was sent to.
    """

    code: ClassVar[str] = "otp_not_found"
    message: ClassVar[str] = "Request a new code."


class OTPExpired(AccountError):
    """The code existed and its window has closed (SEC-OTP-002).

    Kept separate from "wrong code" because a member staring at a code they
    received has a different problem in each case: wait vs. retype.
    """

    code: ClassVar[str] = "otp_expired"
    message: ClassVar[str] = "That code has expired. Request a new one."


class InvalidOTP(AccountError):
    """The code was presented and did not match (SEC-OTP-005, constant-time).

    Also raised on the attempt that exhausts ``max_attempts``, where the code is
    consumed rather than merely rejected (SEC-OTP-003) — the caller's next
    attempt finds no live code and gets :class:`OTPNotFound` instead.
    """

    code: ClassVar[str] = "invalid_otp"
    message: ClassVar[str] = "That code is not correct."


class PhoneAlreadyRegistered(AccountError):
    """A registration attempt reused an existing phone number.

    **Never returned to an unauthenticated caller.** S10 requires that account
    enumeration via registration not be possible, so the registration service
    catches this and answers exactly as it does for a new number. It exists so
    that the *internal* branch is named rather than being a bare ``if exists``,
    and so that tests can assert the collision was detected at all.

    It is in this module rather than raised past the service boundary on
    purpose: an error class that is easy to raise is an error class that leaks.
    """

    code: ClassVar[str] = "phone_already_registered"
    message: ClassVar[str] = "That phone number cannot be registered."


class RegistrationNotAllowed(AccountError):
    """Registration is closed, or this caller may not create accounts.

    A deployment-level switch rather than a per-member rule; it exists so that
    closing registration during an incident is a settings change and not a
    deploy.
    """

    code: ClassVar[str] = "registration_not_allowed"
    message: ClassVar[str] = "New registrations are not being accepted."


class InvalidRefreshToken(AccountError):
    """The refresh token cannot be used: unknown, expired, or revoked.

    One class for all three for the same reason :class:`InvalidCredentials`
    collapses its cases — a client that could distinguish "no such token" from
    "that token was revoked" would have an oracle for whether a guessed token
    ever existed, and 384 bits of entropy is only a defence if guessing is the
    attacker's *only* option.

    The client's response is to re-authenticate, which is correct in every case.
    """

    code: ClassVar[str] = "invalid_refresh_token"
    message: ClassVar[str] = "This session has ended. Sign in again."


class RefreshTokenReuse(InvalidRefreshToken):
    """SEC-JWT-004: an already-rotated token was presented.

    A subclass rather than a sibling, and that is the design: the *client* must
    see exactly what it sees for any other dead token — telling a thief that they
    tripped reuse detection tells them they have been noticed, and tells them
    which token to discard — while the *server* must be able to alert on it
    (SEC-AUD-005). Subclassing gives both: a handler catching
    :class:`InvalidRefreshToken` handles it as an ordinary failure, and a
    security-event hook catching this one sees the theft signal.

    Reaching this class means the whole session family has already been revoked,
    inside the same transaction.
    """

    code: ClassVar[str] = "refresh_token_reuse"
    message: ClassVar[str] = "This session has ended. Sign in again."


class RateLimited(AccountError):
    """A 10 §6.4 scope refused the attempt. Maps to 429 with ``Retry-After``.

    Carries the scope and the delay because the API layer needs both: the scope
    for the log line, the delay for the header. ``retry_after`` is the seconds
    left in the window, not the window's length (``config.ratelimit``).

    Distinct from :class:`config.ratelimit.RateLimiterUnavailable`, which is the
    platform's control being *down* rather than a client being over its ceiling.
    That one is a P1 for an operator and a 503 for the caller; this one is
    routine and is a 429. Collapsing them would make an outage look like
    ordinary throttling in the logs.
    """

    code: ClassVar[str] = "rate_limited"
    message: ClassVar[str] = "Too many attempts. Try again shortly."

    def __init__(self, scope: str, retry_after: int) -> None:
        super().__init__(f"Rate limit {scope!r} exceeded; retry in {retry_after}s.")
        self.scope = scope
        self.retry_after = retry_after
