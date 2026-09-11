"""Select configured adapters without leaking providers into AL/X Core."""

from __future__ import annotations

import logging

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.config import ConfigurationError, RuntimeSettings
from alx.config.settings import (
    CLAUDE_SUBSCRIPTION_PROVIDER,
    CODEX_SUBSCRIPTION_PROVIDER,
    GROK_SUBSCRIPTION_PROVIDER,
    NO_PROVIDER,
)
from alx.contracts import ReasoningModel, SpeechSynthesizer, SpeechTranscriber
from alx.providers import (
    CartesiaTranscriber,
    ClaudeSubscriptionReasoningModel,
    CodexSubscriptionReasoningModel,
    ElevenLabsSynthesizer,
    OpenAIReasoningModel,
    XAIReasoningModel,
)
from alx.providers.claude_subscription import subscription_cli_present
from alx.providers.codex_subscription import subscription_cli_present as codex_subscription_cli_present
from alx.contracts import CodingSession
from alx.providers.coding_session import GrokCodingSession
from alx.providers.grok_subscription import GrokSubscriptionReasoningModel
from alx.providers.gated_transcription import GatedTranscriber


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeProviders:
    reasoning: ReasoningModel
    # Configured independently of the Core. None when the configured
    # specialist provider has no adapter, which disables specialist work
    # rather than sending it to the Core.
    specialist: ReasoningModel | None
    # D-024a experiment: the Core that answers an autonomous turn. None when
    # unconfigured, which disables the experiment rather than sending an
    # autonomous turn to the conversational Core under another name.
    autonomous: ReasoningModel | None
    # D-028 coding jobs. Independent of the conversational Core. None when
    # unconfigured or unusable, which leaves the capability unregistered.
    coding: ReasoningModel | None
    # A separate advisory model for local CA review. It must not share the
    # coding planner instance, even when both use the same provider and model.
    coding_reviewer: ReasoningModel | None
    # The native coding-agent session that carries a plan out in the assigned
    # worktree. None when the configured coding provider has no session
    # adapter, which leaves the capability unregistered rather than letting a
    # job plan and then have nothing to execute it.
    coding_session: CodingSession | None
    speech_to_text: SpeechTranscriber
    # None when no speech transport is configured. Audio is then absent and
    # nothing else differs: the Core never learns whether anyone could hear.
    text_to_speech: SpeechSynthesizer | None



def _build_reasoning_model(
    settings, telemetry_sink
) -> ReasoningModel | None:
    """Build a model for the specialist, or None when it cannot be built.

    Returning None disables specialist work. It must never be answered by the
    Core instead: that is the expensive path this exists to avoid, and a silent
    fallback would hide the misconfiguration.
    """
    if settings.provider == NO_PROVIDER:
        # Deliberately absent, not misconfigured. Nothing is constructed, so
        # there is no client holding a credential and nothing that could be
        # invoked by accident. The work refuses instead.
        LOGGER.info("Specialist reasoning is disabled by configuration")
        return None
    if settings.provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        # Scoped to the conversational Core. Specialist and research cognition
        # are not authorised on the subscription path, so this is refused
        # rather than quietly built.
        raise ConfigurationError(
            f"{CLAUDE_SUBSCRIPTION_PROVIDER} is not available for specialist "
            "or research cognition"
        )
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


def _build_coding_session(
    settings: RuntimeSettings,
) -> "CodingSession | None":
    """The native session that executes a coding plan, or None.

    Only the Grok subscription path has a session adapter. Another configured
    coding provider can still plan, but nothing would carry the plan out, so
    the capability is left unregistered instead.
    """
    coding = settings.coding
    if not coding.enabled or not coding.is_usable:
        return None
    if coding.reasoning.provider != GROK_SUBSCRIPTION_PROVIDER:
        LOGGER.info(
            "Coding session adapter is not installed: %s",
            coding.reasoning.provider,
        )
        return None
    # Deliberately not `reasoning.timeout_seconds`: that bounds one planning
    # call, and a native session working a real defect needs far longer.
    return GrokCodingSession(
        coding.reasoning.model,
        coding.session_timeout_seconds,
        effort=coding.reasoning.effort,
    )


