"""Small, bounded Responses API client shared by the app and CI tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_MODEL = "gpt-4.1-mini"
MAX_RESPONSE_BYTES = 1_000_000


class AIProviderError(Exception):
    """Stable failure information without provider bodies or credentials."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        # Never forward the Authorization header to a redirect destination.
        return None


@dataclass(frozen=True)
class AIResponse:
    text: str
    response_id: str


@dataclass(frozen=True)
class OpenAIClient:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    timeout: int = 45
    max_output_tokens: int = 1200

    def generate(self, *, instructions: str, input_text: str) -> AIResponse:
        if not self.api_key.strip():
            raise AIProviderError("ai_unavailable")
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        request = Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            exc.close()
            raise AIProviderError(
                "provider_unavailable",
                retryable=exc.code in (408, 429) or exc.code >= 500,
            ) from None
        except OSError:
            raise AIProviderError("provider_unavailable", retryable=True) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AIProviderError("invalid_provider_response")
        try:
            result = json.loads(raw)
            if result.get("status") != "completed":
                raise AIProviderError("incomplete_provider_response")
            parts = [
                content["text"]
                for item in result["output"]
                if item.get("type") == "message"
                for content in item.get("content", [])
                if content.get("type") == "output_text"
            ]
            output = "\n".join(parts).strip()
            response_id = result["id"]
            if not output or len(output) > 16_000 or not isinstance(response_id, str):
                raise AIProviderError("invalid_provider_response")
            return AIResponse(output, response_id[:255])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AIProviderError("invalid_provider_response") from None
