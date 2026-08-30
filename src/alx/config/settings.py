"""Environment-backed provider selection and connection settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


def _boolean(environment: Mapping[str, str], name: str, fallback: bool) -> bool:
    raw = environment.get(name, "true" if fallback else "false").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _number_in_range(
    environment: Mapping[str, str],
    name: str,
    minimum: float,
    maximum: float,
    fallback: float | None = None,
) -> float:
    raw = (
        _required(environment, name)
        if fallback is None
        else environment.get(name, str(fallback)).strip()
    )
    if not raw:
        raise ConfigurationError(f"missing required configuration: {name}")
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
    streaming: bool
    service_tier: str
    effort: str

    def __post_init__(self) -> None:
        if self.service_tier not in ("default", "priority"):
            raise ConfigurationError(
                "ALX_REASONING_SERVICE_TIER must be default or priority"
            )
        if self.effort not in ("none", "low", "medium", "high", "xhigh", "max"):
            raise ConfigurationError(
                "ALX_REASONING_EFFORT must be none, low, medium, high, xhigh, or max"
            )


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
    pronunciation_dictionary_id: str
    pronunciation_dictionary_version_id: str
    speed: float
    stability: float
    similarity_boost: float
    speaker_boost: bool


@dataclass(frozen=True, slots=True)
class MailSettings:
    address: str
    secret: str
    imap_host: str
    imap_port: int
    poll_seconds: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "MailSettings":
        return cls(
            address=_required(environment, "MAIL_ADDRESS"),
            secret=_required(environment, "MAIL_KEY"),
            imap_host=_required(environment, "MAIL_IMAP_HOST"),
            imap_port=_integer_in_range(environment, "MAIL_IMAP_PORT", 1, 65535),
            poll_seconds=_positive_integer(environment, "ALX_MAIL_POLL_SECONDS", 15),
        )

    def __repr__(self) -> str:
        return (
            f"MailSettings(address={self.address!r}, secret=<redacted>, "
            f"imap_host={self.imap_host!r}, imap_port={self.imap_port!r}, "
            f"poll_seconds={self.poll_seconds!r})"
        )


@dataclass(frozen=True, slots=True)
class MailSendSettings:
    """Send authority configuration, deliberately separate from reading.

    The sender identity is fixed here. AL/X may not choose or change it, so no
    capability accepts a sender argument.
    """

    address: str
    secret: str
    smtp_host: str
    smtp_port: int
    timeout_seconds: int
    approval_ttl_seconds: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "MailSendSettings":
        return cls(
            address=_required(environment, "MAIL_ADDRESS"),
            secret=_required(environment, "MAIL_KEY"),
            smtp_host=_required(environment, "MAIL_SMTP_HOST"),
            smtp_port=_integer_in_range(environment, "MAIL_SMTP_PORT", 1, 65535),
            timeout_seconds=_positive_integer(
                environment, "ALX_MAIL_SEND_TIMEOUT_SECONDS", 60
            ),
            approval_ttl_seconds=_positive_integer(
                environment, "ALX_MAIL_APPROVAL_TTL_SECONDS", 600
            ),
        )

    def __repr__(self) -> str:
        return (
            f"MailSendSettings(address={self.address!r}, secret=<redacted>, "
            f"smtp_host={self.smtp_host!r}, smtp_port={self.smtp_port!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"approval_ttl_seconds={self.approval_ttl_seconds!r})"
        )


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    reasoning: ReasoningSettings
    speech_to_text: SpeechToTextSettings
    text_to_speech: TextToSpeechSettings

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RuntimeSettings:
        reasoning_provider = _required(environment, "ALX_REASONING_PROVIDER")
        provider_key_name = {
            "openai": "OPENAI_API_KEY",
            "xai": "XAI_API_KEY",
        }.get(reasoning_provider, "ALX_REASONING_API_KEY")
        provider_base_name = {
            "openai": "OPENAI_BASE_URL",
            "xai": "XAI_BASE_URL",
        }.get(reasoning_provider, "ALX_REASONING_BASE_URL")
        provider_base_fallback = {
            "openai": "https://api.openai.com",
            "xai": "https://api.x.ai",
        }.get(reasoning_provider)
        return cls(
            reasoning=ReasoningSettings(
                provider=reasoning_provider,
                model=_required(environment, "ALX_REASONING_MODEL"),
                api_key=_credential(
                    environment, "ALX_REASONING_API_KEY", provider_key_name
                ),
                base_url=_configured(
                    environment,
                    "ALX_REASONING_BASE_URL",
                    provider_base_name,
                    provider_base_fallback,
                ).rstrip("/"),
                timeout_seconds=_positive_integer(environment, "ALX_REASONING_TIMEOUT_SECONDS", 120),
                streaming=_boolean(environment, "ALX_REASONING_STREAMING", True),
                service_tier=environment.get(
                    "ALX_REASONING_SERVICE_TIER", "default"
                ).strip().lower(),
                effort=environment.get(
                    "ALX_REASONING_EFFORT", "medium"
                ).strip().lower(),
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
                pronunciation_dictionary_id=_required(
                    environment, "ALX_TTS_PRONUNCIATION_DICTIONARY_ID"
                ),
                pronunciation_dictionary_version_id=_required(
                    environment, "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID"
                ),
                speed=_number_in_range(
                    environment, "ALX_TTS_SPEED", 0.7, 1.2, 1.0
                ),
                stability=_number_in_range(
                    environment, "ALX_TTS_STABILITY", 0.0, 1.0, 0.5
                ),
                similarity_boost=_number_in_range(
                    environment, "ALX_TTS_SIMILARITY_BOOST", 0.0, 1.0, 0.75
                ),
                speaker_boost=_boolean(
                    environment, "ALX_TTS_SPEAKER_BOOST", True
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class LiveVoiceSettings:
    host: str
    port: int
    storage_root: Path
    primary_person_id: str
    goal_retention_days: int
    core_step_budget: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> LiveVoiceSettings:
        return cls(
            host=_required(environment, "ALX_INTERFACE_HOST"),
            port=_integer_in_range(environment, "ALX_INTERFACE_PORT", 1, 65535),
            storage_root=Path(
                _required(environment, "ALX_RUNTIME_STORAGE_ROOT")
            ).expanduser(),
            primary_person_id=_required(environment, "ALX_PRIMARY_PERSON_ID"),
            goal_retention_days=_positive_integer(
                environment, "ALX_GOAL_RETENTION_DAYS", 3650
            ),
            core_step_budget=_positive_integer(environment, "ALX_CORE_STEP_BUDGET", 8),
        )
