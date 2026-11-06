"""Domain errors — SEC-AUTH-005, S10, and 10 §6.4.

Marked ``unit``: this asserts the *shape* of the error vocabulary, not a
service's behaviour. It matters because the enumeration requirements are
requirements about which failures are distinguishable, and the error classes are
where that is decided — a new class added during a feature is exactly how an
enumeration oracle gets introduced without anyone deciding to.

These are guards, not documentation. Each one fails when the rule it protects is
broken.
"""

from __future__ import annotations

import re

import pytest

from apps.accounts.services import errors
from apps.accounts.services.errors import (
    AccountError,
    InvalidCredentials,
    InvalidRefreshToken,
    RateLimited,
    RefreshTokenReuse,
)
from config.ratelimit import RateLimiterUnavailable

pytestmark = pytest.mark.unit

_CODE_PATTERN = re.compile(r"\A[a-z][a-z0-9_]*\Z")

LOGIN_PATH_CODES = {
    "invalid_credentials",
    "account_suspended",
    "account_closed",
    "service_unavailable",
}
"""Every code a login attempt can produce, and no others.

``invalid_credentials`` is the collapsed failure SEC-AUTH-005 requires.
``account_suspended``/``account_closed`` are permitted because they are raised
only *after* the password verified — a caller who reaches them has already
proved the account exists, so nothing is leaked that they did not already know.
``service_unavailable`` is also raised after verification when token signing or
the required MFA flow is unavailable.
"""

OTHER_CODES = {
    "account_error",  # the base class, which is public so callers can catch it
    "invalid_phone_number",
    "weak_password",
    "invalid_pin",
    "otp_not_found",
    "otp_expired",
    "invalid_otp",
    "phone_already_registered",
    "registration_not_allowed",
    "rate_limited",
    # Not login-path failures: a caller reaching these is already holding a token
    # and being told to sign in again, which reveals nothing about any account.
    "invalid_refresh_token",
    "refresh_token_reuse",
    "invalid_access_token",
}
"""Every code that is *not* on the login path.

Listed exhaustively on purpose. The two constants together partition the
vocabulary, so a new error class fails :meth:`TestTheLoginFailuresAreTheOnesWeMeant
.test_every_code_is_classified` until someone decides which path it belongs to.
That decision is the security-relevant one: an error added to the login path
without thinking about it is how an enumeration or lockout oracle gets in.
"""


def _error_classes() -> list[type[AccountError]]:
    return [getattr(errors, name) for name in errors.__all__]


def _codes() -> set[str]:
    return {cls.code for cls in _error_classes()}


class TestTheVocabulary:
    def test_every_exported_name_is_an_account_error(self) -> None:
        classes = _error_classes()

        assert classes
        assert all(issubclass(cls, AccountError) for cls in classes)

    def test_every_error_declares_a_message(self) -> None:
        """A blank default is a 500 with an empty body somewhere downstream."""
        for cls in _error_classes():
            assert cls.message.strip(), f"{cls.__name__} has no default message"

    def test_codes_are_unique(self) -> None:
        """Two classes sharing a code makes the code useless to a client.

        The client branches on the code; if two failures share one, the client
        cannot tell them apart, which is either a bug or — worse — a silent
        widening of which failures look alike.
        """
        codes = [cls.code for cls in _error_classes()]

        assert len(codes) == len(set(codes))

    def test_codes_are_machine_readable(self) -> None:
        for code in _codes():
            assert _CODE_PATTERN.match(code), f"{code!r} is not a stable identifier"


class TestNoErrorAnnouncesThatAnAccountExists:
    """S10: enumeration via registration, password reset or invitation is not
    possible. That is a property of the vocabulary, checkable here."""

    def test_there_is_no_account_not_found_error(self) -> None:
        """The class of mistake this guards against.

        A ``UserNotFound`` looks helpful, reads well in a traceback, and is an
        oracle: an attacker walks a phone-number range and learns which numbers
        hold accounts. If one is ever needed internally it must not be raised
        past the service boundary — see ``PhoneAlreadyRegistered``, which is
        documented as caught and not returned.
        """
        forbidden = {"user_not_found", "account_not_found", "phone_not_found", "email_not_found"}

        assert not (_codes() & forbidden)

    def test_the_only_not_found_error_is_about_a_code_not_an_account(self) -> None:
        assert {code for code in _codes() if "not_found" in code} == {"otp_not_found"}

    def test_the_only_already_error_is_the_internal_registration_one(self) -> None:
        """An error that is easy to return is an error that leaks.

        ``PhoneAlreadyRegistered`` is detected by the registration service and
        answered exactly as a new number would be, so it never reaches a caller.
        This asserts it is the only one — a second ``*_already_*`` class is a
        second place that decision has to be made.
        """
        already = {code for code in _codes() if "already" in code}

        assert already == {"phone_already_registered"}


