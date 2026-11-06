from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.groups.catalogue import seed


class Command(BaseCommand):
    help = "Restore the system group catalogue without resetting group customisations."

    def handle(self, *args: object, **options: object) -> None:
        with transaction.atomic():
            seed(apps)
        self.stdout.write(self.style.SUCCESS("Group catalogue verified."))
