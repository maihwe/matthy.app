"""Re-encrypt active and pending TOTP seeds with the configured first key."""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import MFAEnrollment, User
from apps.accounts.services import mfa


class Command(BaseCommand):
    help = "Rotate TOTP seed encryption. Configure new,old MFA_ENCRYPTION_KEYS first."

    def handle(self, *args: object, **options: object) -> None:
        count = 0
        cipher = mfa.cipher()
        with transaction.atomic():
            for user in User.objects.select_for_update().order_by("pk").iterator():
                if user.totp_secret:
                    user.totp_secret = cipher.rotate(user.totp_secret.encode()).decode()
                    user.save(update_fields=["totp_secret"])
                    count += 1
                enrollment = MFAEnrollment.objects.filter(user=user).first()
                if enrollment is not None and enrollment.secret:
                    enrollment.secret = cipher.rotate(enrollment.secret.encode()).decode()
                    enrollment.save(update_fields=["secret"])
                    count += 1
        self.stdout.write(f"Re-encrypted {count} TOTP seeds.")
