from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.models import User
from apps.groups.errors import GroupError
from apps.groups.services import bootstrap_platform


class Command(BaseCommand):
    help = "Assign the first platform SUPER_ADMIN to an active Django superuser with confirmed MFA."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("user", help="Public user UUID; no credentials are accepted.")

    def handle(self, *args: object, **options: Any) -> None:
        try:
            user = User.objects.get(public_id=options["user"])
            bootstrap_platform(user=user)
        except (User.DoesNotExist, ValueError, ValidationError, GroupError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Platform SUPER_ADMIN bootstrapped; sign in again."))
