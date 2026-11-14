from __future__ import annotations

import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from integrations import openai
from integrations.openai import AIProviderError, NoRedirect, OpenAIClient
from scripts.ai_review import fetch_diff, filter_diff, main

pytestmark = pytest.mark.unit
TEST_API_KEY = "synthetic-test-key"  # pragma: allowlist secret


def provider(monkeypatch, payload):
    response = io.BytesIO(json.dumps(payload).encode())
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(openai, "build_opener", lambda *args: opener)
    return opener


def completed(text="Use your authenticator app."):
    return {
        "id": "resp_test",
        "status": "completed",
        "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
    }


def test_request_limits_and_provider_storage(monkeypatch):
    opener = provider(monkeypatch, completed())
    result = OpenAIClient(api_key=TEST_API_KEY, timeout=32, max_output_tokens=800).generate(
        instructions="Trusted instructions", input_text="Question"
    )
    request = opener.open.call_args.args[0]
    body = json.loads(request.data)
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert body["store"] is False
    assert body["max_output_tokens"] == 800
    assert body["instructions"] == "Trusted instructions"
    assert body["input"] == "Question"
    assert opener.open.call_args.kwargs["timeout"] == 32
    assert result.response_id == "resp_test"
    assert result.text == "Use your authenticator app."


@pytest.mark.parametrize(
    "status,retryable", [(401, False), (403, False), (429, True), (500, True), (302, False)]
)
def test_http_errors_do_not_expose_provider_bodies(monkeypatch, status, retryable):
    opener = Mock()
    opener.open.side_effect = HTTPError(
        "https://api.openai.com", status, "sensitive provider body", {}, io.BytesIO(b"sensitive")
    )
    monkeypatch.setattr(openai, "build_opener", lambda *args: opener)
    with pytest.raises(AIProviderError) as captured:
        OpenAIClient(api_key=TEST_API_KEY).generate(instructions="Rules", input_text="Question")
    assert captured.value.retryable is retryable
    assert str(captured.value) == "provider_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"status": "incomplete"},
        {"status": "completed", "output": []},
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}],
        },
        completed("x" * 16001),
    ],
)
def test_malformed_incomplete_and_refused_outputs_fail(monkeypatch, payload):
    provider(monkeypatch, payload)
    with pytest.raises(AIProviderError):
        OpenAIClient(api_key=TEST_API_KEY).generate(instructions="Rules", input_text="Question")


def test_network_timeout_is_retryable(monkeypatch):
    opener = Mock()
    opener.open.side_effect = TimeoutError("sensitive network detail")
    monkeypatch.setattr(openai, "build_opener", lambda *args: opener)
    with pytest.raises(AIProviderError) as captured:
        OpenAIClient(api_key=TEST_API_KEY).generate(instructions="Rules", input_text="Question")
    assert captured.value.retryable


def test_missing_key_refuses_without_network(monkeypatch):
    network = Mock()
    monkeypatch.setattr(openai, "build_opener", network)
    with pytest.raises(AIProviderError, match="ai_unavailable"):
        OpenAIClient(api_key="").generate(instructions="Rules", input_text="Question")
    network.assert_not_called()


def test_redirects_are_never_followed():
    assert (
        NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.test")
        is None
    )


def test_oversized_response_is_bounded(monkeypatch):
    opener = Mock()
    opener.open.return_value = io.BytesIO(b"x" * (openai.MAX_RESPONSE_BYTES + 1))
    monkeypatch.setattr(openai, "build_opener", lambda *args: opener)
    with pytest.raises(AIProviderError, match="invalid_provider_response"):
        OpenAIClient(api_key=TEST_API_KEY).generate(instructions="Rules", input_text="Question")


def test_review_filters_environment_key_binary_and_generated_files():
    def section(path, body="+changed"):
        return f"diff --git a/{path} b/{path}\n{body}\n"

    diff = "".join(
        section(path)
        for path in [
            ".env",
            ".env.example",
            "keys/service.pem",
            "requirements/lock/prod.txt",
            "docs/openapi.yaml",
            "apps/groups/services.py",
        ]
    )
    diff += section("photo.png", "Binary files a/photo.png and b/photo.png differ")
    assert filter_diff(diff) == section("apps/groups/services.py")


@pytest.mark.parametrize(
    "repository,base,head",
    [
        ("../../evil", "a" * 40, "b" * 40),
        ("owner/repo", "main", "b" * 40),
        ("owner/repo", "a" * 40, "$(command)"),
    ],
)
def test_review_validates_revisions_before_network(repository, base, head):
    with pytest.raises(ValueError):
        fetch_diff(
            repository=repository, base_sha=base, head_sha=head, token="synthetic-test-token"
        )


def test_review_script_saves_report_without_django(monkeypatch, tmp_path):
    from scripts import ai_review

    monkeypatch.setenv("OPENAI_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("AI_REVIEW_BASE_SHA", "a" * 40)
    monkeypatch.setenv("AI_REVIEW_HEAD_SHA", "b" * 40)
    monkeypatch.setattr(
        ai_review, "fetch_diff", lambda **kwargs: "diff --git a/app.py b/app.py\n+change\n"
    )
    monkeypatch.setattr(
        OpenAIClient,
        "generate",
        lambda *args, **kwargs: openai.AIResponse("No supported findings.", "resp_test"),
    )
    output = tmp_path / "report.md"
    monkeypatch.setattr("sys.argv", ["ai_review.py", "--output", str(output)])
    assert main() == 0
    assert "No supported findings." in output.read_text()
    assert "b" * 40 in output.read_text()
