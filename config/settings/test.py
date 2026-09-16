"""Test settings — synthetic data, created and destroyed per run (02 §8, 12 §1).

13 §3 is the constraint that shapes this file: **integration tests run against
real PostgreSQL, never SQLite.** The schema depends on ``CHECK`` constraints,
deferred constraint triggers, ``GENERATED`` columns and ``JSONB``. A suite on
SQLite would pass while the production invariants never fired — a green build
proving the opposite of what it claims.

The database name is suffixed so that a mistaken run against a running
development stack cannot truncate the development database. 12 §1: production
is never a development target, and the same reasoning applies one level down.
"""

from __future__ import annotations

from config.settings.base import *  # noqa: F403
from config.settings.base import DATABASES, env

DEBUG = False
"""False, so that a test exercising an error path sees the same response shape
production produces rather than a Django debug page (12 §4)."""

DATABASES["default"]["NAME"] = env("POSTGRES_TEST_DB", default="test_matthy")
DATABASES["default"]["CONN_MAX_AGE"] = 0
"""No connection reuse between tests: a stale connection holding a lock is a
source of concurrency-test flakiness, and 13 §5 Y-DET-008 requires every test
to be independent."""

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
"""Argon2id is deliberately slow, and the suite creates a user in most tests.
Hashing strength is a production property, not a test one (SEC-AUTH-001), and
the production value is asserted directly against `config.settings.production`
rather than being inferred from this module."""

CACHES["default"]["KEY_PREFIX"] = "ihweza-test"  # noqa: F405

# 13 §5 Y-DET-007: no outbound network. Any test that tries is meant to fail.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = True
"""Tasks run in-process so a test does not need a worker. 13 §5 Y-DET-006: a
test that slept waiting for a result would be non-deterministic by
construction."""

CELERY_TASK_EAGER_PROPAGATES = True

# RATE_LIMITS is inherited from base unchanged, at 10 §6.4's documented values.
#
# An earlier draft of this file carried a global ``RATELIMIT_ENABLE`` flag so
# that one test could not trip a limit another test relied on. It was removed
# for two reasons. It made "are the limits on?" a runtime question that every
# call site had to honour, and a flag that turns a security control off is
# exactly the flag that ends up set in production. And it did not solve the
# problem it was written for: counters live in Redis and outlive the test run,
# so two runs of the same suite shared them.
#
# Tests key the limiter on a per-test unique identifier instead, which is
# deterministic and needs no switch. A test that needs a different ceiling sets
# it for itself with `override_settings(RATE_LIMITS=...)` — and one that does is
# then visibly asserting against a value it chose.
#
# Note that RATE_LIMITS must not be *mutated* here even though `import *` puts it
# in scope: it is the same dict object as base's, so an item assignment in this
# module would rewrite the base settings for every process that imports them.

# LOCKOUT (SEC-LOCK-001's backoff) is likewise inherited unchanged, and likewise
# has no enable/disable flag. The supported way to test a lockout is to install a
# smaller schedule for that one test, which is what
# `tests/integration/test_lockout_service.py` does: a three-failure threshold with
# two-second windows is both faster than the shipped schedule and stricter about
# it, because the exact deadline can be asserted instead of "at least a minute".
# `tests/unit/test_settings_contract.py` asserts this module does not weaken it.

LOGGING["root"]["level"] = "WARNING"  # noqa: F405
LOGGING["loggers"]["django.request"]["level"] = "ERROR"  # noqa: F405
