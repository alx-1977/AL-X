"""Environment-backed provider selection and connection settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class ConfigurationError(ValueError):
    pass


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"missing required configuration: {name}")
    return value


def _credential(
    environment: Mapping[str, str],
    generic_name: str,
    current_provider_name: str,
) -> str:
    value = environment.get(generic_name, "").strip()
    if value:
        return value
    return _required(environment, current_provider_name)


def _configured(
    environment: Mapping[str, str],
    generic_name: str,
    current_provider_name: str,
    fallback: str | None = None,
) -> str:
    for name in (generic_name, current_provider_name):
        value = environment.get(name, "").strip()
        if value:
            return value
    if fallback is not None:
        return fallback
    raise ConfigurationError(f"missing required configuration: {generic_name}")


def _positive_integer(environment: Mapping[str, str], name: str, fallback: int) -> int:
    raw = environment.get(name, str(fallback)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _number_in_range(
    environment: Mapping[str, str],
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    raw = _required(environment, name)
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _integer_in_range(
    environment: Mapping[str, str],
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    raw = _required(environment, name)
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ReasoningSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class SpeechToTextSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
    api_version: str
    encoding: str
    sample_rate_hz: int
    turn_start_threshold: float
    turn_eager_end_threshold: float
    turn_end_threshold: float
    turn_end_timeout_ms: int

    def __post_init__(self) -> None:
        if not (
            self.turn_start_threshold
            > self.turn_eager_end_threshold
            > self.turn_end_threshold
        ):
            raise ConfigurationError(
                "STT turn thresholds must be ordered start > eager end > end"
            )


@dataclass(frozen=True, slots=True)
class TextToSpeechSettings:
    provider: str
    model: str
    api_key: str
    voice_id: str
    base_url: str
    output_format: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    reasoning: ReasoningSettings
    speech_to_text: SpeechToTextSettings
    text_to_speech: TextToSpeechSettings

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RuntimeSettings:
        return cls(
            reasoning=ReasoningSettings(
                provider=_required(environment, "ALX_REASONING_PROVIDER"),
                model=_required(environment, "ALX_REASONING_MODEL"),
                api_key=_credential(environment, "ALX_REASONING_API_KEY", "XAI_API_KEY"),
                base_url=_configured(
                    environment,
                    "ALX_REASONING_BASE_URL",
                    "XAI_BASE_URL",
                    "https://api.x.ai",
                ).rstrip("/"),
                timeout_seconds=_positive_integer(environment, "ALX_REASONING_TIMEOUT_SECONDS", 120),
            ),
            speech_to_text=SpeechToTextSettings(
                provider=_required(environment, "ALX_STT_PROVIDER"),
                model=_required(environment, "ALX_STT_MODEL"),
                api_key=_credential(environment, "ALX_STT_API_KEY", "CARTESIA_API_KEY"),
                base_url=_configured(
                    environment,
                    "ALX_STT_BASE_URL",
                    "CARTESIA_STT_BASE_URL",
                    "wss://api.cartesia.ai",
                ).rstrip("/"),
                api_version=_configured(
                    environment, "ALX_STT_API_VERSION", "CARTESIA_API_VERSION"
                ),
                encoding=environment.get("ALX_STT_ENCODING", "pcm_s16le"),
                sample_rate_hz=_positive_integer(environment, "ALX_STT_SAMPLE_RATE_HZ", 16000),
                turn_start_threshold=_number_in_range(
                    environment, "ALX_STT_TURN_START_THRESHOLD", 0.5, 0.9
                ),
                turn_eager_end_threshold=_number_in_range(
                    environment, "ALX_STT_TURN_EAGER_END_THRESHOLD", 0.3, 0.6
                ),
                turn_end_threshold=_number_in_range(
                    environment, "ALX_STT_TURN_END_THRESHOLD", 0.05, 0.5
                ),
                turn_end_timeout_ms=_integer_in_range(
                    environment, "ALX_STT_TURN_END_TIMEOUT_MS", 640, 11200
                ),
            ),
            text_to_speech=TextToSpeechSettings(
                provider=_required(environment, "ALX_TTS_PROVIDER"),
                model=_required(environment, "ALX_TTS_MODEL"),
                api_key=_credential(environment, "ALX_TTS_API_KEY", "ELEVENLABS_API_KEY"),
                voice_id=_configured(
                    environment, "ALX_TTS_VOICE_ID", "ELEVENLABS_VOICE_ID"
                ),
                base_url=_configured(
                    environment,
                    "ALX_TTS_BASE_URL",
                    "ELEVENLABS_BASE_URL",
                    "https://api.elevenlabs.io",
                ).rstrip("/"),
                output_format=environment.get("ALX_TTS_OUTPUT_FORMAT", "mp3_44100_128"),
                timeout_seconds=_positive_integer(environment, "ALX_TTS_TIMEOUT_SECONDS", 60),
            ),
        )
