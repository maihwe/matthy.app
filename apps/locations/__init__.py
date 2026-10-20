"""The ``locations`` module (slice S3). Layer L0.

Place, not identity: countries, their currency, the administrative tiers a
country defines, and the tree of places inside it. It depends on nothing else,
which is what lets ``accounts`` and ``groups`` hold a single ``location_id``
rather than five denormalised columns (03 §4.5.1, §5.2.1).
"""
