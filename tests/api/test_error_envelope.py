"""The error envelope of 04 §1.2, asserted at the handler.

Every endpoint in the project returns errors through one function. These tests
pin its contract, because the alternative — each view raising and formatting
its own errors — is how an API acquires four dialects of "not allowed" and a
mobile client acquires four special cases.

No database is needed: DRF's handler touches the connection registry but does
not open a connection unless a request is already inside an atomic block.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf

from config.api.exceptions import (
    Conflict,
    IdempotencyConflict,
    SeparationOfDuties,
    Unprocessable,
    envelope_exception_handler,
)
from config.logging import set_correlation_id

pytestmark = pytest.mark.unit


def handle(exc: Exception) -> tuple[int, dict[str, Any]]:
    """Run the handler and return ``(status, parsed_body)``.

    Reparses through JSON rather than reading ``response.data``, so the test
    sees what the client sees — a value that only serialises under duress would
    otherwise pass here and fail in production.
    """
    response = envelope_exception_handler(exc, {})
    body = json.loads(json.dumps(response.data, default=str))
    return response.status_code, body


def error_of(exc: Exception) -> dict[str, Any]:
    return handle(exc)[1]["error"]


class TestEnvelopeShape:
    """04 §1.2 — the four keys are always present, and always these."""

    def test_error_object_has_exactly_the_documented_keys(self) -> None:
        error = error_of(drf.NotFound())
        assert set(error) == {"code", "message", "details", "request_id"}

    def test_details_is_a_list_even_when_empty(self) -> None:
        """Not ``null``, and not absent.

        Clients index into ``details``; making its emptiness a different shape
        from its fullness means every client carries a null check.
        """
        error = error_of(drf.NotFound())
        assert error["details"] == []

    def test_request_id_defaults_when_no_request_is_in_flight(self) -> None:
        """A management-command or Celery failure has no HTTP request.

        The key is still present and still a string, so a log line that
        includes it never has to handle a missing field.
        """
        set_correlation_id("-")
        assert error_of(drf.NotFound())["request_id"] == "-"

    def test_request_id_carries_the_correlation_id(self) -> None:
        """04 §1.2 — the ID a client is shown is the ID its logs carry.

        Without this the ``request_id`` is decoration: an operator given one by
        a user could not find the request it came from.
        """
        set_correlation_id("req_deadbeefcafe0001")
        assert error_of(drf.NotFound())["request_id"] == "req_deadbeefcafe0001"


class TestValidationErrors:
    """400 — the case clients hit constantly, so the shape matters most."""

    def test_is_a_400_with_code_validation_error(self) -> None:
        """DRF's own code for this is ``"invalid"``.

        Taking ``default_code`` from the exception would ship ``"invalid"``
        while the specification promises ``"validation_error"``, and a client
        branching on the documented value would silently never match.
        """
        status, body = handle(drf.ValidationError("Nope."))
        assert status == 400
        assert body["error"]["code"] == "validation_error"

    def test_named_fields_appear_in_details(self) -> None:
        status, body = handle(
            drf.ValidationError({"phone_number": ["Enter a valid phone number."]})
        )
        assert status == 400
        assert body["error"]["details"] == [
            {"field": "phone_number", "issue": "Enter a valid phone number."}
        ]

    def test_several_issues_for_one_field_are_all_reported(self) -> None:
        """Reporting only the first is how a client ends up in a
        fix-submit-fix loop over a form it could have fixed in one pass."""
        _, body = handle(drf.ValidationError({"amount": ["Required.", "Must be positive."]}))
        assert [d["issue"] for d in body["error"]["details"]] == [
            "Required.",
            "Must be positive.",
        ]

    def test_nested_serializer_errors_are_dotted(self) -> None:
        """DRF nests one dict level per nested serializer.

        Flattening gives the client the field path it addressed in its own
        JSON, which is the only name it knows.
        """
        _, body = handle(
            drf.ValidationError({"bank_account": {"account_number": ["This field is required."]}})
        )
        assert body["error"]["details"] == [
            {"field": "bank_account.account_number", "issue": "This field is required."}
        ]

    def test_a_list_error_has_no_field(self) -> None:
        _, body = handle(drf.ValidationError(["Something went wrong."]))
        assert body["error"]["details"] == [
            {"field": "non_field_errors", "issue": "Something went wrong."}
        ]

    def test_the_message_stays_generic(self) -> None:
        """The per-field detail belongs in ``details``.

        Copying it into ``message`` produces a top-level string that is either
        truncated and useless or a nested dump of every field error.
        """
        error = error_of(drf.ValidationError({"phone_number": ["Enter a valid phone number."]}))
        assert error["message"] == "The request was malformed or contained invalid values."
        assert "phone_number" not in error["message"]


class TestProjectExceptions:
    def test_conflict_is_409(self) -> None:
        status, body = handle(Conflict())
        assert status == 409
        assert body["error"]["code"] == "conflict"

    def test_idempotency_conflict_has_its_own_code(self) -> None:
        """04 §1.5 — a key reused with a different body is a client bug.

        It shares 409 with ``Conflict`` but not its code, because the client's
        correct response differs: this one means the caller's own logic is
        wrong, not that the resource moved underneath it.
        """
        status, body = handle(IdempotencyConflict())
        assert status == 409
        assert body["error"]["code"] == "idempotency_conflict"

    def test_unprocessable_is_422(self) -> None:
        status, body = handle(Unprocessable())
        assert status == 422
        assert body["error"]["code"] == "unprocessable"

    def test_a_specific_detail_reaches_the_client(self) -> None:
        """A 409 that says only "conflict" makes the client guess which rule it
        broke."""
        _, body = handle(Conflict("A group with this name already exists."))
        assert body["error"]["message"] == "A group with this name already exists."

    def test_the_default_detail_is_used_when_none_is_given(self) -> None:
        _, body = handle(Conflict())
        assert (
            body["error"]["message"] == "The request conflicts with the resource's current state."
        )


class TestSeparationOfDuties:
    """05 §6, 44 rule 6 — the one 403 a client must not retry."""

    def test_is_a_403_with_its_own_code(self) -> None:
        status, body = handle(SeparationOfDuties())
        assert status == 403
        assert body["error"]["code"] == "separation_of_duties"

    def test_is_distinguishable_from_an_ordinary_403(self) -> None:
        """Both are 403. Only the code tells the client whether to stop.

        A generic 403 invites the client to prompt for a permission change,
        and no permission change will make self-approval succeed.
        """
        sod = error_of(SeparationOfDuties())
        ordinary = error_of(drf.PermissionDenied())
        assert sod["code"] != ordinary["code"]
        assert ordinary["code"] == "insufficient_permission"

    def test_names_the_requester_when_raised_with_a_detail(self) -> None:
        _, body = handle(SeparationOfDuties("A contribution cannot be approved by its collector."))
        assert body["error"]["message"] == "A contribution cannot be approved by its collector."

    def test_has_a_default_message_when_raised_bare(self) -> None:
        """``__str__`` on a bare Django ``PermissionDenied`` is the empty
        string, and an empty ``message`` renders as a blank error dialog."""
        _, body = handle(SeparationOfDuties())
        assert body["error"]["message"] == "This action cannot be performed by the requester."

    def test_it_survives_drf_replacing_the_exception_type(self) -> None:
        """DRF's handler converts Django's ``PermissionDenied`` into DRF's own.

        The code is chosen from the *original* exception for exactly this
        reason; reading it off the converted one would collapse every
        separation-of-duties refusal into ``insufficient_permission``.
        """
        assert error_of(SeparationOfDuties())["code"] == "separation_of_duties"
        assert error_of(PermissionDenied())["code"] == "insufficient_permission"


class TestStandardErrors:
    @pytest.mark.parametrize(
        ("exc", "expected_status", "expected_code"),
        [
            (drf.NotFound(), 404, "not_found"),
            (Http404(), 404, "not_found"),
            (drf.PermissionDenied(), 403, "insufficient_permission"),
            (drf.NotAuthenticated(), 401, "authentication_required"),
            (drf.AuthenticationFailed(), 401, "authentication_required"),
            (drf.ParseError(), 400, "validation_error"),
            (drf.UnsupportedMediaType("application/xml"), 415, "unsupported_media_type"),
        ],
    )
    def test_status_and_code(
        self, exc: Exception, expected_status: int, expected_code: str
    ) -> None:
        status, body = handle(exc)
        assert status == expected_status
        assert body["error"]["code"] == expected_code

    def test_http404_becomes_the_envelope(self) -> None:
        """A view raising Django's ``Http404`` must not bypass the envelope.

        Django's own 404 handler serves HTML; an API client parsing that gets a
        JSON decode error where it expected a documented error.
        """
        status, body = handle(Http404("Group not found."))
        assert status == 404
        assert body["error"]["code"] == "not_found"

    def test_not_found_does_not_reveal_whether_the_object_exists(self) -> None:
        """04 §1.2 — deliberately ambiguous.

        "Group not found" and "you may not see this group" must be the same
        response, or the error channel becomes a way to enumerate groups.
        """
        _, body = handle(Http404("Group 4f2c-... not found."))
        assert body["error"]["message"] == "Not found."
        assert "4f2c" not in json.dumps(body)

    def test_method_not_allowed_keeps_its_specific_message(self) -> None:
        """The one place a specific message is safe and useful: the caller
        already reached the endpoint."""
        status, body = handle(drf.MethodNotAllowed("POST"))
        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"
        assert "POST" in body["error"]["message"]

    def test_rate_limited_sets_retry_after(self) -> None:
        """04 §1.2 — a 429 without ``Retry-After`` leaves the client to guess,
        and a client that guesses is a client that retries in a tight loop."""
        response = envelope_exception_handler(drf.Throttled(wait=30), {})
        assert response.status_code == 429
        assert response.data["error"]["code"] == "rate_limited"
        assert response["Retry-After"] == "30"


class TestUnhandledExceptions:
    """10 §5.2 — nothing internal leaves the process."""

    def test_an_unrecognised_exception_becomes_a_500_envelope(self) -> None:
        status, body = handle(ValueError("internal detail"))
        assert status == 500
        assert body["error"]["code"] == "internal_error"
        assert set(body["error"]) == {"code", "message", "details", "request_id"}

    def test_the_message_leaks_nothing(self) -> None:
        """The exception text is logged, never returned.

        An unexpected exception is the one most likely to carry a SQL
        fragment, a file path or a connection string, and it is returned to
        whoever triggered it.
        """
        secret = "connection to server at 10.0.0.5:5432 failed"
        _, body = handle(RuntimeError(secret))
        rendered = json.dumps(body)
        assert secret not in rendered
        assert "10.0.0.5" not in rendered
        assert "RuntimeError" not in rendered

    def test_the_response_is_still_json_with_the_documented_shape(self) -> None:
        """An unhandled error is the one case where a client most needs the
        error to parse, and the one where a stack-trace HTML page is most
        likely."""
        _, body = handle(RuntimeError("boom"))
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["details"] == []
