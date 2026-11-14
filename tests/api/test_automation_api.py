from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.automation import services
from apps.automation.models import AutomationRequest, AutomationStatus
from apps.automation.tasks import dispatch_automations, run_automation
from integrations.openai import AIProviderError, AIResponse, OpenAIClient
from tests.factories import UserFactory

pytestmark = [pytest.mark.api, pytest.mark.django_db]
URL = "/api/v1/ai/automations/"


@pytest.fixture(autouse=True)
def ai_settings(monkeypatch):
    monkeypatch.setattr(settings, "AI_AUTOMATION_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "synthetic-test-key")


@pytest.fixture
def actor():
    return UserFactory()


@pytest.fixture
def client(actor):
    value = APIClient()
    value.force_authenticate(user=actor)
    return value


def payload(**changes):
    return {
        "kind": "SUPPORT_ANSWER",
        "input_text": "How do group invitations work?",
        "idempotency_key": str(uuid.uuid4()),
        **changes,
    }


def submit(client, **changes):
    response = client.post(URL, payload(**changes), format="json")
    assert response.status_code == 202, response.json()
    return AutomationRequest.objects.get(public_id=response.json()["data"]["public_id"])


def test_authentication_required():
    assert APIClient().post(URL, payload(), format="json").status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "TRANSFER_MONEY"),
        ("input_text", " "),
        ("input_text", "x" * 4001),
        ("language", "unknown"),
        ("idempotency_key", "invalid"),
    ],
)
def test_invalid_requests_write_nothing(client, field, value):
    response = client.post(URL, payload(**{field: value}), format="json")
    assert response.status_code == 400
    assert not AutomationRequest.objects.exists()


@pytest.mark.parametrize(
    "attribute,value", [("AI_AUTOMATION_ENABLED", False), ("OPENAI_API_KEY", "")]
)
def test_unconfigured_ai_writes_nothing(client, monkeypatch, attribute, value):
    monkeypatch.setattr(settings, attribute, value)
    response = client.post(URL, payload(), format="json")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ai_unavailable"
    assert not AutomationRequest.objects.exists()


def test_queue_delivery_happens_only_after_commit(
    client, monkeypatch, django_capture_on_commit_callbacks
):
    enqueue = Mock()
    monkeypatch.setattr(services, "enqueue", enqueue)
    with django_capture_on_commit_callbacks(execute=True):
        job = submit(client)
        enqueue.assert_not_called()
        assert AuditLog.objects.filter(action="automation.requested").exists()
    enqueue.assert_called_once_with(str(job.public_id))


def test_idempotency_reuses_request_and_rejects_changed_content(client, monkeypatch):
    body = payload()
    assert client.post(URL, body, format="json").status_code == 202
    monkeypatch.setattr(settings, "AI_AUTOMATION_ENABLED", False)
    assert client.post(URL, body, format="json").status_code == 200
    body["input_text"] = "Different question"
    assert client.post(URL, body, format="json").status_code == 409
    assert AutomationRequest.objects.count() == 1


def test_usage_limit_counts_under_user_lock(client, monkeypatch):
    monkeypatch.setattr(settings, "AI_DAILY_REQUEST_LIMIT", 1)
    job = submit(client)
    limited = client.post(URL, payload(), format="json")
    assert limited.status_code == 429
    assert 1 <= int(limited["Retry-After"]) <= 86401
    AutomationRequest.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    assert client.post(URL, payload(), format="json").status_code == 202


def test_other_users_cannot_read_results(client):
    job = submit(client)
    other = APIClient()
    other.force_authenticate(user=UserFactory())
    assert other.get(f"{URL}{job.public_id}/").status_code == 404
    assert client.get(f"{URL}{job.public_id}/").status_code == 200


@pytest.mark.parametrize("kind", ["SUPPORT_ANSWER", "DRAFT_ANNOUNCEMENT"])
def test_worker_generates_private_result_once(client, actor, kind, monkeypatch):
    job = submit(client, kind=kind, language="fr")
    generate = Mock(return_value=AIResponse("Une réponse utile.", "resp_test"))
    monkeypatch.setattr(OpenAIClient, "generate", generate)
    run_automation(str(job.public_id))
    run_automation(str(job.public_id))
    generate.assert_called_once()
    assert actor.phone not in str(generate.call_args)
    assert actor.email not in str(generate.call_args)
    data = client.get(f"{URL}{job.public_id}/").json()["data"]
    assert data["status"] == "SUCCEEDED"
    assert data["output_text"] == "Une réponse utile."
    assert data["completed_at"]
    assert AuditLog.objects.filter(action="automation.completed").count() == 1


