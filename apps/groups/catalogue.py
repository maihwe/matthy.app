"""Explicit repair uses the same frozen catalogue as the data migration."""

from apps.groups.migrations._seed_v1 import seed

__all__ = ["seed"]
