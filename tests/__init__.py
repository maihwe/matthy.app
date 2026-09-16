"""Tests package.

Layout follows 13 §4: ``unit/``, ``integration/``, ``api/``, ``concurrency/``,
``security/``, ``reconciliation/``, ``e2e/``, with shared ``factories/`` and
``assertions/`` packages.

Directories are created by the slice that first has a test to put in them,
rather than up front — an empty package is a placeholder that reads like
coverage.
"""
