"""Location API contracts — 04 §4, FR-LOC-002…005.

Public reads, cursor-paginated, in the envelope of 04 §1.7. The tests assert
the things a client depends on: the identifier is the public UUID, the country
code is ISO alpha-2, the tree modes are distinguishable, and the anonymous read
limit answers 429 with ``Retry-After`` rather than an unthrottled success.
"""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from apps.locations.models import Country, Location, LocationType
from config.ratelimit import Decision
from tests.factories.accounts import UserFactory
from tests.factories.locations import LocationFactory

pytestmark = [pytest.mark.api, pytest.mark.django_db]


def _nigeria() -> Country:
    return Country.objects.get(iso2="NG")


def _tier(code: str) -> LocationType:
    return LocationType.objects.get(country=_nigeria(), code=code)


def _lagos() -> Location:
    return Location.objects.get(slug="lagos")


class TestCountryEndpoints:
    def test_countries_are_listed_with_their_currency(self) -> None:
        response = APIClient().get("/api/v1/locations/countries/")
        assert response.status_code == 200
        body = response.json()
        assert body["pagination"]["has_more"] is False
        nigeria = next(row for row in body["data"] if row["iso2"] == "NG")
        assert nigeria["currency"] == "NGN"
        assert nigeria["dial_code"] == "+234"

    def test_a_country_lists_its_tiers_in_level_order(self) -> None:
        response = APIClient().get("/api/v1/locations/countries/ng/types/")
        assert response.status_code == 200
        assert [row["code"] for row in response.json()["data"]] == [
            "STATE",
            "LGA",
            "CITY",
            "AREA",
            "STREET",
        ]

    def test_the_country_code_is_case_insensitive(self) -> None:
        response = APIClient().get("/api/v1/locations/countries/NG/types/")
        assert response.status_code == 200

    def test_an_unknown_country_is_a_not_found_envelope(self) -> None:
        response = APIClient().get("/api/v1/locations/countries/zz/types/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_an_inactive_country_is_not_listed(self) -> None:
        Country.objects.filter(iso2="NG").update(is_active=False)
        body = APIClient().get("/api/v1/locations/countries/").json()
        assert body["data"] == []


class TestLocationList:
    def test_the_default_list_is_the_top_level(self) -> None:
        response = APIClient().get("/api/v1/locations/")
        assert response.status_code == 200
        rows = response.json()["data"]
        assert len(rows) == 37
        assert {row["level"] for row in rows} == {1}
        assert {row["country"] for row in rows} == {"NG"}

    def test_parent_lists_the_next_level_down(self) -> None:
        lagos = _lagos()
        Location.objects.create(
            country=lagos.country,
            location_type=_tier("LGA"),
            parent=lagos,
            name="Ikeja",
            slug="ikeja",
        )
        response = APIClient().get(f"/api/v1/locations/?parent={lagos.public_id}")
        assert response.status_code == 200
        rows = response.json()["data"]
        assert [row["name"] for row in rows] == ["Ikeja"]
        assert rows[0]["parent_id"] == str(lagos.public_id)

    def test_search_matches_at_any_level(self) -> None:
        lagos = _lagos()
        Location.objects.create(
            country=lagos.country,
            location_type=_tier("LGA"),
            parent=lagos,
            name="Ikeja",
            slug="ikeja-search",
        )
        body = APIClient().get("/api/v1/locations/?search=ikej").json()
        assert [row["slug"] for row in body["data"]] == ["ikeja-search"]

    def test_type_and_country_narrow_the_result(self) -> None:
        body = APIClient().get("/api/v1/locations/?type=state&country=ng").json()
        assert len(body["data"]) == 37
        assert all(row["type"] == "STATE" for row in body["data"])

    def test_an_unknown_parent_is_a_not_found(self) -> None:
        response = APIClient().get(f"/api/v1/locations/?parent={uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_a_malformed_parent_is_a_validation_error(self) -> None:
        response = APIClient().get("/api/v1/locations/?parent=lagos")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_an_inactive_location_is_absent_from_lists(self) -> None:
        lagos = _lagos()
        Location.objects.filter(pk=lagos.pk).update(is_active=False)
        body = APIClient().get("/api/v1/locations/?search=lagos").json()
        assert body["data"] == []

    def test_the_limit_parameter_is_honoured(self) -> None:
        body = APIClient().get("/api/v1/locations/?limit=5").json()
        assert len(body["data"]) == 5
        assert body["pagination"]["has_more"] is True
        assert body["pagination"]["next_cursor"]

    def test_the_limit_parameter_is_capped(self) -> None:
        """04 §1.4's maximum is a contract, not advice: an unbounded client
        would be able to ask for the whole table in one response."""
        body = APIClient().get("/api/v1/locations/?limit=100000").json()
        assert body["pagination"]["limit"] == 200
        assert len(body["data"]) <= 200


class TestLocationDetail:
    def test_the_detail_exposes_the_public_id_not_the_integer_key(self) -> None:
        lagos = _lagos()
        body = APIClient().get(f"/api/v1/locations/{lagos.public_id}/").json()["data"]
        assert body["id"] == str(lagos.public_id)
        assert body["id"] != str(lagos.pk)
        assert body["type"] == "STATE"
        assert body["level"] == 1

    def test_the_detail_embeds_the_children(self) -> None:
        lagos = _lagos()
        child = Location.objects.create(
            country=lagos.country,
            location_type=_tier("LGA"),
            parent=lagos,
            name="Ikeja",
            slug="ikeja-detail",
        )
        body = APIClient().get(f"/api/v1/locations/{lagos.public_id}/").json()["data"]
        assert [row["id"] for row in body["children"]] == [str(child.public_id)]

    def test_an_inactive_location_is_a_not_found(self) -> None:
        place = LocationFactory(country=_nigeria(), location_type=_tier("STATE"), is_active=False)
        response = APIClient().get(f"/api/v1/locations/{place.public_id}/")
        assert response.status_code == 404

    def test_an_unknown_location_is_a_not_found(self) -> None:
        response = APIClient().get(f"/api/v1/locations/{uuid.uuid4()}/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestAnonymousReadThrottle:
    def test_a_limited_caller_gets_429_and_retry_after(self, monkeypatch) -> None:
        def refuse(scope: str, identifier: str) -> Decision:
            return Decision(
                scope=scope,
                allowed=False,
                limit=100,
                remaining=0,
                retry_after=37,
            )

        monkeypatch.setattr("config.api.throttling.ratelimit.consume", refuse)
        response = APIClient().get("/api/v1/locations/countries/")
        assert response.status_code == 429
        assert response["Retry-After"] == "37"
        assert response.json()["error"]["code"] == "rate_limited"

    def test_an_authenticated_member_is_exempt(self, monkeypatch) -> None:
        """A shared limit in front of every member behind one NAT would be an
        outage, not a control; their reads are attributable already."""
        monkeypatch.setattr(
            "config.api.throttling.ratelimit.consume",
            lambda *args, **kwargs: pytest.fail("the limiter should not be consulted"),
        )
        client = APIClient()
        client.force_authenticate(UserFactory())
        assert client.get("/api/v1/locations/countries/").status_code == 200
