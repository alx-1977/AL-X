"""OpenAI Responses transport behind the provider-neutral model port."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

import httpx

from alx.contracts import ModelCompletion, ModelRequest
from alx.providers.errors import ProviderError


LOGGER = logging.getLogger(__name__)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class OpenAIReasoningModel:
    """Translate neutral AL/X model requests to the OpenAI Responses API."""

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
        reasoning_effort: str = "medium",
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if service_tier not in ("default", "priority"):
            raise ValueError("service_tier must be default or priority")
        if reasoning_effort not in ("none", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("unsupported OpenAI reasoning effort")
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._streaming = streaming
        self._service_tier = service_tier
        self._reasoning_effort = reasoning_effort
        self._telemetry_sink = telemetry_sink

    def complete(self, request: ModelRequest) -> ModelCompletion:
        started_at = monotonic()
        LOGGER.info("Reasoning provider request started")
        payload: dict[str, Any] = {
            "model": self._model,
            "input": [
                {"role": item.role.value, "content": item.content}
                for item in request.messages
            ],
            "reasoning": {"effort": self._reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.output_schema_name,
                    "strict": True,
                    "schema": _json_value(request.output_schema),
                }
            },
        }
        if self._service_tier != "default":
            payload["service_tier"] = self._service_tier
        if request.affinity_key is not None:
            payload["prompt_cache_key"] = request.affinity_key
        if self._streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_obfuscation": False}

        try:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            if self._streaming:
                content, model, usage, service_tier, timings = self._stream(
                    headers, payload, started_at
                )
            else:
                response = self._client.post(
                    f"{self._base_url}/v1/responses",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                content = self._response_text(body)
                model = body.get("model") or self._model
                usage = body.get("usage") or {}
                service_tier = body.get("service_tier") or self._service_tier
                timings = {}
            output = json.loads(content)
            if not isinstance(output, dict):
                raise ValueError("structured output is not an object")
            if not isinstance(usage, dict):
                usage = {}
            completion = ModelCompletion("openai", model, output, usage)
            duration = monotonic() - started_at
            self._emit_telemetry(
                request.affinity_key,
                self._completion_telemetry(
                    model, service_tier, usage, duration, timings
                ),
            )
            LOGGER.info("Reasoning provider request completed in %.3f seconds", duration)
            return completion
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            duration = monotonic() - started_at
            self._emit_telemetry(
                request.affinity_key,
                {
                    "code": "reasoning.failed",
                    "provider": "openai",
                    "model": self._model,
                    "duration_ms": round(duration * 1000),
                    "error_type": type(error).__name__,
                },
            )
            LOGGER.info(
                "Reasoning provider request failed after %.3f seconds: %s",
                duration,
                type(error).__name__,
            )
            raise ProviderError("openai", type(error).__name__) from error

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
        completed = False
        with self._client.stream(
            "POST",
            f"{self._base_url}/v1/responses",
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
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        if first_content_at is None:
                            first_content_at = now
                        parts.append(delta)
                elif event_type == "response.completed":
                    result = event.get("response")
                    if not isinstance(result, dict):
                        raise ValueError("completed response event is malformed")
                    completed = True
                    model = result.get("model") or model
                    service_tier = result.get("service_tier") or service_tier
                    result_usage = result.get("usage")
                    if isinstance(result_usage, dict):
                        usage = result_usage
                    if not parts:
                        fallback = self._response_text(result)
                        if fallback:
                            if first_content_at is None:
                                first_content_at = now
                            parts.append(fallback)
                elif event_type in ("response.failed", "response.incomplete"):
                    raise ValueError(str(event_type))
        if not completed:
            raise ValueError("response stream ended before completion")
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
    def _response_text(body: Mapping[str, Any]) -> str:
        output = body.get("output")
        if not isinstance(output, list):
            raise ValueError("response output is missing")
        parts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "refusal":
                    raise ValueError("model refused structured response")
                text = part.get("text")
                if part.get("type") == "output_text" and isinstance(text, str):
                    parts.append(text)
        if not parts:
            raise ValueError("response contains no output text")
        return "".join(parts)

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
        return {
            "code": "reasoning.completed",
            "provider": "openai",
            "model": model,
            "service_tier": service_tier,
            "reasoning_effort": self._reasoning_effort,
            "duration_ms": round(duration * 1000),
            "first_event_ms": round(timings.get("first_event_seconds", 0.0) * 1000),
            "first_content_ms": round(
                timings.get("first_content_seconds", 0.0) * 1000
            ),
            "answer_generation_ms": round(
                timings.get("answer_generation_seconds", 0.0) * 1000
            ),
            "input_tokens": self._nested_integer(usage, "input_tokens"),
            "cached_tokens": self._nested_integer(
                usage, "input_tokens_details", "cached_tokens"
            ),
            "reasoning_tokens": self._nested_integer(
                usage, "output_tokens_details", "reasoning_tokens"
            ),
            "output_tokens": self._nested_integer(usage, "output_tokens"),
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
