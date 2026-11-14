from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.automation import services
from apps.automation.models import AutomationRequest
from tests.api.test_browser_mfa import _finish, _password, _signed_in, _staff, _submit
from tests.factories import UserFactory

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]
URL = "/admin/ai/"


@pytest.fixture(autouse=True)
def security_settings(monkeypatch):
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "AI_AUTOMATION_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "synthetic-dashboard-test-key")
    monkeypatch.setattr(services, "enqueue", lambda public_id: None)


@pytest.fixture
def client():
    return APIClient(
        enforce_csrf_checks=True, REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}"
    )


def body(**changes):
    return {
        "kind": "SUPPORT_ANSWER",
        "input_text": "How do invitations work?",
        "language": "en",
        "idempotency_key": str(uuid.uuid4()),
        **changes,
    }


def job(user, **changes):
    return AutomationRequest.objects.create(user=user, **body(**changes))


def test_anonymous_dashboard_redirects_to_existing_sign_in(client):
    response = client.get(URL)
    assert response.status_code == 302
    assert response["Location"] == "/admin/login/?next=/admin/ai/"


def test_password_only_session_cannot_open_dashboard(client):
    user, _, codes = _staff()
    _password(client, user, next=URL)
    assert client.get(URL).status_code == 302
    assert _finish(client, codes[0])["Location"] == URL
    assert client.get(URL).status_code == 200


def test_dashboard_has_brand_tools_and_private_history(client):
    user, _, _, _ = _signed_in(client)
    own = job(user, input_text="Our group's invitation question")
    job(UserFactory(), input_text="Another user's private question")
    response = client.get(URL)
    assert response.status_code == 200
    assert b"Your AI workspace" in response.content
    assert b"IHWEZA" in response.content
    assert b"Announcement details" not in response.content
    assert str(own.public_id).encode() in response.content
    assert b"Another user" not in response.content
    assert "no-store" in response["Cache-Control"]
    assert "private" in response["Cache-Control"]


def test_admin_navigation_links_to_workspace(client):
    _signed_in(client)
    assert b'href="/admin/ai/"' in client.get("/admin/").content


@pytest.mark.parametrize("kind", ["SUPPORT_ANSWER", "DRAFT_ANNOUNCEMENT"])
def test_form_creates_owned_job_then_redirects_to_result(client, kind):
    user, _, _, _ = _signed_in(client)
    response = _submit(client, URL, **body(kind=kind, language="fr"))
    assert response.status_code == 302
    created = AutomationRequest.objects.get(user=user)
    assert response["Location"] == f"{URL}?request={created.public_id}"
    assert created.language == "fr"
    page = client.get(response["Location"])
    assert page.status_code == 200
    assert page.context_data["selected_job"] == created
    assert page.context_data["form"]["kind"].value() == kind
    assert (
        f'data-poll-url="{reverse("v1:automation-detail", args=[created.public_id])}"'.encode()
        in page.content
    )


def test_browser_post_requires_csrf(client):
    _signed_in(client)
    assert client.post(URL, body()).status_code == 403
    assert not AutomationRequest.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"input_text": " "},
        {"input_text": "x" * 4001},
        {"kind": "MOVE_MONEY"},
        {"language": "invalid"},
        {"idempotency_key": "invalid"},
    ],
)
def test_invalid_forms_show_errors_without_creating_work(client, changes):
    _signed_in(client)
    response = _submit(client, URL, **body(**changes))
    assert response.status_code == 400
    assert b"Please check your request" in response.content
    assert not AutomationRequest.objects.exists()


@pytest.mark.parametrize("request_id", ["invalid", str(uuid.uuid4())])
def test_unknown_or_malformed_result_is_not_exposed(client, request_id):
    _signed_in(client)
    assert client.get(URL, {"request": request_id}).status_code == 404


def test_other_users_result_is_not_exposed(client):
    _signed_in(client)
    other = job(UserFactory())
    assert client.get(URL, {"request": str(other.public_id)}).status_code == 404


def test_saved_inputs_and_ai_outputs_are_escaped(client):
    user, _, _, _ = _signed_in(client)
    saved = job(
        user,
        input_text='<script>alert("input")</script>',
        status="SUCCEEDED",
        output_text='<img src=x onerror="alert(1)">',
        completed_at=timezone.now(),
    )
    response = client.get(URL, {"request": str(saved.public_id)})
    assert b'<script>alert("input")</script>' not in response.content
    assert b"&lt;script&gt;" in response.content
    assert b"<img src=x" not in response.content
    assert b"&lt;img" in response.content


def test_unavailable_ai_keeps_history_readable_and_blocks_new_work(client, monkeypatch):
    user, _, _, _ = _signed_in(client)
    saved = job(user, status="SUCCEEDED", output_text="Saved answer", completed_at=timezone.now())
    monkeypatch.setattr(settings, "AI_AUTOMATION_ENABLED", False)
    page = client.get(URL, {"request": str(saved.public_id)})
    assert b"Saved answer" in page.content
    assert b"Currently unavailable" in page.content
    assert b'id="submit-button" disabled' in page.content
    assert _submit(client, URL, **body()).status_code == 503
    assert AutomationRequest.objects.count() == 1


def test_limit_is_enforced_and_retry_after_is_preserved(client, monkeypatch):
    user, _, _, _ = _signed_in(client)
    monkeypatch.setattr(settings, "AI_DAILY_REQUEST_LIMIT", 1)
    job(user)
    response = _submit(client, URL, **body())
    assert response.status_code == 429
    assert int(response["Retry-After"]) > 0
    assert AutomationRequest.objects.count() == 1


def test_browser_retry_is_idempotent_and_changed_body_is_conflict(client):
    _signed_in(client)
    request_body = body()
    first = _submit(client, URL, **request_body)
    second = _submit(client, URL, **request_body)
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"]
    assert (
        _submit(client, URL, **{**request_body, "input_text": "Different question"}).status_code
        == 409
    )
    assert AutomationRequest.objects.count() == 1


def test_revoked_session_cannot_open_dashboard_or_poll_result(client):
    user, _, _, session = _signed_in(client)
    saved = job(user)
    type(session).objects.filter(pk=session.pk).update(
        revoked_at=timezone.now(), revoked_reason="REVOKED"
    )
    assert client.get(URL).status_code == 302
    assert client.get(reverse("v1:automation-detail", args=[saved.public_id])).status_code == 401


def test_private_poll_result_disallows_caching(client):
    user, _, _, _ = _signed_in(client)
    saved = job(user)
    response = client.get(reverse("v1:automation-detail", args=[saved.public_id]))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_draft_tool_can_be_selected_and_unknown_tool_falls_back(client):
    _signed_in(client)
    page = client.get(URL, {"kind": "DRAFT_ANNOUNCEMENT"})
    assert b"Announcement details" in page.content
    assert page.context_data["form"]["kind"].value() == "DRAFT_ANNOUNCEMENT"
    page = client.get(URL, {"kind": "INVALID"})
    assert page.context_data["form"]["kind"].value() == "SUPPORT_ANSWER"
