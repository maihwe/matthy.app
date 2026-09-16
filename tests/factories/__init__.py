"""Factories (13 §4, Y-DAT-001).

Every fixture in the suite is built here rather than inline, so that a schema
change is a change in one place instead of a search-and-replace across the test
tree.

Two conventions apply to every factory in this project:

* **Reserved domains and numbers only.** Emails use ``.test``, which RFC 2606
  reserves and no registrar can sell. A factory that generated
  ``member@gmail.com`` would eventually send a real person a verification email
  from a test run — and Y-DAT-003 requires fixtures to be synthetic, not
  anonymised copies of production rows.
* **Explicit credentials.** A factory password is a visibly fake constant, not
  a realistic-looking one, so that a reviewer scanning a diff cannot mistake it
  for a leaked secret (SEC-SEC-001).

Every name a factory module defines is re-exported here, including the ones no
test has needed yet. An unexported factory is a factory that gets written a
second time, inline, by whoever next needs that state — which is the outcome this
module exists to prevent.
"""

from tests.factories.accounts import (
    DeviceFactory,
    OTPCodeFactory,
    SuspendedUserFactory,
    UnverifiedUserFactory,
    UserFactory,
    UserProfileFactory,
    UserSessionFactory,
    VerificationTokenFactory,
)
from tests.factories.audit import AuditLogFactory, UserAuditLogFactory, audit_values

__all__ = [
    "AuditLogFactory",
    "DeviceFactory",
    "OTPCodeFactory",
    "SuspendedUserFactory",
    "UnverifiedUserFactory",
    "UserAuditLogFactory",
    "UserFactory",
    "UserProfileFactory",
    "UserSessionFactory",
    "VerificationTokenFactory",
    "audit_values",
]
