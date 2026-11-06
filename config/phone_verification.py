"""Phone-code delivery boundary and a capture transport for local development."""

from __future__ import annotations

from typing import Protocol, cast

from django.conf import settings
from django.core.mail import send_mail
from django.utils.module_loading import import_string


class PhoneCodeBackend(Protocol):
    def check_available(self) -> None: ...

    def send(self, *, destination: str, code: str, purpose: str = "PHONE_VERIFY") -> None: ...


class DeliveryUnavailable(Exception):
    """The configured transport cannot deliver a phone verification code."""


def get_backend() -> PhoneCodeBackend:
    path = settings.PHONE_VERIFICATION_BACKEND
    if not path:
        raise DeliveryUnavailable()
    backend = cast(PhoneCodeBackend, import_string(path)())
    backend.check_available()
    return backend


class LocalEmailCapture:
    """Capture SMS-shaped messages in Mailhog or Django's test outbox."""

    def check_available(self) -> None:
        if settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend":
            return
        if (
            not settings.DEBUG
            or settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend"
            or settings.EMAIL_HOST not in {"localhost", "127.0.0.1", "::1", "mailhog"}
        ):
            raise DeliveryUnavailable()

    def send(self, *, destination: str, code: str, purpose: str = "PHONE_VERIFY") -> None:
        self.check_available()
        if purpose == "PHONE_VERIFY":
            label = "phone verification code"
        elif purpose == "PASSWORD_RESET":
            label = "password reset token"
        elif purpose == "MFA":
            label = "MFA verification code"
        elif purpose == "EMAIL_VERIFY":
            label = "email verification token"
        else:
            raise DeliveryUnavailable()
        expiry = (
            "24 hours"
            if purpose == "EMAIL_VERIFY"
            else f"{'5' if purpose == 'MFA' else '10'} minutes"
        )
        delivered = send_mail(
            f"IHWEZA {label}",
            f"Your {label} is {code}. It expires in {expiry}.",
            settings.DEFAULT_FROM_EMAIL,
            [
                destination
                if "@" in destination
                else f"{destination.removeprefix('+')}@phone.invalid"
            ],
        )
        if delivered != 1:
            raise DeliveryUnavailable()
