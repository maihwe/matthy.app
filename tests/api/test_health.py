"""Health endpoints — 12 §7.3.

12 §7.3 refuses a health check that only asserts the process is running, so
these tests assert the *distinction*: liveness stays 200 when a dependency is
down, readiness does not. A test that only saw the healthy path would pass
against an endpoint that never checked anything.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import resolve, reverse

from config import health

pytestmark = pytest.mark.api


def test_liveness_reports_ok_without_touching_dependencies() -> None:
    response = Client().get(reverse("health-live"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "check": "live"}


@pytest.mark.django_db
def test_readiness_reports_ok_when_postgres_and_redis_answer() -> None:
    response = Client().get(reverse("health-ready"))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"] == {"database": "ok", "cache": "ok"}


def test_liveness_is_still_ok_when_the_database_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_check_database", lambda: (False, "database unreachable"))
    response = Client().get(reverse("health-live"))
    assert response.status_code == 200


def test_readiness_is_503_when_the_database_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_check_database", lambda: (False, "database unreachable"))
    response = Client().get(reverse("health-ready"))
    assert response.status_code == 503
    assert response.json()["dependencies"]["database"] == "database unreachable"


@pytest.mark.django_db
def test_readiness_is_503_when_the_cache_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_check_cache", lambda: (False, "cache unreachable"))
    response = Client().get(reverse("health-ready"))
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_health_endpoints_are_get_only() -> None:
    assert Client().post(reverse("health-live")).status_code == 405
    assert Client().post(reverse("health-ready")).status_code == 405


def test_health_routes_resolve_to_the_documented_views() -> None:
    assert resolve("/health/live/").func is health.liveness
    assert resolve("/health/ready/").func is health.readiness
    assert reverse("health-live") == "/health/live/"
    assert reverse("health-ready") == "/health/ready/"


def test_the_api_prefix_is_versioned_and_included() -> None:
    from django.urls import URLResolver

    from config.urls import urlpatterns

    included = [
        pattern
        for pattern in urlpatterns
        if isinstance(pattern, URLResolver) and str(pattern.pattern) == "api/v1/"
    ]
    assert len(included) == 1
    assert included[0].namespace == "v1"
