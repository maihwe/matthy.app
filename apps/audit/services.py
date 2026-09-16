"""The write path for ``audit_logs`` — FR-AUD-001…002, SEC-AUD-001…002.

**Read this before calling ``record()``.** An audit entry is not telemetry that
may be dropped under load. §44 rule 8 requires every important financial action
to be recorded, so the entry is written *inside the same transaction* as the
action it describes. If the record cannot be written, the action does not happen.
That is the whole point: an action that commits with no record of it is the one
state an audit trail cannot recover from.

The inverse — a record that commits while its action rolls back — is equally
bad, and is why ``record()`` refuses to run outside a transaction rather than
creating its own. A record of something that never happened is indistinguishable
from a record of something that did.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from django.db import transaction
from django.http import HttpRequest

from apps.audit.constants import ActorType
from apps.audit.models import (
    ACTION_MAX_LENGTH,
    OBJECT_ID_MAX_LENGTH,
    OBJECT_REPR_MAX_LENGTH,
    OBJECT_TYPE_MAX_LENGTH,
    USER_AGENT_MAX_LENGTH,
    AuditLog,
)

__all__ = ["AuditContext", "client_ip", "record"]

# `noun.verb_past` — `user.suspended`, `contribution.approved`,
# `disbursement.separation_of_duties_refused`. Lowercase, underscore-separated
# segments, at least two of them. Enforced rather than documented, because the
# action column is the only way to ask "who has suspended anyone this month?",
# and a stray capital or a space makes an entry unreachable by that query while
# looking perfectly fine in the admin.
_ACTION_PATTERN: Final = re.compile(r"\A[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\Z")


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Where the action came from — FR-AUD-001's "IP address … and device/session
    where appropriate".

    All four fields are optional because they are *where appropriate*: a
    scheduled job has no request, and a CLI command has no device. Recording
    null there is honest; inventing a placeholder would not be.
    """

    ip_address: str | None = None
    user_agent: str | None = None
    device_id: int | None = None
    session_id: int | None = None

    @classmethod
    def from_request(cls, request: HttpRequest) -> AuditContext:
        """Build a context from a request.

        Only ``ip_address`` and ``user_agent`` are taken. ``device_id`` and
        ``session_id`` are *not* read from the request: the attribute names for
        them are the authentication layer's to define (S4), and guessing them
        here would put a silent ``None`` in the one column an investigation
        uses to ask "had they revoked that device?".
        """
        return cls(
            ip_address=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT") or None,
        )


def client_ip(request: HttpRequest) -> str | None:
    """The client's IP address, from ``REMOTE_ADDR``, and nothing else.

    ``X-Forwarded-For`` is deliberately **not** consulted. It is a
    client-supplied header: anything behind a proxy that trusts it unconditionally
    is writing the attacker's chosen string into an immutable record, and an
    audit field that a caller can set is not evidence. The correct source depends
    on how many proxies actually sit in front of the app, which 12 has not yet
    specified — so this returns the socket peer, which is never wrong when
    untrusted and merely less useful when proxied.

    Behind a reverse proxy this will record the proxy's address until that is
    decided. Recorded as a blocking item in ``docs/PHASE_1_PLAN.md`` §6 rather
    than papered over with a guess.
    """
    return request.META.get("REMOTE_ADDR") or None


def _require_transaction() -> None:
    """An audit write must join the transaction of the action it describes.

    Checked at runtime rather than asserted in a comment, because the failure it
    prevents — an entry that survives its action rolling back — is silent, rare,
    and only appears under exactly the conditions (a mid-operation error) where
    the audit trail is being relied on.
    """
    if transaction.get_autocommit():
        raise RuntimeError(
            "record() must be called inside a transaction. The audit entry has to "
            "commit with the action it describes, or roll back with it; in autocommit "
            "mode it commits alone. Wrap the caller in transaction.atomic()."
        )


def _checked_identifier(value: str, *, field: str, limit: int) -> str:
    """Validate a column that identifies something.

    Identifiers raise on overflow rather than truncating. A truncated
    ``object_id`` points at the wrong object, and an audit entry that confidently
    names the wrong member is worse than one that failed to be written.
    """
    if not value:
        raise ValueError(f"AuditLog.{field} must not be empty.")
    if len(value) > limit:
        raise ValueError(
            f"AuditLog.{field} is {len(value)} characters; the column holds {limit}. "
            "Shorten it at the call site rather than truncating here."
        )
    return value


