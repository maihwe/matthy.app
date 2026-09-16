"""Session and refresh-token lifecycle — SEC-JWT-002…005, 009, and C6.

Marked ``integration``: rotation locks the predecessor row and writes the
successor in the same transaction, and the reuse case is a race — two callers
holding one token — so ``SELECT ... FOR UPDATE`` is load-bearing rather than
decorative. On SQLite there is no row lock and the tests would pass while the
control did not exist.

Marked ``security`` because every assertion here is one of that register's
obligations (S1, S3) or a requirement from 10 §3.1.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import UserSession
from apps.accounts.services.errors import InvalidRefreshToken, RefreshTokenReuse
from apps.accounts.services.sessions import (
    ACCESS_TTL,
    REFRESH_TTL,
    USER_AGENT_MAX_LENGTH,
    issue,
    revoke,
    revoke_all_for_user,
    revoke_device_sessions,
    revoke_session_family,
    rotate,
)
from tests.factories import DeviceFactory, UserFactory

pytestmark = [pytest.mark.integration, pytest.mark.django_db, pytest.mark.security]


def _reuse_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every reuse-detection record captured so far, in order.

    Filtered by message rather than by logger, because the same call also emits
    ``session.rotated`` on the module logger and a test that took "the last
    record" would be asserting about whichever happened to come last. The fields
    are read with ``getattr`` because ``LogRecord`` does not declare the keys a
    caller passed in ``extra`` — mypy strict checks this file too.
    """
    return [
        record
        for record in caplog.records
        if record.getMessage() == "session.refresh_reuse_detected"
    ]


class TestIssue:
    """SEC-JWT-002."""

    def test_the_token_is_returned_once_and_not_stored(self) -> None:
        issued = issue(user=UserFactory())

        stored = UserSession.objects.get(pk=issued.session.pk)

        assert stored.refresh_token_hash != issued.refresh_token
        assert issued.refresh_token not in stored.refresh_token_hash

    def test_the_plaintext_is_in_no_column(self) -> None:
        """Not just "not in refresh_token_hash" — nowhere in the row.

        A debugging column holding the last issued token would fail this, which is
        the intent: the row is replicated, dumped and read by operators.
        """
        issued = issue(user=UserFactory())

        stored = UserSession.objects.get(pk=issued.session.pk)
        row = {
            field.name: getattr(stored, field.name)
            for field in stored._meta.fields
            if field.name != "id"
        }

        assert issued.refresh_token not in str(row)

    def test_the_repr_redacts_the_token(self) -> None:
        issued = issue(user=UserFactory())

        assert issued.refresh_token not in repr(issued)
        assert "<redacted>" in repr(issued)

    def test_the_repr_redacts_inside_another_structure(self) -> None:
        issued = issue(user=UserFactory())

        assert issued.refresh_token not in repr([issued])

    def test_the_token_is_long_enough_to_be_unguessable(self) -> None:
        """384 bits. The digest's strength is the token's entropy, not the hash."""
        issued = issue(user=UserFactory())

        assert len(issued.refresh_token) >= 64

    def test_the_window_is_thirty_days(self) -> None:
        before = timezone.now()

        issued = issue(user=UserFactory())

        assert before + REFRESH_TTL <= issued.expires_at
        assert issued.expires_at <= timezone.now() + REFRESH_TTL

    def test_the_access_window_is_fifteen_minutes(self) -> None:
        """Declared here so it cannot drift from the refresh window (SEC-JWT-001)."""
        assert ACCESS_TTL == timedelta(minutes=15)
        assert ACCESS_TTL < REFRESH_TTL

    def test_a_session_can_be_issued_without_a_device(self) -> None:
        """The web admin has no device record and uses Django sessions (02 §5.7)."""
        issued = issue(user=UserFactory())

        assert issued.session.device is None

    def test_a_long_user_agent_is_truncated_rather_than_rejected(self) -> None:
        """A 600-character User-Agent must not abort a login.

        Django does not truncate on save and PostgreSQL raises rather than
        trimming, so without the truncation this is an error at the last statement
        of an authentication — a member who cannot sign in because of their
        browser's header length.
        """
        issued = issue(user=UserFactory(), user_agent="x" * 600)

        issued.session.refresh_from_db()

        assert len(issued.session.user_agent) == USER_AGENT_MAX_LENGTH

    def test_a_missing_user_agent_is_recorded_as_empty(self) -> None:
        """The column is ``blank=True, default=""``, not nullable — unlike the
        audit log's, which distinguishes "none sent" from "not recorded"."""
        issued = issue(user=UserFactory(), user_agent=None)

        assert issued.session.user_agent == ""


