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
    # Where a processed supplier invoice is filed. Blank leaves mail in place.
    processed_mailbox: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "MailSettings":
        return cls(
            address=_required(environment, "MAIL_ADDRESS"),
            secret=_required(environment, "MAIL_KEY"),
            imap_host=_required(environment, "MAIL_IMAP_HOST"),
            imap_port=_integer_in_range(environment, "MAIL_IMAP_PORT", 1, 65535),
            poll_seconds=_positive_integer(environment, "ALX_MAIL_POLL_SECONDS", 15),
            processed_mailbox=environment.get(
                "ALX_MAIL_PROCESSED_MAILBOX", ""
            ).strip(),
        )

    def __repr__(self) -> str:
        return (
            f"MailSettings(address={self.address!r}, secret=<redacted>, "
            f"imap_host={self.imap_host!r}, imap_port={self.imap_port!r}, "
            f"poll_seconds={self.poll_seconds!r}, "
            f"processed_mailbox={self.processed_mailbox!r})"
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
class XeroSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    tenant_id: str
    timeout_seconds: int
    approval_ttl_seconds: int
    unattended_bill_writes: bool
    unattended_bill_deletes: bool
    # Where a supplier's own history gives no single answer, the account is a
    # policy choice no document contains. Blank leaves it unresolved and asks.
    default_account_code: str
    default_tax_type: str
    # V1's proven DHL treatment: import VAT is claimable, duty is not, and
    # clearance is a service charge. Configurable for another organisation.
    import_vat_account: str
    customs_duty_account: str
    clearance_account: str
    # D-021: the DHL supplier is configuration, so a wrong contact cannot be
    # supplied to the import capability. It is a name, not a Xero identifier:
    # the contact is resolved by exact name at run time, as V1 did.
    dhl_supplier_name: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "XeroSettings":
        return cls(
            client_id=_required(environment, "XERO_CLIENT_ID"),
            client_secret=_required(environment, "XERO_CLIENT_SECRET"),
            redirect_uri=_required(environment, "XERO_REDIRECT_URI"),
            tenant_id=environment.get("XERO_TENANT_ID", "").strip(),
            timeout_seconds=_positive_integer(
                environment, "ALX_XERO_TIMEOUT_SECONDS", 60
            ),
            approval_ttl_seconds=_positive_integer(
                environment, "ALX_XERO_APPROVAL_TTL_SECONDS", 600
            ),
            # D-018. Friedl authorised unattended supplier-bill writes. The
            # default stays attended so the authority is an explicit choice.
            unattended_bill_writes=_boolean(
                environment, "ALX_XERO_UNATTENDED_BILL_WRITES", False
            ),
            # D-019. Discarding a draft is requested, not routine, so it
            # defaults to asking even where bill writes run unattended.
            unattended_bill_deletes=_boolean(
                environment, "ALX_XERO_UNATTENDED_BILL_DELETES", False
            ),
            default_account_code=environment.get(
                "ALX_XERO_DEFAULT_ACCOUNT_CODE", ""
            ).strip(),
            default_tax_type=environment.get(
                "ALX_XERO_DEFAULT_TAX_TYPE", ""
            ).strip(),
            import_vat_account=environment.get(
                "ALX_XERO_IMPORT_VAT_ACCOUNT", "820"
            ).strip(),
            customs_duty_account=environment.get(
                "ALX_XERO_CUSTOMS_DUTY_ACCOUNT", "426"
            ).strip(),
            clearance_account=environment.get(
                "ALX_XERO_CLEARANCE_ACCOUNT", "425"
            ).strip(),
            dhl_supplier_name=environment.get(
                "ALX_XERO_DHL_SUPPLIER_NAME", "DHL International (Pty) Ltd"
            ).strip(),
        )

    def __repr__(self) -> str:
        return (
            f"XeroSettings(client_id={self.client_id!r}, "
            f"client_secret=<redacted>, redirect_uri={self.redirect_uri!r}, "
            f"tenant_id={self.tenant_id!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"approval_ttl_seconds={self.approval_ttl_seconds!r}, "
            f"unattended_bill_writes={self.unattended_bill_writes!r}, "
            f"unattended_bill_deletes={self.unattended_bill_deletes!r})"
        )



def _specialist_settings(
    environment: Mapping[str, str], core_provider: str
) -> "ReasoningSettings":
    """Configure the specialist independently, defaulting to the Core provider.

    Extraction is a bounded structured question, so it defaults to the lowest
    reasoning setting that still returns reliable structured output. Nothing
    here changes the Core.
    """
    provider = environment.get(
        "ALX_SPECIALIST_PROVIDER", core_provider
    ).strip().lower() or core_provider
    key_name = {
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
        "kimi": "KIMI_API_KEY",
    }.get(provider, "ALX_SPECIALIST_API_KEY")
    base_name = {
        "openai": "OPENAI_BASE_URL",
        "xai": "XAI_BASE_URL",
        "kimi": "KIMI_BASE_URL",
    }.get(provider, "ALX_SPECIALIST_BASE_URL")
    base_fallback = {
        "openai": "https://api.openai.com",
        "xai": "https://api.x.ai",
        "kimi": "https://api.moonshot.ai",
    }.get(provider)
    return ReasoningSettings(
        provider=provider,
        model=_configured(
            environment,
            "ALX_SPECIALIST_MODEL",
            "ALX_REASONING_MODEL",
        ),
        # Defaults to the Core provider's credential; only a specialist on a
        # different provider needs a key of its own.
        api_key=_configured(
            environment,
            "ALX_SPECIALIST_API_KEY",
            key_name,
            environment.get("ALX_REASONING_API_KEY", "").strip() or None,
        ),
        base_url=_configured(
            environment,
            "ALX_SPECIALIST_BASE_URL",
            base_name,
            base_fallback
            or environment.get("ALX_REASONING_BASE_URL", "").strip()
            or "",
        ).rstrip("/"),
        timeout_seconds=_positive_integer(
            environment, "ALX_SPECIALIST_TIMEOUT_SECONDS", 60
        ),
        streaming=_boolean(environment, "ALX_SPECIALIST_STREAMING", False),
        service_tier=environment.get(
            "ALX_SPECIALIST_SERVICE_TIER", "default"
        ).strip().lower(),
        effort=environment.get("ALX_SPECIALIST_EFFORT", "none").strip().lower(),
    )


def _tier_settings(
    environment: Mapping[str, str], tier: str, specialist: "ReasoningSettings"
) -> "ReasoningSettings":
    """Configure one cognition tier, defaulting to the specialist settings.

    A tier is named for how hard the thinking is, never for a vendor. Which
    model serves SURVEY, COMPARE or JUDGE is entirely configuration, so moving
    a tier to another provider needs no code change and creates no second path.
    """
    prefix = f"ALX_RESEARCH_{tier.upper()}"
    provider = (
        environment.get(f"{prefix}_PROVIDER", "").strip().lower()
        or specialist.provider
    )
    key_name = {
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
        "kimi": "KIMI_API_KEY",
    }.get(provider, f"{prefix}_API_KEY")
    base_name = {
        "openai": "OPENAI_BASE_URL",
        "xai": "XAI_BASE_URL",
        "kimi": "KIMI_BASE_URL",
    }.get(provider, f"{prefix}_BASE_URL")
    base_fallback = {
        "openai": "https://api.openai.com",
        "xai": "https://api.x.ai",
        "kimi": "https://api.moonshot.ai",
    }.get(provider, specialist.base_url)
    return ReasoningSettings(
        provider=provider,
        model=environment.get(f"{prefix}_MODEL", "").strip() or specialist.model,
        api_key=_configured(
            environment,
            f"{prefix}_API_KEY",
            key_name,
            specialist.api_key,
        ),
        base_url=_configured(
            environment,
            f"{prefix}_BASE_URL",
            base_name,
            base_fallback,
        ).rstrip("/"),
        timeout_seconds=_positive_integer(
            environment, f"{prefix}_TIMEOUT_SECONDS", specialist.timeout_seconds
        ),
        streaming=_boolean(environment, f"{prefix}_STREAMING", specialist.streaming),
        service_tier=environment.get(
            f"{prefix}_SERVICE_TIER", specialist.service_tier
        ).strip().lower(),
        effort=environment.get(f"{prefix}_EFFORT", specialist.effort).strip().lower(),
    )


def autonomous_cognition_daily_budget_usd(environment: Mapping[str, str]) -> float:
    """Friedl's hard daily ceiling on autonomous Core cognition.

    Denominated in USD because every recorded rate is USD and the provider
    bills in USD, so no currency conversion happens anywhere in the spending
    path. The Rand figure it was chosen from is recorded in D-024a, not
    computed here: a fuse whose size moved with the exchange rate would be a
    different ceiling every day.

    Defaults to zero, so a runtime that has never been told it may spend on
    autonomous cognition cannot.
    """
    return _number_in_range(
        environment, "AUTONOMOUS_COGNITION_DAILY_BUDGET_USD", 0.0, 1000.0, 0.0
    )


def _research_budget(environment: Mapping[str, str]) -> "ResearchLimits":
    """Friedl's hard research spending boundary."""
    daily = _number_in_range(
        environment, "RESEARCH_DAILY_BUDGET_USD", 0.0, 1000.0, 0.0
    )
    per_request = _number_in_range(
        environment, "RESEARCH_PER_REQUEST_MAX_USD", 0.0, 1000.0, 0.0
    )
    return ResearchLimits(daily_usd=daily, per_request_max_usd=per_request)


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    """Configured research ceiling, validated where the ledger is built."""

    daily_usd: float
    per_request_max_usd: float


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """Cognition tiers and the spending ceiling that governs them."""

    survey: ReasoningSettings
    compare: ReasoningSettings
    judge: ReasoningSettings
    limits: ResearchLimits
    # Which tiers may actually run. Empty disables paid research entirely, so a
    # runtime that has never been told which tiers are authorised cannot spend.
    # The first live test enables SURVEY alone; COMPARE and JUDGE stay
    # unconstructed rather than merely unaffordable, because price is not a
    # permission and all three fit the configured ceiling.
    enabled_tiers: frozenset[str] = frozenset()


def _enabled_tiers(environment: Mapping[str, str]) -> frozenset[str]:
    """The cognition tiers this runtime may build, defaulting to none.

    Paid research is off until a tier is named. Defaulting to every tier would
    mean a runtime that had never been configured for research could still
    spend, which is the opposite of the ceiling's intent.
    """
    raw = environment.get("ALX_RESEARCH_ENABLED_TIERS", "").strip()
    if not raw:
        return frozenset()
    names = {item.strip().lower() for item in raw.split(",") if item.strip()}
    allowed = {"survey", "compare", "judge"}
    unknown = names - allowed
    if unknown:
        raise ConfigurationError(
            "ALX_RESEARCH_ENABLED_TIERS may name only survey, compare or judge; "
            f"unknown: {', '.join(sorted(unknown))}"
        )
    return frozenset(names)


def _research_settings(
    environment: Mapping[str, str], core_provider: str
) -> "ResearchSettings":
    specialist = _specialist_settings(environment, core_provider)
    return ResearchSettings(
        survey=_tier_settings(environment, "survey", specialist),
        compare=_tier_settings(environment, "compare", specialist),
        judge=_tier_settings(environment, "judge", specialist),
        limits=_research_budget(environment),
        enabled_tiers=_enabled_tiers(environment),
    )


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    reasoning: ReasoningSettings
    # Bounded extraction does not need Core-level reasoning, and reasoning
    # tokens were the dominant cost. The specialist is configured separately so
    # tuning it never disturbs the Core.
    specialist: ReasoningSettings
    # Cognition tiers for research. Configuration only: a tier chooses which
    # model answers a bounded question, never what AL/X investigates.
    research: ResearchSettings
    speech_to_text: SpeechToTextSettings
    text_to_speech: TextToSpeechSettings

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RuntimeSettings:
        reasoning_provider = _required(environment, "ALX_REASONING_PROVIDER")
        provider_key_name = {
            "openai": "OPENAI_API_KEY",
            "xai": "XAI_API_KEY",
            "kimi": "KIMI_API_KEY",
        }.get(reasoning_provider, "ALX_REASONING_API_KEY")
        provider_base_name = {
            "openai": "OPENAI_BASE_URL",
            "xai": "XAI_BASE_URL",
            "kimi": "KIMI_BASE_URL",
        }.get(reasoning_provider, "ALX_REASONING_BASE_URL")
        provider_base_fallback = {
            "openai": "https://api.openai.com",
            "xai": "https://api.x.ai",
            "kimi": "https://api.moonshot.ai",
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
            specialist=_specialist_settings(environment, reasoning_provider),
            research=_research_settings(environment, reasoning_provider),
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