def _checked_values(value: Mapping[str, Any] | None, *, field: str) -> dict[str, Any] | None:
    """Validate that the before/after snapshot can actually be stored.

    ``JSONField`` uses the standard library's ``json.dumps`` with no custom
    encoder, so anything outside JSON's primitives fails *at insert*. In this
    project that is not hypothetical: money is ``Decimal`` everywhere, and a
    ``Decimal`` would abort the business action at its last statement. Catching it
    here names the field and the offending value instead.

    Money is therefore recorded as a decimal **string**, matching what the API
    speaks (04 §1.1) — not a float, which would put a rounding error into the
    permanent record of a cash handover.
    """
    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"AuditLog.{field} must contain only JSON primitives (str, int, float, "
            f"bool, None, list, dict). Got {value!r}: {exc}. Pass Decimals as strings."
        ) from exc
    return dict(value)


def _truncated(value: str | None, *, limit: int) -> str | None:
    """Descriptive fields truncate instead of raising.

    ``object_repr`` is a human-readable label and ``user_agent`` is a header the
    client chooses; a 600-character User-Agent must not be able to abort a
    member's contribution. Nothing depends on either being complete, so losing a
    tail is the cheaper failure — unlike an identifier, which is never truncated.
    """
    if value is None:
        return None
    return value[:limit]


def record(
    *,
    action: str,
    actor_type: ActorType,
    object_type: str,
    object_id: str | int,
    object_repr: str = "",
    actor_user_id: int | None = None,
    previous_value: Mapping[str, Any] | None = None,
    new_value: Mapping[str, Any] | None = None,
    context: AuditContext | None = None,
) -> AuditLog:
    """Write one immutable audit entry. Must be called inside a transaction.

    ``actor_type`` has no default, on purpose — see ``apps.audit.constants``. A
    default would let a caller who forgot to choose file a human action under
    "the system did it", which is the one misattribution an audit trail cannot
    recover from.

    The actor is also cross-checked against the type, because the two are
    redundant and redundancy that can disagree is worth a check: ``SYSTEM``
    entries name no user, and ``USER``/``ADMIN`` entries must name one. That
    makes "an operator suspended this member" and "a lockout sweep did"
    structurally different rows rather than two rows a reader has to
    interpret.

    **``previous_value`` and ``new_value`` will hold personal data, and that is
    an open question, not a settled one.** FR-AUD-002 names "user changed phone
    number" as an auditable action, so these columns are *designed* to contain
    the data a data subject can ask to have erased — in a table FR-AUD-003 makes
    permanent. Nothing here mitigates that, deliberately: masking or encrypting
    the values is a decision for the repository owner, and doing it silently
    would be inventing a rule. It is recorded as **C4** in
    ``docs/PHASE_1_PLAN.md`` §7. Until it is answered, a caller should not record
    a raw phone number, national identity number, or account number here.
    """
    if not _ACTION_PATTERN.match(action):
        raise ValueError(
            f"Audit action {action!r} must be 'noun.verb_past' — lowercase segments "
            "separated by dots, e.g. 'user.suspended'. It is an identifier, not prose."
        )
    if len(action) > ACTION_MAX_LENGTH:
        raise ValueError(
            f"Audit action {action!r} is {len(action)} characters; the column holds "
            f"{ACTION_MAX_LENGTH}."
        )

    if actor_type == ActorType.SYSTEM:
        if actor_user_id is not None:
            raise ValueError(
                "A SYSTEM audit entry must not name a user. If a person caused this, "
                "the actor_type is USER or ADMIN."
            )
    elif actor_user_id is None:
        raise ValueError(
            f"A {actor_type} audit entry must name the user who acted; got none. "
            "An unattributable human action is not an audit record."
        )

    _require_transaction()

    return AuditLog.objects.create(
        action=action,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        object_type=_checked_identifier(
            object_type, field="object_type", limit=OBJECT_TYPE_MAX_LENGTH
        ),
        object_id=_checked_identifier(
            str(object_id), field="object_id", limit=OBJECT_ID_MAX_LENGTH
        ),
        object_repr=_truncated(object_repr, limit=OBJECT_REPR_MAX_LENGTH) or "",
        previous_value=_checked_values(previous_value, field="previous_value"),
        new_value=_checked_values(new_value, field="new_value"),
        ip_address=context.ip_address if context else None,
        user_agent=_truncated(
            context.user_agent if context else None, limit=USER_AGENT_MAX_LENGTH
        ),
        device_id=context.device_id if context else None,
        session_id=context.session_id if context else None,
    )
