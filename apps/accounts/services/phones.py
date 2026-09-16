"""Phone normalisation to E.164 — 03 §4.1, FR-AUTH-001.

``users.phone`` is ``UNIQUE`` and is the login identifier. That constraint only
means what it looks like it means if every writer normalises first: without it,
``08031234567``, ``+2348031234567`` and ``+234 803 123 4567`` are three rows,
three accounts, and one member. The duplicate is invisible to every query
downstream, because nothing else knows the three are the same identity — and the
member who registers twice has money in two places.

**This is the only place a phone number becomes canonical.** Callers pass a raw
string from a request or a form; every other module in the project may assume
:data:`~apps.accounts.models.PHONE_MAX_LENGTH`-bounded E.164. If a service needs
to compare two numbers, it normalises both and compares the results — it does
not compare raw input.

``phonenumbers`` is a port of Google's libphonenumber, which is why it is a
dependency rather than a regular expression: it carries the actual numbering
plans, so ``+234`` numbers are checked against Nigerian ranges rather than
against a shape that looks like a phone number.
"""

from __future__ import annotations

from typing import Final

import phonenumbers

from apps.accounts.services.errors import InvalidPhoneNumber

__all__ = ["DEFAULT_REGION", "mask_phone", "normalise_phone"]

DEFAULT_REGION: Final = "NG"
"""Nigeria, per 01's target market.

A default region is what makes a leading ``0`` work: ``08031234567`` is a valid
Nigerian number and is not valid in any other country's plan, so
``phonenumbers.parse`` needs to be told where to read it. A number that carries
its own ``+`` ignores this default entirely, which is why international members
are not a special case.

It is a module constant rather than a per-call argument with no default, because
a caller who forgets it should get Nigeria — where the platform operates — rather
than an exception or, worse, a silent misparse as some other country's number.
"""


def normalise_phone(raw: str, *, region: str = DEFAULT_REGION) -> str:
    """Return ``raw`` as E.164, or raise :class:`InvalidPhoneNumber`.

    Args:
        raw: As typed by a member. Whitespace, brackets, dashes, a leading ``0``
            and a leading ``+`` are all understood.
        region: The country to read a number without an international prefix in.
            ``DEFAULT_REGION`` unless a caller has a better answer.

    Raises:
        InvalidPhoneNumber: if the value cannot be parsed, or parses to a number
            the numbering plan does not allocate.

    ``is_valid_number`` is used rather than ``is_possible_number``. The two are
    not the same check: *possible* only tests the length, so ``08000000000``
    passes it, while *valid* tests the number against the ranges the plan
    actually assigns. A number that is digit-shaped but unassigned is a number
    nothing will ever deliver to, and storing it means the member's first OTP
    silently never arrives — a support ticket that looks like a bug in sending.
    """
    candidate = raw.strip()
    if not candidate:
        raise InvalidPhoneNumber("A phone number is required.")

    try:
        parsed = phonenumbers.parse(candidate, region)
    except phonenumbers.NumberParseException as exc:
        # The library's message is about its own parser ("too short after IDD"),
        # which is not something to show a member; `exc` is chained for the log.
        raise InvalidPhoneNumber(f"{candidate!r} could not be parsed as a phone number.") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumber(f"{candidate!r} is not a number in a valid range.")

    formatted: str = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return formatted


def mask_phone(phone: str) -> str:
    """Mask an E.164 number for display back to the member who owns it.

    For the message a member reads — *"a code was sent to +23\\*\\*\\*\\*\\*\\*\\*4567"* —
    so that they can confirm which of their numbers received it without a
    shoulder-surfer learning the whole number. The country code and the last
    four are the parts a person recognises; the masked middle is the subscriber
    prefix that distinguishes two members.

    **Not a logging function.** A masked phone is still personal data, and the
    rule for logs is not "mask it" but "do not put it there at all" — log the
    ``public_id`` instead (SEC-PII-002, 10 §5.2). This exists for the one place
    a human needs to see a fragment of their own number, which is a response
    body, not a log file.

    Non-normalised input is masked by length rather than parsed: a value that
    failed normalisation still should not be echoed in full, and refusing to
    mask it would leave the caller with nothing safe to render.
    """
    if len(phone) <= 7:
        # Too short to reveal anything and still be recognisable; showing the
        # last four of a seven-character value would show most of it.
        return "*" * len(phone)
    head, tail = phone[:3], phone[-4:]
    return f"{head}{'*' * (len(phone) - len(head) - len(tail))}{tail}"