def test_retryable_failure_is_bounded_and_audited(client, monkeypatch):
    job = submit(client)
    generate = Mock(side_effect=AIProviderError("provider_unavailable", retryable=True))
    monkeypatch.setattr(OpenAIClient, "generate", generate)
    for _ in range(4):
        services.run(str(job.public_id))
    job.refresh_from_db()
    assert job.status == AutomationStatus.FAILED
    assert job.attempts == generate.call_count == 3
    assert job.error_code == "provider_unavailable"
    assert AuditLog.objects.filter(action="automation.failed").count() == 1


def test_permanent_failure_is_not_retried(client, monkeypatch):
    job = submit(client)
    monkeypatch.setattr(
        OpenAIClient, "generate", Mock(side_effect=AIProviderError("invalid_provider_response"))
    )
    services.run(str(job.public_id))
    job.refresh_from_db()
    assert job.status == AutomationStatus.FAILED
    assert job.attempts == 1


def test_lease_recovers_crashed_workers_and_ignores_active_work(client, monkeypatch):
    job = submit(client)
    AutomationRequest.objects.filter(pk=job.pk).update(
        status="RUNNING", started_at=timezone.now(), attempts=1
    )
    generate = Mock(return_value=AIResponse("Recovered", "resp_recovery"))
    monkeypatch.setattr(OpenAIClient, "generate", generate)
    services.run(str(job.public_id))
    generate.assert_not_called()
    AutomationRequest.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - timedelta(minutes=6)
    )
    services.run(str(job.public_id))
    job.refresh_from_db()
    assert job.status == AutomationStatus.SUCCEEDED
    assert job.attempts == 2


def test_late_worker_cannot_overwrite_new_attempt(client, monkeypatch):
    job = submit(client)

    def late_result(*args, **kwargs):
        AutomationRequest.objects.filter(pk=job.pk).update(attempt_token=uuid.uuid4())
        return AIResponse("Stale output", "resp_late")

    monkeypatch.setattr(OpenAIClient, "generate", late_result)
    services.run(str(job.public_id))
    job.refresh_from_db()
    assert job.output_text == ""
    assert not AuditLog.objects.filter(action="automation.completed").exists()


def test_dispatcher_recovers_broker_failures(
    client, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(run_automation, "delay", Mock(side_effect=OSError("broker unavailable")))
    with django_capture_on_commit_callbacks(execute=True):
        job = submit(client)
    job.refresh_from_db()
    assert job.status == AutomationStatus.QUEUED
    delivered = Mock()
    monkeypatch.setattr(run_automation, "delay", delivered)
    assert dispatch_automations() == 1
    delivered.assert_called_once_with(str(job.public_id))


def test_transaction_rollback_discards_job_audit_and_queue(actor, monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr(services, "enqueue", enqueue)
    with pytest.raises(RuntimeError), transaction.atomic():
        services.submit(
            user=actor,
            kind="SUPPORT_ANSWER",
            input_text="Question",
            language="en",
            idempotency_key=uuid.uuid4(),
        )
        raise RuntimeError("Rollback")
    assert not AutomationRequest.objects.exists()
    assert not AuditLog.objects.filter(action="automation.requested").exists()
    enqueue.assert_not_called()


@pytest.mark.parametrize("changes", [{"account_status": "SUSPENDED"}, {"is_phone_verified": False}])
def test_worker_rechecks_account_state_before_provider_call(client, actor, monkeypatch, changes):
    job = submit(client)
    type(actor).objects.filter(pk=actor.pk).update(**changes)
    generate = Mock()
    monkeypatch.setattr(OpenAIClient, "generate", generate)
    services.run(str(job.public_id))
    generate.assert_not_called()
    job.refresh_from_db()
    assert job.status == AutomationStatus.FAILED


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_delivery_claims_one_provider_call(client, monkeypatch):
    monkeypatch.setattr(services, "enqueue", lambda public_id: None)
    job = submit(client)
    started, release = Event(), Event()

    def generate(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return AIResponse("One accepted result", "resp_once")

    provider = Mock(side_effect=generate)
    monkeypatch.setattr(OpenAIClient, "generate", provider)

    def worker():
        try:
            services.run(str(job.public_id))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert started.wait(10)
            services.run(str(job.public_id))
            provider.assert_called_once()
        finally:
            release.set()
        future.result(timeout=10)
    job.refresh_from_db()
    assert job.status == AutomationStatus.SUCCEEDED
