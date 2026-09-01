"""Select configured adapters without leaking providers into AL/X Core."""

from __future__ import annotations

import logging

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.config import ConfigurationError, RuntimeSettings
from alx.contracts import (
    Cognition,
    ReasoningModel,
    SpeechSynthesizer,
    SpeechTranscriber,
)
from alx.providers import (
    CartesiaTranscriber,
    ElevenLabsSynthesizer,
    OpenAIReasoningModel,
    XAIReasoningModel,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeProviders:
    reasoning: ReasoningModel
    # Configured independently of the Core. None when the configured
    # specialist provider has no adapter, which disables specialist work
    # rather than sending it to the Core.
    specialist: ReasoningModel | None
    # One model per cognition tier. A tier absent here has no adapter for its
    # configured provider, so questions at that tier are refused rather than
    # answered by a different tier: silently buying more or less thinking than
    # AL/X asked for would make the tier meaningless.
    research_tiers: Mapping[Cognition, ReasoningModel]
    # What each tier resolves to, so a spending ledger records the model that
    # was actually charged.
    research_identity: Mapping[Cognition, tuple[str, str]]
    speech_to_text: SpeechTranscriber
    text_to_speech: SpeechSynthesizer



def _build_reasoning_model(
    settings, telemetry_sink
) -> ReasoningModel | None:
    """Build a model for the specialist, or None when it cannot be built.

    Returning None disables specialist work. It must never be answered by the
    Core instead: that is the expensive path this exists to avoid, and a silent
    fallback would hide the misconfiguration.
    """
    if settings.provider == "openai":
        return OpenAIReasoningModel(
            settings.model,
            settings.api_key,
            settings.base_url,
            settings.timeout_seconds,
            streaming=settings.streaming,
            service_tier=settings.service_tier,
            reasoning_effort=settings.effort,
            telemetry_sink=telemetry_sink,
        )
    if settings.provider in ("xai", "kimi"):
        # One OpenAI-style /v1/chat/completions client serves both. The
        # blueprint keeps the model a configuration choice, so a second vendor
        # speaking the same protocol needs a base URL and a key, not a second
        # adapter or a second conversation path.
        #
        # Neither transport takes a reasoning-effort parameter, so the
        # configured effort cannot be honoured there. Saying so is better than
        # leaving a setting that looks active and is not.
        if settings.effort not in ("", "medium"):
            LOGGER.info(
                "Specialist reasoning effort %r is not supported by %s and is ignored",
                settings.effort,
                settings.provider,
            )
        return XAIReasoningModel(
            settings.model,
            settings.api_key,
            settings.base_url,
            settings.timeout_seconds,
            streaming=settings.streaming,
            service_tier=settings.service_tier,
            telemetry_sink=telemetry_sink,
        )
    LOGGER.info("Specialist adapter is not installed: %s", settings.provider)
    return None


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
    elif settings.reasoning.provider in ("xai", "kimi"):
        # Same OpenAI-style transport; the vendor is a base URL and a key.
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
    specialist = _build_reasoning_model(settings.specialist, telemetry_sink)
    tier_settings = {
        Cognition.SURVEY: settings.research.survey,
        Cognition.COMPARE: settings.research.compare,
        Cognition.JUDGE: settings.research.judge,
    }
    research_tiers: dict[Cognition, ReasoningModel] = {}
    research_identity: dict[Cognition, tuple[str, str]] = {}
    for tier, tier_setting in tier_settings.items():
        model = _build_reasoning_model(tier_setting, telemetry_sink)
        if model is None:
            LOGGER.info(
                "Research tier %s has no adapter for provider %s and is disabled",
                tier.value,
                tier_setting.provider,
            )
            continue
        research_tiers[tier] = model
        research_identity[tier] = (tier_setting.provider, tier_setting.model)

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
        specialist=specialist,
        research_tiers=research_tiers,
        research_identity=research_identity,
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
            settings.text_to_speech.pronunciation_dictionary_id,
            settings.text_to_speech.pronunciation_dictionary_version_id,
            telemetry_sink=telemetry_sink,
            speed=settings.text_to_speech.speed,
            stability=settings.text_to_speech.stability,
            similarity_boost=settings.text_to_speech.similarity_boost,
            speaker_boost=settings.text_to_speech.speaker_boost,
        ),
    )
