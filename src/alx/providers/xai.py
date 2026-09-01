"""xAI structured-output transport behind the provider-neutral model port."""

from __future__ import annotations

import json
import logging
from time import monotonic
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from alx.contracts import ModelCompletion, ModelRequest
from alx.providers.errors import (
    ProviderError,
    raise_provider_failure,
    status_code_of,
)


LOGGER = logging.getLogger(__name__)


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
        *,
        streaming: bool = True,
        service_tier: str = "default",
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if service_tier not in ("default", "priority"):
            raise ValueError("service_tier must be default or priority")
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._streaming = streaming
        self._service_tier = service_tier
        self._telemetry_sink = telemetry_sink

    def complete(self, request: ModelRequest) -> ModelCompletion:
        started_at = monotonic()
        LOGGER.info("Reasoning provider request started")
        payload = {
            "model": self._model,
            "service_tier": self._service_tier,
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
        if self._streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        try:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            if request.affinity_key is not None:
                headers["x-grok-conv-id"] = request.affinity_key
            if self._streaming:
                content, model, usage, service_tier, timings = self._stream(
                    headers, payload, started_at
                )
            else:
                response = self._client.post(
                    f"{self._base_url}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                content = body["choices"][0]["message"]["content"]
                model = body.get("model") or self._model
                usage = body.get("usage") or {}
                service_tier = body.get("service_tier") or self._service_tier
                timings = {}
            output = json.loads(content)
            if not isinstance(output, dict):
                raise ValueError("structured output is not an object")
            if not isinstance(usage, dict):
                usage = {}
            completion = ModelCompletion("xai", model, output, usage)
            duration = monotonic() - started_at
            self._emit_telemetry(
                request.affinity_key,
                self._completion_telemetry(
                    model, service_tier, usage, duration, timings
                ),
            )
            LOGGER.info(
                "Reasoning provider request completed in %.3f seconds",
                duration,
            )
            return completion
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            error_code = type(error).__name__
            # The status is a number, not payload: 403 says the credit ran out,
            # where the exception name alone says only that something failed.
            status = status_code_of(error)
            if status is not None:
                error_code = f"{error_code}:{status}"
            duration = monotonic() - started_at
            self._emit_telemetry(
                request.affinity_key,
                {
                    "code": "reasoning.failed",
                    "provider": "xai",
                    "model": self._model,
                    "duration_ms": round(duration * 1000),
                    "error_type": error_code,
                    "status_code": status,
                },
            )
            if status in (402, 403):
                # The one failure Friedl must be able to see without a browser:
                # a spent account otherwise looks like an unexplained hang.
                LOGGER.error(
                    "Reasoning provider rejected the request with %s after "
                    "%.3f seconds: the API key is out of credit, over its "
                    "spending limit, or not permitted for this model",
                    status,
                    duration,
                )
            else:
                LOGGER.info(
                    "Reasoning provider request failed after %.3f seconds: %s",
                    duration,
                    error_code,
                )
        raise_provider_failure("xai", error_code)

    def _stream(
        self,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        started_at: float,
    ) -> tuple[str, str, dict[str, Any], str, dict[str, float]]:
        parts: list[str] = []
        model = self._model
        service_tier = self._service_tier
        usage: dict[str, Any] = {}
        first_event_at: float | None = None
        first_content_at: float | None = None
        with self._client.stream(
            "POST",
            f"{self._base_url}/v1/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                event = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                now = monotonic()
                if first_event_at is None:
                    first_event_at = now
                model = event.get("model") or model
                service_tier = event.get("service_tier") or service_tier
                event_usage = event.get("usage")
                if isinstance(event_usage, dict) and event_usage:
                    usage = event_usage
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                delta = choices[0].get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    if first_content_at is None:
                        first_content_at = now
                    parts.append(content)
        finished_at = monotonic()
        timings = {
            "first_event_seconds": (
                finished_at if first_event_at is None else first_event_at
            )
            - started_at,
            "first_content_seconds": (
                finished_at if first_content_at is None else first_content_at
            )
            - started_at,
            "answer_generation_seconds": (
                0.0 if first_content_at is None else finished_at - first_content_at
            ),
        }
        return "".join(parts), model, usage, service_tier, timings

    @staticmethod
    def _nested_integer(data: Mapping[str, Any], *path: str) -> int:
        current: Any = data
        for key in path:
            if not isinstance(current, Mapping):
                return 0
            current = current.get(key)
        return current if isinstance(current, int) and not isinstance(current, bool) else 0

    def _completion_telemetry(
        self,
        model: str,
        service_tier: str,
        usage: Mapping[str, Any],
        duration: float,
        timings: Mapping[str, float],
    ) -> dict[str, Any]:
        output_tokens = self._nested_integer(usage, "completion_tokens")
        if not output_tokens:
            output_tokens = self._nested_integer(usage, "output_tokens")
        reasoning_tokens = self._nested_integer(
            usage, "completion_tokens_details", "reasoning_tokens"
        ) or self._nested_integer(usage, "output_tokens_details", "reasoning_tokens")
        return {
            "code": "reasoning.completed",
            "provider": "xai",
            "model": model,
            "service_tier": service_tier,
            "duration_ms": round(duration * 1000),
            "first_event_ms": round(timings.get("first_event_seconds", 0.0) * 1000),
            "first_content_ms": round(timings.get("first_content_seconds", 0.0) * 1000),
            "answer_generation_ms": round(
                timings.get("answer_generation_seconds", 0.0) * 1000
            ),
            "input_tokens": self._nested_integer(usage, "prompt_tokens")
            or self._nested_integer(usage, "input_tokens"),
            "cached_tokens": self._nested_integer(
                usage, "prompt_tokens_details", "cached_tokens"
            ) or self._nested_integer(usage, "input_tokens_details", "cached_tokens"),
            "reasoning_tokens": reasoning_tokens,
            "output_tokens": output_tokens,
            "total_tokens": self._nested_integer(usage, "total_tokens"),
        }

    def _emit_telemetry(
        self,
        affinity_key: str | None,
        values: Mapping[str, Any],
    ) -> None:
        if affinity_key is None or self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(affinity_key, values)
        except Exception:
            LOGGER.info("Reasoning telemetry sink failed")
