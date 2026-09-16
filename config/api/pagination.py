"""Cursor pagination and the response envelope — 04 §1.4, §1.7.

Cursor-based rather than offset-based, because offset pagination degrades
badly on exactly the tables this API reads most — contributions, ledger
entries, notifications — which grow without bound and are almost always read
from the recent end (04 §1.4).

    {"data": [...], "pagination": {"next_cursor", "has_more", "limit"}}

Cursors are opaque and constructed only here. A client that builds its own is
relying on an encoding this module is free to change.
"""

from __future__ import annotations

from typing import Any

from rest_framework.pagination import CursorPagination as DRFCursorPagination
from rest_framework.response import Response

__all__ = ["CursorPagination", "data_response", "envelope"]

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def envelope(data: Any, *, pagination: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the response body of 04 §1.7."""
    body: dict[str, Any] = {"data": data}
    if pagination is not None:
        body["pagination"] = pagination
    return body


class CursorPagination(DRFCursorPagination):
    """Pagination with 04 §1.4's parameter names and metadata shape.

    **Every view using this must set ``ordering``.** DRF's cursor pagination
    requires a deterministic order — without one, a page boundary is not a
    position and rows repeat or vanish across pages. There is no default here
    on purpose: a default would be wrong for most models and silently so.
    """

    page_size = DEFAULT_PAGE_SIZE
    page_size_query_param = "limit"
    max_page_size = MAX_PAGE_SIZE

    # `get_page_size` is not overridden: DRF's own implementation already
    # rejects a negative, non-integer, or over-maximum `limit`, and restating
    # it here would mean maintaining a copy of someone else's validation that
    # can drift from it. The bound it enforces is asserted by an API test.

    def get_next_cursor(self) -> str | None:
        """The cursor for the next page, or None at the end.

        DRF exposes the next page only as a full URL. The contract in 04 §1.4
        returns the opaque cursor itself, so it is re-encoded here rather than
        having a client parse a URL to find it.
        """
        if not self.has_next or self.cursor is None:
            return None
        return self.encode_cursor(self.cursor)  # type: ignore[no-any-return]

    def get_paginated_response(self, data: Any) -> Response:
        return Response(
            envelope(
                data,
                pagination={
                    "next_cursor": self.get_next_cursor(),
                    "has_more": self.has_next,
                    "limit": self.page_size,
                },
            )
        )

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Describe the envelope to OpenAPI (04 §1.4).

        Overridden so the generated schema matches what clients actually
        receive, rather than DRF's `{count, next, previous, results}` default —
        an OpenAPI document that disagrees with the API is worse than none.
        """
        return {
            "type": "object",
            "required": ["data", "pagination"],
            "properties": {
                "data": schema,
                "pagination": {
                    "type": "object",
                    "required": ["next_cursor", "has_more", "limit"],
                    "properties": {
                        "next_cursor": {"type": "string", "nullable": True},
                        "has_more": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                },
            },
        }

    def get_schema_operation_parameters(self, view: Any) -> list[dict[str, Any]]:
        parameters = super().get_schema_operation_parameters(view)
        for parameter in parameters:
            if parameter["name"] == self.page_size_query_param:
                parameter["schema"]["maximum"] = self.max_page_size
                parameter["description"] = (
                    f"Number of results per page (default {self.page_size}, "
                    f"maximum {self.max_page_size})."
                )
        return parameters


def data_response(
    data: Any, *, status: int = 200, headers: dict[str, str] | None = None
) -> Response:
    """A single-resource response in the envelope of 04 §1.7.

    Deliberately a function rather than a mixin or a custom renderer. A mixin
    that rewrote every response would also rewrite error bodies, and this
    project has exactly one place that builds an error body
    (``config.api.exceptions``).
    """
    return Response(envelope(data), status=status, headers=headers)
