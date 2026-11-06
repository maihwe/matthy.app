"""Account creation — FR-AUTH-001, FR-AUTH-007, FR-AUTH-014, S10, SEC-AUTH-004.

**S10 is the constraint that shapes this module.** Enumeration by registration
must not be possible, so "this number is already registered" is not an outcome
this function can report. It is *known* — the query has to run — but it stays
inside: the returned object names it for the caller, and the caller answers
exactly as it would have for a new number. The name is deliberately blunt
(``already_registered``) so that using it in a response body reads as what it is.

**What happens to a number that is already registered** is a decision, and it is
recorded as P1-22 because nothing in 03 or 10 settles it:

* **Not yet phone-verified** — a fresh verification code is issued to the number's
  owner and nothing is written. This is the member who began registering and did
  not finish, which is the common case in a market where the SMS may not have
  arrived. Refusing them here would leave a half-registered account that password
  reset cannot help (there is no working account to reset into) and no route
  forward at all.
* **Already phone-verified** — nothing is written and no message is sent. The
  member is not helped by a verification code for an account that is verified:
  the code would change nothing, and pairing it with the registration form would
  make this endpoint a way to send a code to any member whose number someone
  knows. A member who has forgotten that they registered has password reset
  (FR-AUTH-009), which is the designed path and has the same non-disclosing
  answer.

The branch is on **phone presence only** — never on account status. A suspended
or closed number follows the verified branch, because a status-dependent answer
is a second oracle: "your number is registered but suspended" identifies an
account just as plainly as "your number is registered".

**A note on what is not checked here.** The *shape* of the email, and the
presence of a name, are the request serializer's obligations (S4), not this
module's — parsing belongs to the API layer, and duplicating it here would create
two definitions of a valid request. ``full_name`` is stripped, which is
normalisation and belongs here. There is deliberately **no** not-empty database
constraint on ``full_name``: it is personal data, and SEC-PII-003's erasure
would have to write a placeholder around such a constraint (see C4). An empty
name is refused by the serializer, and if that check ever moved, the outcome is a
member row with a blank name — visible and correctable — rather than a 500.

Per-IP registration limiting is applied by ``onboarding.register()`` before
database work. Destination OTP budgets are applied here to new, unverified and
already-verified accounts alike, so throttling does not disclose account state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction

from apps.accounts.constants import OTPPurpose
from apps.accounts.models import User, UserProfile
from apps.accounts.services import otp as otp_service
from apps.accounts.services import passwords, phones
from apps.accounts.services.otp import IssuedOTP

__all__ = [
    "Registration",
    "register",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Registration:
    """The outcome of a registration attempt.

    ``already_registered`` exists for the caller's *logic* — it decides whether an
    account was created — and exists with that name so that any use of it in a
    response body is conspicuous in review. It must never be rendered, serialised
    or turned into a different status code: doing so converts this endpoint into
    the enumeration oracle S10 forbids.

    ``user`` is ``None`` when the supplied email is unavailable and no account
    was created. This conflict follows the same generic public response.

    ``otp`` is ``None`` when no message was sent. The ``IssuedOTP`` it holds
    redacts the code in its ``repr``; delivering the code is the caller's job,
    because Phase 1 has no notification service for it to call.
    """

    user: User | None
    already_registered: bool
    otp: IssuedOTP | None


def register(
    *,
    full_name: str,
    phone: str,
    password: str,
    email: str | None = None,
    region: str = phones.DEFAULT_REGION,
) -> Registration:
    """Create a member, or hand back the one the number already belongs to.

    Order matters, and the order is: normalise the phone, check the password,
    *then* look at the database. Validating the password first means a weak
    submission is answered identically whether or not the number is taken, so the
    validation path cannot be used to probe for accounts.

    Raises:
        InvalidPhoneNumber: the number is not a usable one for its region.
        WeakPassword: the password fails SEC-AUTH-004's validators.
        RateLimited: the destination has exhausted SEC-OTP-004's issuance
            windows. Raised for new *and* existing numbers alike — the limiter is
            keyed on the destination, not on the account — so the member is told
            why no message arrived without the answer differing between them.
    """
    normalised = phones.normalise_phone(phone, region=region)
    proposed = User(full_name=full_name.strip(), phone=normalised, email=_clean_email(email))
    passwords.enforce_strength(password, user=proposed)

    existing = User.objects.filter(phone=normalised).first()
    if existing is not None:
        return _existing(existing, normalised)

    try:
        created = _create(proposed, password)
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint == "users_email_lower_uniq":
            # Public registration must not disclose who already holds an
            # address, or turn an email conflict into a missing-phone 500.
            otp_service.reserve_issuance(normalised)
            logger.info("registration.email_unavailable")
            return Registration(user=None, already_registered=True, otp=None)
        if constraint != "users_phone_key":
            raise
        # Two registrations for one number arriving together, or a number
        # registered between the query above and this insert. The UNIQUE
        # constraint is what actually decides who owns the number; the loser
        # follows the existing-account path, which is the answer it would have
        # received a moment later anyway. Without this branch the member sees a
        # 500 for a race they cannot cause or understand.
        logger.info("registration.race_resolved_by_constraint")
        return _existing(User.objects.get(phone=normalised), normalised)

    logger.info(
        "registration.created",
        # The public id, never the phone: this line reaches a log file with no
        # access control (SEC-PII-001, S1).
        extra={"user_public_id": str(created.public_id)},
    )
    return Registration(
        user=created,
        already_registered=False,
        otp=otp_service.issue(
            destination=normalised,
            purpose=OTPPurpose.PHONE_VERIFY,
            user=created,
        ),
    )


def _clean_email(email: str | None) -> str | None:
    """Strip an address, and treat empty as absent.

    Not lowercased: the column's uniqueness is enforced by a functional index on
    ``Lower(email)`` (P1-2), so two case variants cannot both be stored, and the
    member's own spelling is what gets returned to them. An empty string is
    stored as ``NULL`` rather than ``""``, so that "no address" is one value
    instead of two.
    """
    if email is None:
        return None
    return email.strip() or None


def _create(proposed: User, password: str) -> User:
    """Write the member and their profile as one unit.

    Both rows or neither: a user without a profile is an account whose admin page
    and API representation are broken in a way that no later code path repairs.
    ``create_user`` rather than ``objects.create`` so the password goes through
    ``set_password()`` — the one place Django's hasher list is applied.
    """
    with transaction.atomic():
        user = User.objects.create_user(
            phone=proposed.phone,
            password=password,
            full_name=proposed.full_name,
            email=proposed.email,
        )
        UserProfile.objects.create(user=user)
    return user


def _existing(user: User, normalised: str) -> Registration:
    """The answer for a number that is already registered. See the module notes.

    The account is not touched: not its password, not its name, not its status.
    A registration form is not a way to modify an account, and the alternative —
    helpfully updating the existing member's details — would let anyone who knows
    a number rewrite the record behind it.

    ``is_phone_verified`` rather than ``verification_status`` is read here, and
    the choice needs saying because the schema offers both: 03 §4.1 specifies
    ``verification_status``, ``is_phone_verified`` *and* ``is_email_verified``
    with no stated precedence, which is **C10**. The narrower column is used
    because the question this branch asks is specifically about the phone — the
    thing a verification code proves — while the status ladder may be advanced by
    a mechanism that does not exist yet. When C10 is settled, this is one of the
    places that has to be re-read.
    """
    if user.is_phone_verified:
        # Public registration must consume the same destination budget on every path.
        otp_service.reserve_issuance(normalised)
        logger.info(
            "registration.existing_verified",
            extra={"user_public_id": str(user.public_id)},
        )
        return Registration(user=user, already_registered=True, otp=None)

    logger.info(
        "registration.existing_unverified",
        extra={"user_public_id": str(user.public_id)},
    )
    return Registration(
        user=user,
        already_registered=True,
        otp=otp_service.issue(
            destination=normalised,
            purpose=OTPPurpose.PHONE_VERIFY,
            user=user,
        ),
    )
