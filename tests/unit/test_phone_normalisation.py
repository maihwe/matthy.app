"""Phone normalisation — 03 §4.1, FR-AUTH-001.

Marked ``unit``: ``phonenumbers`` is a port of libphonenumber with the numbering
plans compiled in, so it answers offline and needs no database and no service.

The assertions are the ones that decide whether ``users.phone``'s ``UNIQUE``
constraint prevents duplicate accounts or merely looks like it does. A
normaliser that accepted two spellings of one number would leave the constraint
passing while two rows for one member existed.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import PHONE_MAX_LENGTH
from apps.accounts.services.errors import InvalidPhoneNumber
from apps.accounts.services.phones import DEFAULT_REGION, mask_phone, normalise_phone

pytestmark = pytest.mark.unit

# The canonical Nigerian mobile used throughout these tests: a 0803 MTN number,
# written the way the numbering plan defines it.
NG_E164 = "+2348031234567"


class TestNormalisation:
    def test_a_local_number_gets_its_country_code(self) -> None:
        assert normalise_phone("08031234567") == NG_E164

    def test_the_trunk_zero_is_optional(self) -> None:
        """``8031234567`` and ``08031234567`` are the same number.

        Members write both, and a normaliser that understood only one of them
        would let one person hold two accounts by typing their number differently
        on the second registration.
        """
        assert normalise_phone("8031234567") == NG_E164

    @pytest.mark.parametrize(
        "as_typed",
        [
            "+234 803 123 4567",
            "+234-803-123-4567",
            "  +2348031234567  ",
            "+234 (803) 123 4567",
            "0803 123 4567",
        ],
    )
    def test_spacing_and_punctuation_do_not_change_the_identity(self, as_typed: str) -> None:
        assert normalise_phone(as_typed) == NG_E164

    def test_normalisation_is_idempotent(self) -> None:
        """A stored value must survive being normalised again.

        Every writer normalises. Without idempotence, a service that normalised
        an already-stored value would produce a *third* spelling, and the lookups
        that compare against the column would start missing.
        """
        once = normalise_phone("08031234567")

        assert normalise_phone(once) == once

    def test_an_international_prefix_ignores_the_default_region(self) -> None:
        """A ``+`` means the number says where it is from.

        Asserted by parsing a Nigerian number while claiming the default region
        is the UK: if the ``+`` were being overridden by ``region``, this could
        not come back as a Nigerian number. That is the property international
        members depend on, and it is tested without asserting anything about
        another country's numbering plan.
        """
        assert normalise_phone(NG_E164, region="GB") == NG_E164

    def test_the_result_fits_the_column(self) -> None:
        """E.164 caps at 15 digits, so the column has headroom and never truncates.

        ``PHONE_MAX_LENGTH`` is 20 because 03 §4.1 specifies it; the longest
        legal E.164 value is 16 characters with the ``+``. A value that did not
        fit would be silently truncated by the database on some backends, which
        is how a member ends up with a phone number that is nearly theirs.
        """
        assert len(NG_E164) <= PHONE_MAX_LENGTH

    def test_the_default_region_is_where_the_platform_operates(self) -> None:
        assert DEFAULT_REGION == "NG"


class TestRefusals:
    @pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
    def test_an_empty_value_is_refused(self, empty: str) -> None:
        with pytest.raises(InvalidPhoneNumber):
            normalise_phone(empty)

    @pytest.mark.parametrize(
        "not_a_number",
        [
            "not a phone number",
            "0803-CALL-ME",
            "+",
            "+234",
        ],
    )
    def test_a_value_that_is_not_a_number_is_refused(self, not_a_number: str) -> None:
        with pytest.raises(InvalidPhoneNumber):
            normalise_phone(not_a_number)

    def test_a_number_of_the_wrong_length_is_refused(self) -> None:
        """Short by one digit: digit-shaped, and nothing will ever deliver to it.

        The distinction this pins is ``is_valid_number`` over
        ``is_possible_number``: a plausible length is not a allocated number, and
        storing one produces an OTP that never arrives.
        """
        with pytest.raises(InvalidPhoneNumber):
            normalise_phone("0803123456")

    def test_the_refusal_names_the_offending_value(self) -> None:
        """The detail goes to the log, not to the member — but it has to be there.

        "Invalid phone number" with no value is unactionable in a support ticket;
        the member's own input is what lets someone see the typo.
        """
        with pytest.raises(InvalidPhoneNumber) as refused:
            normalise_phone("0803-CALL-ME")

        assert "0803-CALL-ME" in str(refused.value)

    def test_the_refusal_is_a_domain_error_not_a_value_error(self) -> None:
        """Catchable by the API layer as one thing, not by class name."""
        with pytest.raises(InvalidPhoneNumber) as refused:
            normalise_phone("")

        assert refused.value.code == "invalid_phone_number"


class TestMasking:
    """For the message a member reads back, and only for that."""

    def test_it_keeps_the_country_code_and_the_last_four(self) -> None:
        assert mask_phone(NG_E164) == "+23*******4567"

    def test_the_subscriber_prefix_is_not_recoverable(self) -> None:
        """``8031`` is what distinguishes this member from the next one.

        Masking that left the prefix visible would make the mask decorative: two
        members' masked numbers would be tellable apart, and the last four alone
        are the part people already share.
        """
        masked = mask_phone(NG_E164)

        assert "8031" not in masked
        assert masked.endswith("4567")
        assert len(masked) == len(NG_E164)

    def test_a_value_too_short_to_mask_partially_is_masked_entirely(self) -> None:
        """Revealing four of seven characters is not masking."""
        assert mask_phone("1234567") == "*******"

    def test_a_value_that_was_never_normalised_is_still_hidden(self) -> None:
        """The masking path is reached by input that failed normalisation.

        A response that echoed a rejected value in full would put the member's
        mistyped number — which is usually a real number, just not a valid one —
        into a response body and every proxy log of it.
        """
        masked = mask_phone("08031234567")

        assert "1234" not in masked
        assert masked.endswith("4567")

    def test_the_length_is_preserved(self) -> None:
        """So the mask does not itself become a signal about the number."""
        for phone in (NG_E164, "08031234567", "123456789012345"):
            assert len(mask_phone(phone)) == len(phone)
