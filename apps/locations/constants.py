"""Closed vocabularies for the ``locations`` module.

`RoundingMode` lives here rather than in a financial module because 03 §4.5.2
puts it on ``currencies``: rounding is a property of the currency, beside its
exponent. The financial modules will read it through ``Currency`` rather than
restating the vocabulary.
"""

from __future__ import annotations

from django.db import models

__all__ = ["MAX_EMBEDDED_CHILDREN", "RoundingMode"]

# How many children a location detail response embeds. A state can hold 774
# LGAs; the detail endpoint is a browsing aid, not an export, and a client that
# wants the whole level pages through `/locations/?parent=`.
MAX_EMBEDDED_CHILDREN = 100


class RoundingMode(models.TextChoices):
    """How a rational becomes a whole minor unit — 06 §3.4.1.

    The odd pair is ``DOWN`` and ``TOWARD_ZERO``. They are not synonyms in the
    06 table: ``DOWN`` is the intermediate-allocation rule and ``TOWARD_ZERO``
    is an explicit truncation, and the document names both. Keeping both makes
    the column a faithful copy of the specification; collapsing them would make
    a later jurisdiction's mandated mode unrepresentable without a migration.
    """

    HALF_UP = "HALF_UP", "Half away from zero"
    HALF_EVEN = "HALF_EVEN", "Half to even (banker's)"
    DOWN = "DOWN", "Down"
    UP = "UP", "Up"
    TOWARD_ZERO = "TOWARD_ZERO", "Truncate toward zero"
