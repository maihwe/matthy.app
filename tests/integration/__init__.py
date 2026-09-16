"""Integration tests: real PostgreSQL, never SQLite (13 §3).

Every test in this package is marked ``integration`` and requires a database,
because the properties under test exist only there — CHECK constraints, unique
indexes, transactional foreign-key behaviour.
"""