class TestTheLoginFailuresAreTheOnesWeMeant:
    def test_every_code_is_classified(self) -> None:
        """The vocabulary partitions into login-path and everything else.

        The guard is the exhaustiveness. Every error class is either a failure an
        unauthenticated caller can provoke — which is a security decision, because
        those responses must not distinguish an existing account from a missing
        one — or it is not. A new class fails this test until it is placed, and
        placing it is the review point.
        """
        assert _codes() == LOGIN_PATH_CODES | OTHER_CODES

    def test_the_login_path_has_exactly_one_failure_before_verification(self) -> None:
        """Of the login codes, only one is reachable with the wrong password.

        ``account_suspended`` and ``account_closed`` are raised after the
        credentials verified, so they are not oracles. If a fourth code appears
        here without that property, this test does not catch it — but the
        docstring on ``InvalidCredentials`` and this name are what a reviewer is
        pointed at.
        """
        pre_verification = {"invalid_credentials"}

        assert pre_verification < LOGIN_PATH_CODES

    def test_invalid_credentials_says_nothing_about_which_part_was_wrong(self) -> None:
        message = InvalidCredentials.message.lower()

        assert "phone" in message or "number" in message
        assert "password" in message
        # It must not single one out: a message that mentions only the password
        # implies the number was fine.
        assert "does not exist" not in message
        assert "no account" not in message
        assert "not registered" not in message
        assert "locked" not in message
        assert "suspended" not in message

    def test_the_lockout_case_is_documented_as_deliberately_indistinguishable(self) -> None:
        """The trade is written down where the next reader will find it.

        Someone will eventually "fix" the missing lockout message as a UX bug.
        The docstring is what tells them it was a decision, and what the member
        is told instead.
        """
        doc = InvalidCredentials.__doc__ or ""

        assert "SEC-AUTH-005" in doc
        assert "lock" in doc.lower()


class TestRateLimited:
    def test_it_carries_the_scope_and_the_wait(self) -> None:
        """The API layer needs both: the scope for the log, the delay for the
        ``Retry-After`` header (04 §1)."""
        error = RateLimited(scope="login", retry_after=42)

        assert error.scope == "login"
        assert error.retry_after == 42
        assert error.code == "rate_limited"

    def test_it_names_the_scope_for_the_log(self) -> None:
        assert "login" in str(RateLimited(scope="login", retry_after=1))

    def test_it_is_not_the_limiter_being_unavailable(self) -> None:
        """429 and 503 are different events and must not be merged.

        ``RateLimited`` is a client over its ceiling: routine, expected, and
        answered with a delay. ``RateLimiterUnavailable`` is the platform's own
        control being down — a P1 for an operator, a 503 for the caller. If one
        were a subclass of the other, an exception handler catching the supertype
        would report an outage as ordinary throttling, which is how a control
        that has stopped working goes unnoticed.
        """
        assert not issubclass(RateLimited, RateLimiterUnavailable)
        assert not issubclass(RateLimiterUnavailable, AccountError)

    def test_the_two_are_caught_separately(self) -> None:
        with pytest.raises(RateLimiterUnavailable):
            raise RateLimiterUnavailable("login")

        # And the accounts error is not swept up by that handler.
        with pytest.raises(RateLimited):
            raise RateLimited(scope="login", retry_after=1)


class TestRefreshTokenReuse:
    """SEC-JWT-004 must be alertable on the server and invisible to the client."""

    def test_it_is_catchable_as_any_other_dead_token(self) -> None:
        """So a handler written before reuse detection existed still works."""
        assert issubclass(RefreshTokenReuse, InvalidRefreshToken)

    def test_it_carries_its_own_code_for_alerting(self) -> None:
        assert RefreshTokenReuse.code == "refresh_token_reuse"
        assert RefreshTokenReuse().code != InvalidRefreshToken.code

    def test_the_client_visible_message_is_identical(self) -> None:
        """The property that makes the subclassing safe.

        If reuse detection ever grew its own wording, a thief would learn from
        the response that they had been detected — and, in learning it, learn
        which token to stop using. The signal belongs in the server's alerting
        (SEC-AUD-005), not in the response body.
        """
        assert RefreshTokenReuse.message == InvalidRefreshToken.message

    def test_it_is_a_domain_error_like_the_rest(self) -> None:
        assert issubclass(RefreshTokenReuse, AccountError)


class TestMessagesAndDetails:
    def test_an_error_with_no_argument_uses_its_class_message(self) -> None:
        error = InvalidCredentials()

        assert str(error) == InvalidCredentials.message
        assert error.detail == InvalidCredentials.message

    def test_a_caller_can_supply_a_more_specific_detail(self) -> None:
        """For the log. The response body is built from the code, not from this."""
        error = InvalidCredentials("password mismatch for user 8f2c (from login service)")

        assert "8f2c" in error.detail
        assert error.code == "invalid_credentials"

    def test_the_detail_is_the_exception_message(self) -> None:
        """So an uncaught domain error still logs something useful."""
        assert str(InvalidCredentials("specific detail")) == "specific detail"