class TestRotation:
    """SEC-JWT-003."""

    def test_rotation_revokes_the_predecessor(self) -> None:
        issued = issue(user=UserFactory())

        rotate(refresh_token=issued.refresh_token)

        issued.session.refresh_from_db()
        assert issued.session.revoked_at is not None

    def test_rotation_returns_a_different_token(self) -> None:
        issued = issue(user=UserFactory())

        successor = rotate(refresh_token=issued.refresh_token)

        assert successor.refresh_token != issued.refresh_token
        assert successor.session.pk != issued.session.pk

    def test_the_successor_starts_a_fresh_thirty_day_window(self) -> None:
        """The window is from the rotation, not the original issue.

        Otherwise a member who rotates diligently for a month is signed out
        despite holding a token the server just minted.
        """
        issued = issue(user=UserFactory())

        successor = rotate(refresh_token=issued.refresh_token)

        assert successor.expires_at >= timezone.now() + REFRESH_TTL - timedelta(seconds=5)

    def test_the_successor_keeps_the_device(self) -> None:
        """SEC-JWT-005: the binding follows the session, not the caller.

        A rotation that re-bound the device from the request would let a stolen
        token re-bind itself to the thief's device — and then survive the victim
        revoking the original device, which is the one action the binding exists
        to make effective.
        """
        device = DeviceFactory()
        issued = issue(user=device.user, device=device)

        successor = rotate(refresh_token=issued.refresh_token)

        assert successor.session.device_id == device.pk

    def test_rotation_records_the_current_request(self) -> None:
        """``ip_address`` and ``user_agent`` are observed, not inherited.

        A change of address across rotations is a signal SEC-LOCK-004 wants
        available, and copying the predecessor's values would erase it.
        """
        issued = issue(user=UserFactory(), ip_address="10.0.0.1", user_agent="before")

        successor = rotate(
            refresh_token=issued.refresh_token,
            ip_address="10.0.0.2",
            user_agent="after",
        )

        successor.session.refresh_from_db()
        assert successor.session.ip_address == "10.0.0.2"
        assert successor.session.user_agent == "after"

    def test_an_unknown_token_is_refused(self) -> None:
        with pytest.raises(InvalidRefreshToken):
            rotate(refresh_token="not-a-token-that-was-ever-issued")

    def test_an_expired_token_is_refused_and_revoked(self) -> None:
        issued = issue(user=UserFactory())
        UserSession.objects.filter(pk=issued.session.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        with pytest.raises(InvalidRefreshToken):
            rotate(refresh_token=issued.refresh_token)

        issued.session.refresh_from_db()
        # Recorded as revoked on the way out, so the member's session list does
        # not keep showing an expired session as active.
        assert issued.session.revoked_at is not None


class TestDeviceBinding:
    """SEC-JWT-005, FR-AUTH-012."""

    def test_rotating_on_a_revoked_device_is_refused(self) -> None:
        device = DeviceFactory(is_revoked=True)
        issued = issue(user=device.user, device=device)

        with pytest.raises(InvalidRefreshToken):
            rotate(refresh_token=issued.refresh_token)

    def test_the_refused_session_is_marked_revoked(self) -> None:
        device = DeviceFactory(is_revoked=True)
        issued = issue(user=device.user, device=device)

        with pytest.raises(InvalidRefreshToken):
            rotate(refresh_token=issued.refresh_token)

        issued.session.refresh_from_db()
        assert issued.session.revoked_at is not None


class TestReuseDetection:
    """SEC-JWT-004 — the requirement that makes rotation worth doing."""

    def test_presenting_a_spent_token_is_refused(self) -> None:
        issued = issue(user=UserFactory())
        rotate(refresh_token=issued.refresh_token)

        with pytest.raises(RefreshTokenReuse):
            rotate(refresh_token=issued.refresh_token)

    def test_it_is_a_dead_token_to_any_other_handler(self) -> None:
        """A handler written before reuse detection existed still catches it."""
        issued = issue(user=UserFactory())
        rotate(refresh_token=issued.refresh_token)

        with pytest.raises(InvalidRefreshToken):
            rotate(refresh_token=issued.refresh_token)

    def test_the_whole_family_is_revoked(self) -> None:
        """S3's obligation, stated as the assertion it names.

        The successor is the token a thief would have to also hold in order to
        benefit. Revoking only the presented one would leave the attacker's copy
        live, which is the compromise reuse detection exists to end.
        """
        device = DeviceFactory()
        issued = issue(user=device.user, device=device)
        successor = rotate(refresh_token=issued.refresh_token)

        with pytest.raises(RefreshTokenReuse):
            rotate(refresh_token=issued.refresh_token)

        successor.session.refresh_from_db()
        assert successor.session.revoked_at is not None

    def test_a_different_device_is_not_collateral(self) -> None:
        """The family is (user, device) — one device's theft is not the other's.

        Signing a member out of every device because one token leaked would make
        the response to a detected theft worse than the theft. Revoking
        everything is available as an explicit action (`revoke_all_for_user`),
        which is a decision someone makes rather than a side effect.
        """
        user = UserFactory()
        laptop = DeviceFactory(user=user)
        phone = DeviceFactory(user=user)
        phone_session = issue(user=user, device=phone)

        laptop_session = issue(user=user, device=laptop)
        rotate(refresh_token=laptop_session.refresh_token)
        with pytest.raises(RefreshTokenReuse):
            rotate(refresh_token=laptop_session.refresh_token)

        phone_session.session.refresh_from_db()
        assert phone_session.session.revoked_at is None

    def test_the_family_revocation_is_idempotent_under_repeated_attempts(self) -> None:
        """A thief who retries must not produce a different outcome each time.

        Both attempts are refused identically, and the second revokes nothing —
        the first detection already did the work. The count in the log record is
        how that is observable: ``rotate`` raises rather than returning, so the
        log is the only place the number appears.
        """
        issued = issue(user=UserFactory())
        rotate(refresh_token=issued.refresh_token)

        with pytest.raises(RefreshTokenReuse):
            rotate(refresh_token=issued.refresh_token)
        first = _reuse_events(caplog)

        with pytest.raises(RefreshTokenReuse):
            rotate(refresh_token=issued.refresh_token)
        second = _reuse_events(caplog)

        assert getattr(first[-1], "family_sessions_revoked") == 1
        assert getattr(second[-1], "family_sessions_revoked") == 0

    def test_the_detection_is_alertable(self, caplog: pytest.LogCaptureFixture) -> None:
        """SEC-AUD-005: reuse detection must reach the server's alerting.

        There is no separate security sink yet (C5), so this log record is the
        interim mechanism — and therefore an obligation with a test rather than
        an intention. If it stops being emitted, the interim mechanism has
        quietly gone away and nothing else would notice.
        """
        issued = issue(user=UserFactory())
        rotate(refresh_token=issued.refresh_token)

        with pytest.raises(RefreshTokenReuse), caplog.at_level(
            logging.WARNING, logger="apps.accounts.services.sessions"
        ):
            rotate(refresh_token=issued.refresh_token)

        assert _reuse_events(caplog)

    def test_the_detection_record_identifies_the_session_not_the_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The record has to be useful to an investigation without itself being
        a credential. It carries public ids; the token is in the request the
        caller already holds, and does not belong in a log file (S1)."""
        issued = issue(user=UserFactory())
        rotate(refresh_token=issued.refresh_token)

        with pytest.raises(RefreshTokenReuse), caplog.at_level(
            logging.WARNING, logger="apps.accounts.services.sessions"
        ):
            rotate(refresh_token=issued.refresh_token)

        event = _reuse_events(caplog)[-1]
        assert getattr(event, "session_public_id")
        assert issued.refresh_token not in str(event.__dict__)


class TestRevocation:
    def test_revoke_reports_whether_it_was_the_one_that_ended_it(self) -> None:
        issued = issue(user=UserFactory())

        assert revoke(issued.session) is True
        assert revoke(issued.session) is False

    def test_revoke_all_ends_every_session_the_member_holds(self) -> None:
        """SEC-JWT-009: a password change ends every session.

        A credential change that left other sessions alive would leave whoever
        knew the old password signed in — the specific thing the requirement
        prevents.
        """
        user = UserFactory()
        sessions = [issue(user=user) for _ in range(3)]

        count = revoke_all_for_user(user)

        assert count == 3
        for issued in sessions:
            issued.session.refresh_from_db()
            assert issued.session.revoked_at is not None

    def test_revoke_all_does_not_touch_another_member(self) -> None:
        other = issue(user=UserFactory())
        revoke_all_for_user(UserFactory())

        other.session.refresh_from_db()
        assert other.session.revoked_at is None

    def test_revoke_all_counts_only_live_sessions(self) -> None:
        """The count is what was revoked, not what matched — it is logged, and a
        number that included already-dead sessions would overstate the action."""
        user = UserFactory()
        first = issue(user=user)
        revoke(first.session)

        assert revoke_all_for_user(user) == 0

    def test_revoke_device_ends_only_that_device(self) -> None:
        """FR-AUTH-012: losing one phone is not a reason to sign out everywhere."""
        user = UserFactory()
        lost, kept = DeviceFactory(user=user), DeviceFactory(user=user)
        lost_session = issue(user=user, device=lost)
        kept_session = issue(user=user, device=kept)

        count = revoke_device_sessions(lost)

        assert count == 1
        lost_session.session.refresh_from_db()
        kept_session.session.refresh_from_db()
        assert lost_session.session.revoked_at is not None
        assert kept_session.session.revoked_at is None

    def test_a_device_less_session_is_revoked_alone(self) -> None:
        """The conservative branch of ``revoke_session_family``.

        A session with no device has no family, so "revoke the family" cannot
        mean "revoke everything this member holds" — that would be a wider action
        than the caller asked for, taken silently. This asserts the narrow
        reading: the device-less session dies, its siblings do not.
        """
        user = UserFactory()
        device_less = issue(user=user)
        sibling = issue(user=user, device=DeviceFactory(user=user))

        count = revoke_session_family(device_less.session)

        assert count == 1
        device_less.session.refresh_from_db()
        sibling.session.refresh_from_db()
        assert device_less.session.revoked_at is not None
        assert sibling.session.revoked_at is None
