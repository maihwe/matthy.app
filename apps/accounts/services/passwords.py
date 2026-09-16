"""Password policy — SEC-AUTH-004, SEC-AUTH-003.

The validators themselves live in ``AUTH_PASSWORD_VALIDATORS``; this module is
the one place that *applies* them and converts the result into the project's own
error vocabulary. It exists so that registration (S1b) and password change
(SEC-AUTH-007, S4) cannot drift apart: both call this, and neither decides for
itself what "strong enough" means.

**Validation is not the same as hashing.** A password reaches Django's hashers
exactly once, in ``User.set_password()``. Nothing here stores, returns or logs the
value it was given; the only thing that leaves this module is an exception whose
message comes from the validators' own templates.
"""

from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.accounts.models import User
from apps.accounts.services.errors import WeakPassword

__all__ = ["enforce_strength"]


def enforce_strength(password: str, *, user: User | None = None) -> None:
    """Refuse a password the configured validators reject.

    ``user`` is what makes the similarity check possible: a password is weak *in
    relation to a person*, and the validator needs the person. Registration
    passes an unsaved instance built from the submitted values — unsaved,
    because a password must be rejected before an account exists for it, and a
    row created only to be validated would have to be deleted or rolled back on
    every weak submission.

    Raises:
        WeakPassword: carrying every validator message, joined. All of them
            rather than the first, because a member told "too short" who then
            submits twelve characters of their own phone number should not have
            to discover the next rule one round trip at a time.
    """
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        # The validators' templates name the *rule* and never the value
        # ("This password is too common"), so a joined message is safe to return
        # to the caller and safe in a log — which matters, because this detail
        # reaches both (SEC-AUTH-003, S1).
        raise WeakPassword(" ".join(exc.messages)) from exc
