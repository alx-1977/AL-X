"""xAI structured-output transport behind the provider-neutral model port."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from alx.contracts import ModelCompletion, ModelRequest
from alx.providers.errors import ProviderError


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class XAIReasoningModel:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: int,
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def complete(self, request: ModelRequest) -> ModelCompletion:
        payload = {
            "model": self._model,
            "messages": [
                {"role": item.role.value, "content": item.content}
                for item in request.messages
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema_name,
                    "strict": True,
                    "schema": _json_value(request.output_schema),
                },
            },
        }
        try:
            response = self._client.post(
                f"{self._base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            content = body["choices"][0]["message"]["content"]
            output = json.loads(content)
            if not isinstance(output, dict):
                raise ValueError("structured output is not an object")
            model = body.get("model") or self._model
            usage = body.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            return ModelCompletion("xai", model, output, usage)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError("xai", type(error).__name__) from error
