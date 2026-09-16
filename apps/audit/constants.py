"""Closed vocabularies for the ``audit`` module."""

from __future__ import annotations

from django.db import models

__all__ = ["ActorType"]


class ActorType(models.TextChoices):
    """Who caused the recorded action (03 §12.1).

    The distinction is the difference between "an operator suspended this
    member" and "the system suspended this member", and between either of those
    and "a scheduled job did". An audit trail that records only *what* happened
    answers questions about the data and not about the people.

    ``SYSTEM`` is for automated transitions — a lockout applied by the
    authentication path, an expiry sweep. It is deliberately **not** a default:
    a writer that forgets to choose would otherwise file human actions under
    "the system did it", which is the one misattribution an audit trail cannot
    recover from. A CHECK constraint enforces the column is never blank.
    """

    USER = "USER", "User"
    SYSTEM = "SYSTEM", "System"
    ADMIN = "ADMIN", "Administrator"
