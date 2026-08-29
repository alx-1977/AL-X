"""Select configured adapters without leaking providers into AL/X Core."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.config import ConfigurationError, RuntimeSettings
from alx.contracts import ReasoningModel, SpeechSynthesizer, SpeechTranscriber
from alx.providers import (
    CartesiaTranscriber,
    ElevenLabsSynthesizer,
    OpenAIReasoningModel,
    XAIReasoningModel,
)


@dataclass(frozen=True, slots=True)
class RuntimeProviders:
    reasoning: ReasoningModel
    speech_to_text: SpeechTranscriber
    text_to_speech: SpeechSynthesizer


def build_runtime_providers(
    settings: RuntimeSettings,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> RuntimeProviders:
    if settings.reasoning.provider == "openai":
        reasoning = OpenAIReasoningModel(
            settings.reasoning.model,
            settings.reasoning.api_key,
            settings.reasoning.base_url,
            settings.reasoning.timeout_seconds,
            streaming=settings.reasoning.streaming,
            service_tier=settings.reasoning.service_tier,
            reasoning_effort=settings.reasoning.effort,
            telemetry_sink=telemetry_sink,
        )
    elif settings.reasoning.provider == "xai":
        reasoning = XAIReasoningModel(
            settings.reasoning.model,
            settings.reasoning.api_key,
            settings.reasoning.base_url,
            settings.reasoning.timeout_seconds,
            streaming=settings.reasoning.streaming,
            service_tier=settings.reasoning.service_tier,
            telemetry_sink=telemetry_sink,
        )
    else:
        raise ConfigurationError(
            f"reasoning provider adapter is not installed: {settings.reasoning.provider}"
        )
    if settings.speech_to_text.provider != "cartesia":
        raise ConfigurationError(
            f"speech-to-text provider adapter is not installed: {settings.speech_to_text.provider}"
        )
    if settings.text_to_speech.provider != "elevenlabs":
        raise ConfigurationError(
            f"text-to-speech provider adapter is not installed: {settings.text_to_speech.provider}"
        )
    return RuntimeProviders(
        reasoning=reasoning,
        speech_to_text=CartesiaTranscriber(
            settings.speech_to_text.model,
            settings.speech_to_text.api_key,
            settings.speech_to_text.base_url,
            settings.speech_to_text.api_version,
            settings.speech_to_text.encoding,
            settings.speech_to_text.sample_rate_hz,
            settings.speech_to_text.turn_start_threshold,
            settings.speech_to_text.turn_eager_end_threshold,
            settings.speech_to_text.turn_end_threshold,
            settings.speech_to_text.turn_end_timeout_ms,
        ),
        text_to_speech=ElevenLabsSynthesizer(
            settings.text_to_speech.model,
            settings.text_to_speech.api_key,
            settings.text_to_speech.voice_id,
            settings.text_to_speech.base_url,
            settings.text_to_speech.output_format,
            settings.text_to_speech.timeout_seconds,
            telemetry_sink=telemetry_sink,
        ),
    )