def _build_coding_reasoning_model(
    reasoning,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None,
    purpose: str,
) -> ReasoningModel | None:
    """Build one configured Coding Agent reasoning adapter.

    Planning and local review deliberately call this separately, so they have
    independent model instances and selected-provider failures cannot cross.
    """
    if reasoning.provider == GROK_SUBSCRIPTION_PROVIDER:
        return GrokSubscriptionReasoningModel(
            reasoning.model, reasoning.timeout_seconds,
            telemetry_sink=telemetry_sink, effort=reasoning.effort,
        )
    if reasoning.provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        if not subscription_cli_present():
            raise ConfigurationError(
                f"the Claude Code CLI is required for {purpose}"
            )
        return ClaudeSubscriptionReasoningModel(
            reasoning.model, reasoning.timeout_seconds,
            telemetry_sink=telemetry_sink,
        )
    if reasoning.provider == CODEX_SUBSCRIPTION_PROVIDER:
        if not codex_subscription_cli_present():
            raise ConfigurationError(
                f"the Codex CLI is required for {purpose}"
            )
        return CodexSubscriptionReasoningModel(
            reasoning.model,
            reasoning.timeout_seconds,
            telemetry_sink=telemetry_sink,
            effort=reasoning.effort,
        )
    if reasoning.provider == "openai":
        return OpenAIReasoningModel(
            reasoning.model, reasoning.api_key,
            reasoning.base_url, reasoning.timeout_seconds,
            streaming=reasoning.streaming,
            service_tier=reasoning.service_tier,
            reasoning_effort=reasoning.effort,
            telemetry_sink=telemetry_sink,
        )
    raise ConfigurationError(
        f"{purpose} provider adapter is not installed: {reasoning.provider}"
    )


def _build_coding_model(
    settings: RuntimeSettings,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None,
) -> ReasoningModel | None:
    """The Coding Agent's planner model, or None when disabled."""
    coding = settings.coding
    if not coding.enabled or not coding.is_usable:
        LOGGER.info("Coding agent reasoning is disabled by configuration")
        return None
    return _build_coding_reasoning_model(
        coding.reasoning, telemetry_sink, "coding"
    )


def _build_coding_reviewer_model(
    settings: RuntimeSettings,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None,
) -> ReasoningModel | None:
    """The independent advisory local-review model, or None when disabled."""
    coding = settings.coding
    if not coding.enabled or not coding.is_usable:
        LOGGER.info("Coding reviewer reasoning is disabled by configuration")
        return None
    return _build_coding_reasoning_model(
        coding.reviewer, telemetry_sink, "coding reviewer"
    )


def build_runtime_providers(
    settings: RuntimeSettings,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> RuntimeProviders:
    if settings.reasoning.provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        # Friedl's Claude subscription, through the Claude Code CLI. No API key
        # is held, none is passed to the child, and there is no metered path to
        # fall back to: if the subscription cannot answer, the turn fails and
        # AL/X says nothing rather than quietly billing another provider.
        if not subscription_cli_present():
            raise ConfigurationError(
                "the Claude Code CLI is required for the "
                f"{CLAUDE_SUBSCRIPTION_PROVIDER} reasoner and was not found"
            )
        reasoning = ClaudeSubscriptionReasoningModel(
            settings.reasoning.model,
            settings.reasoning.timeout_seconds,
            telemetry_sink=telemetry_sink,
        )
    elif settings.reasoning.provider == "openai":
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
    # D-024a: the experimental autonomous Core, when one is configured. It is
    # built by the same function, from the same settings shape, so it differs
    # from the conversational Core only in provider, model and effort.
    autonomous = (
        None if settings.autonomous is None
        else _build_reasoning_model(settings.autonomous, telemetry_sink)
    )
    coding = _build_coding_model(settings, telemetry_sink)
    coding_reviewer = _build_coding_reviewer_model(settings, telemetry_sink)
    coding_session = _build_coding_session(settings)

    if settings.speech_to_text.provider != "cartesia":
        raise ConfigurationError(
            f"speech-to-text provider adapter is not installed: {settings.speech_to_text.provider}"
        )
    # Speech is a transport capability. A runtime configured without it starts,
    # reasons and records identically; only audio is absent. Refusing to start
    # would make being heard a precondition for thinking, and telling the Core
    # about it would let a missing speaker change what she says.
    speech_configured = settings.text_to_speech.provider.strip().lower() != "none"
    if speech_configured and settings.text_to_speech.provider != "elevenlabs":
        raise ConfigurationError(
            f"text-to-speech provider adapter is not installed: {settings.text_to_speech.provider}"
        )
    return RuntimeProviders(
        reasoning=reasoning,
        specialist=specialist,
        autonomous=autonomous,
        coding=coding,
        coding_reviewer=coding_reviewer,
        coding_session=coding_session,
        # Wrapped, not replaced. The gate decides which audio is worth
        # paying to transmit; what the audio means is still Cartesia's answer
        # and then AL/X's. Removing the wrapper restores the previous
        # behaviour exactly, including its cost.
        speech_to_text=GatedTranscriber(
            CartesiaTranscriber(
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
            settings.speech_to_text.sample_rate_hz,
        ),
        text_to_speech=None if not speech_configured else ElevenLabsSynthesizer(
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
