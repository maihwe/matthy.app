"""Password policy — SEC-AUTH-004, SEC-AUTH-003, P1-23.

Marked ``unit``: the validators are pure functions of a string and a person. No
database is involved, which is worth stating because it is the property that lets
a password be refused *before* an account exists for it.

The assertions are on the *messages*, not merely on the refusal, wherever more
than one rule could have fired. "Refused" is satisfied by any of the four
validators; naming the rule is what makes the test fail when that rule is
removed — and a test that cannot fail when the control it names is deleted is
not a test of that control.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from apps.accounts.services.errors import AccountError, WeakPassword
from apps.accounts.services.passwords import enforce_strength

pytestmark = pytest.mark.unit

STRONG = "xK7#mQp2!vLz9Rt"
"""Sixteen characters, mixed, not a phrase and not from any breach corpus.

Deliberately not a memorable sentence: the same reasoning that makes this a good
password makes it one whose acceptance is stable, since it can never drift into
a common-password list that is regenerated upstream.
"""

MEMBER_NAME = "Adaeze Okonkwo"
MEMBER_PHONE = "+2348031234567"


@pytest.fixture
def member() -> User:
    """An unsaved instance standing in for the person being registered.

    Unsaved on purpose — this is the object registration builds from the
    submitted values, and the whole point is that no row exists while the
    password is being judged.
    """
    return User(full_name=MEMBER_NAME, phone=MEMBER_PHONE)


class TestTheRulesThatDoNotNeedAPerson:
    def test_a_strong_password_is_accepted(self) -> None:
        enforce_strength(STRONG, user=None)

    def test_a_short_password_is_refused(self) -> None:
        with pytest.raises(WeakPassword) as refused:
            enforce_strength("Short1!aa", user=None)

        assert "at least 12" in refused.value.detail

    def test_a_common_password_is_refused(self) -> None:
        """The message is asserted, because the length rule fires here too.

        "password" is eight characters *and* in the corpus, so an assertion of
        "an exception was raised" would keep passing if the corpus check were
        deleted outright.
        """
        with pytest.raises(WeakPassword) as refused:
            enforce_strength("password", user=None)

        assert "too common" in refused.value.detail

    def test_an_entirely_numeric_password_is_refused(self) -> None:
        """Twelve digits satisfy the length rule, so this isolates the numeric
        one — and it matters here because a Nigerian phone number is eleven
        digits, which a member who has just been asked for twelve characters is
        very likely to have tried."""
        with pytest.raises(WeakPassword) as refused:
            enforce_strength("123456789012", user=None)

        assert "entirely numeric" in refused.value.detail

    def test_every_failing_rule_is_reported_at_once(self) -> None:
        """Not the first failure only.

        A member told "too short", who then submits twelve characters of the
        first word they thought of, should not have to discover the corpus rule
        on the next round trip — and the member most likely to abandon a
        registration is the one being corrected one rule at a time.
        """
        with pytest.raises(WeakPassword) as refused:
            enforce_strength("password", user=None)

        assert "at least 12" in refused.value.detail
        assert "too common" in refused.value.detail


class TestTheRulesThatNeedTheMember:
    """P1-23 — the similarity check is configured for what identifies a member."""

    def test_the_members_own_name_is_not_a_password(self, member: User) -> None:
        with pytest.raises(WeakPassword):
            enforce_strength(MEMBER_NAME, user=member)

    def test_a_password_containing_the_members_phone_is_refused(self, member: User) -> None:
        """The ``phone`` entry in isolation, which is why it is there.

        ``Okeke2348031234567`` is not entirely numeric, so the numeric validator
        allows it; and Django's default attribute list does not name the phone,
        so the similarity validator would not see it either. It is refused here
        only because ``phone`` was added to the options — the member's own name,
        which *is* in the default-adjacent list, does not catch it at any
        similarity the validator considers.
        """
        with pytest.raises(WeakPassword):
            enforce_strength("Okeke2348031234567", user=member)

    def test_a_strong_password_is_still_accepted_for_a_member(self, member: User) -> None:
        """The check must not become a rule that rejects everything.

        A validators misconfiguration that refuses all passwords would satisfy
        every test above, and would be discovered by members rather than here.
        """
        enforce_strength(STRONG, user=member)


class TestTheCheckNeedsAPerson:
    def test_without_a_member_only_the_person_independent_rules_run(self, member: User) -> None:
        """The limitation, stated plainly so no caller is surprised by it.

        Similarity is a comparison *between* a password and a person, so with no
        person there is nothing to compare against: the member's own name is
        accepted without ``user`` and refused with it. S4 must therefore pass the
        user on every path, including password *change* (SEC-AUTH-007), where the
        person is the one making the request.
        """
        enforce_strength(MEMBER_NAME, user=None)

        with pytest.raises(WeakPassword):
            enforce_strength(MEMBER_NAME, user=member)


class TestTheErrorItRaises:
    def test_it_is_a_domain_error_with_a_stable_code(self) -> None:
        with pytest.raises(WeakPassword) as refused:
            enforce_strength("password", user=None)

        assert isinstance(refused.value, AccountError)
        assert refused.value.code == "weak_password"

    def test_the_message_never_contains_the_password(self) -> None:
        """SEC-AUTH-003 and S1: the message names the rule, never the value.

        The value used here is the numeric one rather than ``"password"``, and
        for a reason worth writing down: Django's own templates contain the
        *word* "password" ("This password is too short"), so asserting that the
        attempted value is absent would fail for a value that happens to be a
        word the templates use. That is a property of the corpus, not a leak —
        the assertion has to be made with a value that cannot collide.
        """
        attempted = "123456789012"

        with pytest.raises(WeakPassword) as refused:
            enforce_strength(attempted, user=None)

        assert attempted not in refused.value.detail
        assert attempted not in str(refused.value)
