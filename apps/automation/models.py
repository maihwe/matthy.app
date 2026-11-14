"""Durable, privately owned AI requests; Redis is only their delivery queue."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

MAX_ATTEMPTS = 3


class AutomationKind(models.TextChoices):
    SUPPORT_ANSWER = "SUPPORT_ANSWER", "Support answer"
    DRAFT_ANNOUNCEMENT = "DRAFT_ANNOUNCEMENT", "Announcement draft"


class AutomationStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class AutomationRequest(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.UUIDField()
    kind = models.CharField(max_length=32, choices=AutomationKind.choices)
    input_text = models.CharField(max_length=4000)
    language = models.CharField(
        max_length=2, choices=[("en", "English"), ("fr", "French"), ("sw", "Swahili")], default="en"
    )
    status = models.CharField(
        max_length=16, choices=AutomationStatus.choices, default=AutomationStatus.QUEUED
    )
    output_text = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    model = models.CharField(max_length=100, blank=True, default="")
    provider_response_id = models.CharField(max_length=255, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    attempt_token = models.UUIDField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "automation_requests"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="automation_dispatch_idx"),
            models.Index(fields=["user", "created_at"], name="automation_user_time_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"], name="automation_user_key_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=AutomationStatus.values),
                name="automation_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=AutomationKind.values), name="automation_kind_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__lte=MAX_ATTEMPTS), name="automation_attempts_bounded"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} ({self.public_id}): {self.status}"
