"""Submit, claim and complete AI work without holding locks over network calls."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.constants import ActorType
from apps.audit.services import AuditContext, record
from apps.automation import prompts
from apps.automation.errors import AutomationConflict, AutomationLimitReached, AutomationUnavailable
from apps.automation.models import MAX_ATTEMPTS, AutomationKind, AutomationRequest, AutomationStatus
from integrations.openai import AIProviderError, OpenAIClient

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=5)


def available() -> bool:
    return bool(settings.AI_AUTOMATION_ENABLED and settings.OPENAI_API_KEY)


def submit(
    *,
    user: User,
    kind: str,
    input_text: str,
    language: str,
    idempotency_key: uuid.UUID,
    context: AuditContext | None = None,
) -> tuple[AutomationRequest, bool]:
    if (
        kind not in AutomationKind.values
        or language not in ("en", "fr", "sw")
        or not input_text.strip()
        or len(input_text) > 4000
    ):
        raise ValueError("Invalid automation input")
    with transaction.atomic():
        actor = User.objects.select_for_update().get(pk=user.pk)
        if not actor.is_active or not actor.is_phone_verified:
            raise PermissionDenied("A verified, active account is required.")
        existing = AutomationRequest.objects.filter(
            user=actor, idempotency_key=idempotency_key
        ).first()
        if existing:
            if (existing.kind, existing.input_text, existing.language) != (
                kind,
                input_text,
                language,
            ):
                raise AutomationConflict
            return existing, False
        if not available():
            raise AutomationUnavailable
        recent = AutomationRequest.objects.filter(
            user=actor, created_at__gte=timezone.now() - timedelta(days=1)
        )
        if recent.count() >= settings.AI_DAILY_REQUEST_LIMIT:
            oldest = recent.order_by("created_at").values_list("created_at", flat=True).first()
            retry_after = max(
                1,
                int(
                    (
                        (oldest or timezone.now()) + timedelta(days=1) - timezone.now()
                    ).total_seconds()
                )
                + 1,
            )
            raise AutomationLimitReached(retry_after)
        job = AutomationRequest.objects.create(
            user=actor,
            kind=kind,
            input_text=input_text,
            language=language,
            idempotency_key=idempotency_key,
        )
        record(
            action="automation.requested",
            actor_type=ActorType.USER,
            actor_user_id=actor.pk,
            object_type="automation_request",
            object_id=job.public_id.hex,
            new_value={"kind": kind, "prompt_version": prompts.PROMPT_VERSION},
            context=context,
        )
        transaction.on_commit(lambda: enqueue(str(job.public_id)))
        return job, True


def enqueue(public_id: str) -> None:
    from apps.automation.tasks import run_automation

    try:
        run_automation.delay(public_id)
    except Exception:
        # The committed SQL row is an outbox; the scheduled dispatcher retries it.
        # Never log exception bodies: broker URLs may contain credentials.
        logger.warning("AI request %s awaits queue delivery", public_id)


def dispatch_pending() -> int:
    if not available():
        return 0
    pending = (
        AutomationRequest.objects.filter(
            Q(status=AutomationStatus.QUEUED)
            | Q(status=AutomationStatus.RUNNING, started_at__lt=timezone.now() - LEASE)
        )
        .order_by("created_at")
        .values_list("public_id", flat=True)[:100]
    )
    ids = list(pending)
    for public_id in ids:
        enqueue(str(public_id))
    return len(ids)


def run(public_id: str) -> None:
    with transaction.atomic():
        job = (
            AutomationRequest.objects.select_for_update()
            .select_related("user")
            .filter(public_id=public_id)
            .first()
        )
        if job is None or job.status in (AutomationStatus.SUCCEEDED, AutomationStatus.FAILED):
            return
        if (
            job.status == AutomationStatus.RUNNING
            and job.started_at
            and job.started_at > timezone.now() - LEASE
        ):
            return
        if (
            not available()
            or not job.user.is_active
            or not job.user.is_phone_verified
            or job.attempts >= MAX_ATTEMPTS
        ):
            _finish(
                job,
                AutomationStatus.FAILED,
                error_code="ai_unavailable" if not available() else "request_expired",
            )
            return
        job.status = AutomationStatus.RUNNING
        job.started_at = timezone.now()
        job.attempt_token = uuid.uuid4()
        job.attempts += 1
        job.model = settings.OPENAI_MODEL
        job.save(update_fields=["status", "started_at", "attempt_token", "attempts", "model"])
        token = job.attempt_token

    try:
        result = OpenAIClient(
            api_key=settings.OPENAI_API_KEY,
            model=job.model,
            timeout=settings.AI_TIMEOUT_SECONDS,
            max_output_tokens=settings.AI_MAX_OUTPUT_TOKENS,
        ).generate(
            instructions=prompts.instructions(job.kind),
            input_text=json.dumps({"language": job.language, "text": job.input_text}),
        )
    except AIProviderError as exc:
        with transaction.atomic():
            current = AutomationRequest.objects.select_for_update().get(pk=job.pk)
            if current.attempt_token != token or current.status != AutomationStatus.RUNNING:
                return
            if exc.retryable and current.attempts < MAX_ATTEMPTS:
                current.status = AutomationStatus.QUEUED
                current.error_code = exc.code
                current.save(update_fields=["status", "error_code"])
            else:
                _finish(current, AutomationStatus.FAILED, error_code=exc.code)
        return

    with transaction.atomic():
        current = AutomationRequest.objects.select_for_update().get(pk=job.pk)
        if current.attempt_token != token or current.status != AutomationStatus.RUNNING:
            return
        current.output_text = result.text
        current.provider_response_id = result.response_id
        _finish(current, AutomationStatus.SUCCEEDED)


def _finish(job: AutomationRequest, status: str, *, error_code: str = "") -> None:
    job.status = status
    job.error_code = error_code
    job.completed_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "error_code",
            "completed_at",
            "output_text",
            "provider_response_id",
        ]
    )
    record(
        action="automation.completed"
        if status == AutomationStatus.SUCCEEDED
        else "automation.failed",
        actor_type=ActorType.SYSTEM,
        object_type="automation_request",
        object_id=job.public_id.hex,
        new_value={
            "status": status,
            "error_code": error_code,
            "attempts": job.attempts,
            "model": job.model,
        },
    )
