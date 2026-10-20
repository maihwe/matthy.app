"""Cursor pagination and the response envelope — 04 §1.4, §1.7.

No database and no HTTP: the paginator is exercised directly, with the state
DRF would have set after a page was fetched. That keeps these tests about the
envelope's shape, which is the contract a client depends on, rather than about
DRF's queryset slicing, which has its own tests upstream.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from config.api.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorPagination,
    data_response,
    envelope,
)

pytestmark = pytest.mark.unit


def test_envelope_without_pagination_carries_only_data() -> None:
    assert envelope([1, 2]) == {"data": [1, 2]}


def test_envelope_with_pagination_adds_the_metadata_block() -> None:
    body = envelope([], pagination={"next_cursor": None, "has_more": False, "limit": 10})
    assert body["pagination"]["limit"] == 10


def test_data_response_wraps_and_forwards_status_and_headers() -> None:
    response = data_response({"id": 1}, status=201, headers={"Location": "/x"})
    assert response.status_code == 201
    assert response.data == {"data": {"id": 1}}
    assert response["Location"] == "/x"


def test_the_next_cursor_is_none_at_the_end_of_the_list() -> None:
    paginator = CursorPagination()
    paginator.has_next = False
    assert paginator.get_next_cursor() is None


def test_the_next_cursor_is_none_when_drf_offers_no_next_link(monkeypatch) -> None:
    paginator = CursorPagination()
    paginator.has_next = True
    monkeypatch.setattr(paginator, "get_next_link", lambda: None)
    assert paginator.get_next_cursor() is None


def test_the_next_cursor_is_the_token_from_the_next_link(monkeypatch) -> None:
    """DRF computes the next page's cursor; only the URL around it is dropped.

    The token is not the request's cursor re-encoded: on the first page there
    is no request cursor at all, and later pages carry a different offset. So
    the unit test substitutes the link DRF would have built and asserts that
    exactly its ``cursor`` parameter survives — no scheme, path or query.
    """
    paginator = CursorPagination()
    token = base64.urlsafe_b64encode(b"o=5").decode().rstrip("=")
    monkeypatch.setattr(
        paginator,
        "get_next_link",
        lambda: f"http://testserver/items/?cursor={token}&limit=25",
    )
    encoded = paginator.get_next_cursor()
    assert encoded == token
    assert "/" not in encoded and "?" not in encoded
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)) == b"o=5"


def test_the_paginated_response_uses_the_contract_shape(monkeypatch) -> None:
    paginator = CursorPagination()
    paginator.has_next = True
    paginator.page_size = 25
    token = base64.urlsafe_b64encode(b"o=5").decode().rstrip("=")
    monkeypatch.setattr(
        paginator,
        "get_next_link",
        lambda: f"http://testserver/items/?cursor={token}&limit=25",
    )
    response = paginator.get_paginated_response([{"id": 1}])
    assert response.data == {
        "data": [{"id": 1}],
        "pagination": {
            "next_cursor": token,
            "has_more": True,
            "limit": 25,
        },
    }


def test_the_page_size_limits_are_the_documented_ones() -> None:
    assert DEFAULT_PAGE_SIZE == 50
    assert MAX_PAGE_SIZE == 200


def test_the_schema_describes_the_envelope_not_drfs_default() -> None:
    paginator = CursorPagination()
    schema = paginator.get_paginated_response_schema({"type": "array"})
    assert schema["required"] == ["data", "pagination"]
    assert schema["properties"]["pagination"]["required"] == [
        "next_cursor",
        "has_more",
        "limit",
    ]


def test_the_schema_bounds_the_limit_parameter() -> None:
    paginator = CursorPagination()
    paginator.ordering = "-id"
    parameters: list[dict[str, Any]] = paginator.get_schema_operation_parameters(object())

    limit_parameter = next(p for p in parameters if p["name"] == "limit")
    assert limit_parameter["schema"]["maximum"] == MAX_PAGE_SIZE
    assert str(MAX_PAGE_SIZE) in limit_parameter["description"]
