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


LLAMAPARSE_DEFAULT_BASE_URL = "https://api.cloud.llamaindex.ai"


@dataclass(frozen=True, slots=True)
class LlamaParseSettings:
    """LlamaCloud structured extraction for supplier invoices, not Core cognition.

    Capture is advertised only when this is usable. An empty key leaves the
    adapter unbuilt: there is no fallback to the Core or the generic specialist.
    """

    api_key: str
    base_url: str
    timeout_seconds: int
    project_id: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "LlamaParseSettings":
        api_key = environment.get("ALX_LLAMAPARSE_API_KEY", "").strip()
        if not api_key:
            api_key = environment.get("LLAMA_CLOUD_API_KEY", "").strip()
        return cls(
            api_key=api_key,
            base_url=environment.get(
                "ALX_LLAMAPARSE_BASE_URL", LLAMAPARSE_DEFAULT_BASE_URL
            ).strip()
            or LLAMAPARSE_DEFAULT_BASE_URL,
            timeout_seconds=_positive_integer(
                environment, "ALX_LLAMAPARSE_TIMEOUT_SECONDS", 60
            ),
            project_id=environment.get("ALX_LLAMAPARSE_PROJECT_ID", "").strip(),
        )

    @property
    def is_usable(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return (
            f"LlamaParseSettings(api_key=<redacted>, base_url={self.base_url!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"project_id={self.project_id!r})"
        )



# The Core reasoner that runs on Friedl's Claude subscription instead of metered
# API credit. Named here so configuration, the adapter and the tests agree on
# one spelling.
CLAUDE_SUBSCRIPTION_PROVIDER = "claude_subscription"
CODEX_SUBSCRIPTION_PROVIDER = "codex_subscription"

# Grok CLI subscription used by the Coding Agent. Named to match the Claude
# subscription spelling: a CLI login, not a metered API key.
GROK_SUBSCRIPTION_PROVIDER = "grok_subscription"

# A provider deliberately configured as absent. The runtime builds nothing for
# it, reads no credential, and the work it would have done refuses instead of
# being answered somewhere more expensive. Already the spelling text-to-speech
# uses, reused here rather than inventing a second word for the same idea.
NO_PROVIDER = "none"


def _core_reasoning_settings(
    environment: Mapping[str, str],
    provider: str,
    provider_key_name: str,
    provider_base_name: str,
    provider_base_fallback: "str | None",
) -> "ReasoningSettings":
    """The conversational Core's reasoner.

    The subscription path is configured differently from the metered ones
    because it genuinely is different: it authenticates through the Claude Code
    installation, so there is no key to supply and no base URL to point at.
    Demanding either would refuse a correct configuration, and accepting a key
    would put a billable credential into the one path whose entire purpose is
    not to use one.

    Nothing else about the Core changes. The same dataclass is returned, so the
    reasoner, the loop and the capability layer see one shape.
    """
    if provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        api_key = environment.get("ALX_REASONING_API_KEY", "").strip()
        if api_key:
            # Refused rather than ignored. A key sitting in the configuration
            # of a path that must never bill is either a misunderstanding or a
            # leftover, and silently not using it would leave Friedl believing
            # something about this runtime that is not true.
            raise ConfigurationError(
                "ALX_REASONING_API_KEY must not be set for "
                f"{CLAUDE_SUBSCRIPTION_PROVIDER}: it authenticates through the "
                "Claude Code subscription and never through a billed key"
            )
        return ReasoningSettings(
            provider=provider,
            model=_required(environment, "ALX_REASONING_MODEL"),
            api_key="",
            base_url="",
            timeout_seconds=_positive_integer(
                environment, "ALX_REASONING_TIMEOUT_SECONDS", 300
            ),
            # The port is not a streaming port: `complete()` returns one whole
            # completion. The CLI is asked for one JSON result, so there is no
            # stream to enable and the setting would describe nothing.
            streaming=False,
            service_tier="default",
            # A subscription turn has no tier or effort dial to set. Fixed at
            # the neutral value rather than read from configuration, so no
            # setting appears to be active while doing nothing.
            effort="medium",
        )
    return ReasoningSettings(
        provider=provider,
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
        timeout_seconds=_positive_integer(
            environment, "ALX_REASONING_TIMEOUT_SECONDS", 120
        ),
        streaming=_boolean(environment, "ALX_REASONING_STREAMING", True),
        service_tier=environment.get(
            "ALX_REASONING_SERVICE_TIER", "default"
        ).strip().lower(),
        effort=environment.get("ALX_REASONING_EFFORT", "medium").strip().lower(),
    )


def _specialist_settings(
    environment: Mapping[str, str], core_provider: str
) -> "ReasoningSettings":
    """Configure the specialist independently, defaulting to the Core provider.

    Extraction is a bounded structured question, so it defaults to the lowest
    reasoning setting that still returns reliable structured output. Nothing
    here changes the Core.
    """
    requested = environment.get("ALX_SPECIALIST_PROVIDER", "").strip().lower()
    provider = requested or core_provider
    if provider == CLAUDE_SUBSCRIPTION_PROVIDER and requested:
        # Asked for by name. Specialist cognition is not authorised on the
        # subscription path, so this is refused rather than quietly turned
        # into something else: a configuration that names a provider and
        # silently gets another is worse than one that stops.
        raise ConfigurationError(
            f"{CLAUDE_SUBSCRIPTION_PROVIDER} is not available for specialist "
            "cognition; set ALX_SPECIALIST_PROVIDER=none to disable it"
        )
    if provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        # This change is scoped to the conversational Core. The specialist
        # inherits the Core's provider by default, so a Core moved to the
        # subscription would otherwise drag specialist extraction onto it
        # silently - a second cognition path switched by a setting nobody
        # pointed at it. There is no subscription specialist, so the default
        # becomes "none": absent, and refusing, rather than quietly running on
        # a path that was never chosen for it.
        provider = NO_PROVIDER
    if provider == NO_PROVIDER:
        # Deliberately absent. Nothing is built, so no credential is read and
        # no metered client exists to be invoked by accident. Extraction then
        # refuses rather than falling back to the Core, which is the expensive
        # path that separation exists to avoid.
        return ReasoningSettings(
            provider=NO_PROVIDER,
            model=NO_PROVIDER,
            api_key="",
            base_url="",
            timeout_seconds=_positive_integer(
                environment, "ALX_SPECIALIST_TIMEOUT_SECONDS", 60
            ),
            streaming=False,
            service_tier="default",
            effort="none",
        )
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
    if provider == NO_PROVIDER:
        # A tier whose provider is absent stays absent. Research already
        # refuses a tier it cannot build; what matters here is that no
        # credential is required and no metered client is constructed.
        return ReasoningSettings(
            provider=NO_PROVIDER,
            model=NO_PROVIDER,
            api_key="",
            base_url="",
            timeout_seconds=specialist.timeout_seconds,
            streaming=False,
            service_tier="default",
            effort="none",
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
    if specialist.provider == NO_PROVIDER:
        model = environment.get(f"{prefix}_MODEL", "").strip()
        if not model or model.lower() == NO_PROVIDER:
            raise ConfigurationError(f"{prefix}_MODEL must name a usable model")
        api_key = _credential(environment, f"{prefix}_API_KEY", key_name)
    else:
        model = environment.get(f"{prefix}_MODEL", "").strip() or specialist.model
        api_key = _configured(
            environment, f"{prefix}_API_KEY", key_name, specialist.api_key
        )
    return ReasoningSettings(
        provider=provider,
        model=model,
        api_key=api_key,
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


AUTONOMOUS_MAX_OUTPUT_TOKENS = 32_000
# The exact provider, model and effort EX-001 approves for autonomous turns.
# Not a default: the only value configuration may take.
AUTONOMOUS_APPROVED_IDENTITY = ("openai", "gpt-5.6-luna", "max")
# The input ceiling the autonomous reservation is computed against. Enforced on
# the constructed request before dispatch: a bound nothing checks makes the
# worst case a guess rather than a ceiling.
#
# 96,000 rather than 32,000, corrected in D-024a on 2026-09-03. A real Core
# request measures roughly 59.6k input units with the full capability catalogue
# and an empty conversation, so 32,000 guaranteed refusal and 64,000 left no
# room for the conversation, goals and thoughts that make a turn worth having.
# The alternative was a thinner prompt for the autonomous Core, which D-024a
# forbids: both origins must reason in the same identity and capability
# environment, or the experiment compares two different minds.
AUTONOMOUS_MAX_INPUT_TOKENS = 96_000


def autonomous_reasoning_settings(
    environment: Mapping[str, str],
) -> "ReasoningSettings | None":
    """The Core that answers an autonomous turn, or None when unconfigured.

    Recorded under D-024a as a time-boxed experiment. Absent configuration
    disables the second reasoner entirely rather than falling back to the
    conversational Core, because a silent fallback would make the experiment
    invisible: an autonomous turn would still run, on a model nobody chose.
    """
    provider = environment.get("ALX_AUTONOMOUS_PROVIDER", "").strip().lower()
    model = environment.get("ALX_AUTONOMOUS_MODEL", "").strip()
    if not provider or not model:
        return None
    effort = environment.get("ALX_AUTONOMOUS_EFFORT", "max").strip().lower()
    # EX-001 authorises one exact arrangement, so configuration may install one
    # exact arrangement. Anything else would put an unapproved reasoning
    # authority into production under cover of an exception that does not
    # describe it, and a typo would do it silently. Widening this set requires
    # widening the exception first, which is Friedl's decision and not a
    # configuration change.
    if (provider, model, effort) != AUTONOMOUS_APPROVED_IDENTITY:
        approved = "/".join(AUTONOMOUS_APPROVED_IDENTITY)
        raise ConfigurationError(
            f"autonomous cognition is approved under EX-001 for {approved} "
            f"only; refusing {provider}/{model}/{effort}"
        )
    key_name = {
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
        "kimi": "KIMI_API_KEY",
    }.get(provider, "ALX_AUTONOMOUS_API_KEY")
    base_name = {
        "openai": "OPENAI_BASE_URL",
        "xai": "XAI_BASE_URL",
        "kimi": "KIMI_BASE_URL",
    }.get(provider, "ALX_AUTONOMOUS_BASE_URL")
    base_fallback = {
        "openai": "https://api.openai.com",
        "xai": "https://api.x.ai",
        "kimi": "https://api.moonshot.ai",
    }.get(provider, "")
    return ReasoningSettings(
        provider=provider,
        model=model,
        api_key=_configured(
            environment, "ALX_AUTONOMOUS_API_KEY", key_name, ""
        ),
        base_url=_configured(
            environment, "ALX_AUTONOMOUS_BASE_URL", base_name, base_fallback
        ).rstrip("/"),
        timeout_seconds=_positive_integer(
            environment, "ALX_AUTONOMOUS_TIMEOUT_SECONDS", 120
        ),
        streaming=_boolean(environment, "ALX_AUTONOMOUS_STREAMING", False),
        service_tier=environment.get(
            "ALX_AUTONOMOUS_SERVICE_TIER", "default"
        ).strip().lower(),
        effort=effort,
    )


def autonomous_due_check_seconds(environment: Mapping[str, str]) -> float:
    """How promptly a matured request is noticed. Not a cognition cadence.

    It bounds the delay between a `not_before` passing and the runtime seeing
    it. It says nothing about how often AL/X thinks: with no pending request
    the tick runs forever and invokes her never.
    """
    return _number_in_range(
        environment, "ALX_AUTONOMOUS_DUE_CHECK_SECONDS", 1.0, 3600.0, 30.0
    )


def autonomous_commissioning_limit(environment: Mapping[str, str]) -> int | None:
    """Temporary one-shot safety for the first supervised activation.

    Counts dispatch attempts, because the financial fuse cannot limit turns: a
    reservation reconciles to actual spend, so a cheap turn returns most of its
    withdrawal and the next fits. Measured, a $0.1632 fuse permits 79
    dispatches rather than two.

    Absent in normal operation, which is the point: this is commissioning
    safety and must never become a cognition quota or a cadence rule.
    """
    raw = environment.get("ALX_AUTONOMOUS_COMMISSIONING_DISPATCHES", "").strip()
    if not raw:
        return None
    limit = int(raw)
    if limit <= 0:
        raise ConfigurationError(
            "ALX_AUTONOMOUS_COMMISSIONING_DISPATCHES must be positive when set"
        )
    return limit


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
    enabled = _enabled_tiers(environment)
    tiers = {
        name: (
            _tier_settings(environment, name, specialist)
            if name in enabled
            else ReasoningSettings(
                provider=NO_PROVIDER,
                model=NO_PROVIDER,
                api_key="",
                base_url="",
                timeout_seconds=specialist.timeout_seconds,
                streaming=False,
                service_tier="default",
                effort="none",
            )
        )
        for name in ("survey", "compare", "judge")
    }
    for name in enabled:
        tier = tiers[name]
        if (tier.provider == NO_PROVIDER or tier.model.lower() == NO_PROVIDER
                or not tier.model.strip() or not tier.api_key.strip()):
            raise ConfigurationError(
                f"enabled research tier {name} requires a usable provider, model and credential"
            )
    return ResearchSettings(
        **tiers,
        limits=_research_budget(environment),
        enabled_tiers=enabled,
    )


# Twenty minutes. A planning call answers in seconds; a session that inspects
# a repository, edits it and reports back needs room to finish, and cutting one
# off mid-edit is what `session_timeout` evidence showed.
DEFAULT_CODING_SESSION_TIMEOUT_SECONDS = 1200


@dataclass(frozen=True, slots=True)
class CodingSettings:
    """Coding-agent model and authority, off until it is configured.

    Independent of the conversational Core. A Core on the Claude subscription
    must not drag coding jobs onto that path, and coding jobs must not change
    Core configuration.
    """

    enabled: bool
    reasoning: ReasoningSettings
    # The local reviewer is independently configured. It is advisory only,
    # but must never silently become the coding model (or vice versa).
    reviewer: ReasoningSettings
    # A native coding session is a multi-turn agent working a real defect, not
    # one model call. Sizing it from `reasoning.timeout_seconds` killed a
    # working session after two minutes, so it carries its own bound.
    session_timeout_seconds: int = DEFAULT_CODING_SESSION_TIMEOUT_SECONDS

    @property
    def is_usable(self) -> bool:
        return bool(
            self.enabled
            and self.reasoning.provider
            in (GROK_SUBSCRIPTION_PROVIDER, CLAUDE_SUBSCRIPTION_PROVIDER, "openai")
            and self.reasoning.model.strip()
            and self.reasoning.model.lower() != NO_PROVIDER
            and self.reviewer.provider
            in (
                GROK_SUBSCRIPTION_PROVIDER,
                CLAUDE_SUBSCRIPTION_PROVIDER,
                CODEX_SUBSCRIPTION_PROVIDER,
                "openai",
            )
            and self.reviewer.model.strip()
            and self.reviewer.model.lower() != NO_PROVIDER
        )


def _coding_reviewer_settings(
    environment: Mapping[str, str],
) -> ReasoningSettings:
    """Configure the local reviewer without inheriting Coding Agent settings."""
    provider = environment.get("ALX_CODING_REVIEWER_PROVIDER", "").strip().lower()
    if not provider or provider == NO_PROVIDER:
        return ReasoningSettings(
            provider=NO_PROVIDER,
            model=NO_PROVIDER,
            api_key="",
            base_url="",
            timeout_seconds=120,
            streaming=False,
            service_tier="default",
            effort="none",
        )
    if provider not in (
        GROK_SUBSCRIPTION_PROVIDER,
        CLAUDE_SUBSCRIPTION_PROVIDER,
        CODEX_SUBSCRIPTION_PROVIDER,
        "openai",
    ):
        raise ConfigurationError(
            f"coding reviewer provider adapter is not installed: {provider}"
        )
    if provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        if environment.get("ALX_CODING_REVIEWER_API_KEY", "").strip():
            raise ConfigurationError(
                "ALX_CODING_REVIEWER_API_KEY must not be set for "
                "claude_subscription"
            )
        return ReasoningSettings(
            provider=provider,
            model=_required(environment, "ALX_CODING_REVIEWER_MODEL"),
            api_key="",
            base_url="",
            timeout_seconds=_positive_integer(
                environment, "ALX_CODING_TIMEOUT_SECONDS", 120
            ),
            streaming=False,
            service_tier="default",
            effort="medium",
        )
    if provider == CODEX_SUBSCRIPTION_PROVIDER:
        if environment.get("ALX_CODING_REVIEWER_API_KEY", "").strip():
            raise ConfigurationError(
                "ALX_CODING_REVIEWER_API_KEY must not be set for "
                "codex_subscription"
            )
        return ReasoningSettings(
            provider=provider,
            model=_required(environment, "ALX_CODING_REVIEWER_MODEL"),
            api_key="",
            base_url="",
            timeout_seconds=_positive_integer(
                environment, "ALX_CODING_TIMEOUT_SECONDS", 120
            ),
            streaming=False,
            service_tier="default",
            effort=_coding_effort(environment, "ALX_CODING_REVIEWER_EFFORT"),
        )
    if provider == "openai":
        return ReasoningSettings(
            provider=provider,
            model=_required(environment, "ALX_CODING_REVIEWER_MODEL"),
            api_key=_credential(
                environment, "ALX_CODING_REVIEWER_API_KEY", "OPENAI_API_KEY"
            ),
            base_url=_configured(
                environment,
                "ALX_CODING_REVIEWER_BASE_URL",
                "OPENAI_BASE_URL",
                "https://api.openai.com",
            ).rstrip("/"),
            timeout_seconds=_positive_integer(
                environment, "ALX_CODING_TIMEOUT_SECONDS", 120
            ),
            streaming=False,
            service_tier=environment.get(
                "ALX_CODING_REVIEWER_SERVICE_TIER", "default"
            ).strip().lower(),
            effort=_coding_effort(environment, "ALX_CODING_REVIEWER_EFFORT"),
        )
    return ReasoningSettings(
        provider=GROK_SUBSCRIPTION_PROVIDER,
        model=_required(environment, "ALX_CODING_REVIEWER_MODEL"),
        api_key="",
        base_url="",
        timeout_seconds=_positive_integer(environment, "ALX_CODING_TIMEOUT_SECONDS", 120),
        streaming=False,
        service_tier="default",
        effort=_coding_effort(environment, "ALX_CODING_REVIEWER_EFFORT"),
    )


def _coding_settings(environment: Mapping[str, str]) -> "CodingSettings":
    """Configure the coding backend independently of the Core.

    Defaults to the Grok CLI subscription when enabled. Provider selection is
    explicit and independent from the Core; no configuration path substitutes
    another provider when the selected one is unavailable.
    """
    enabled = _boolean(environment, "ALX_CODING_ENABLED", False)
    absent = ReasoningSettings(
        provider=NO_PROVIDER,
        model=NO_PROVIDER,
        api_key="",
        base_url="",
        timeout_seconds=120,
        streaming=False,
        service_tier="default",
        effort="none",
    )
    session_timeout_seconds = _positive_integer(
        environment,
        "ALX_CODING_SESSION_TIMEOUT_SECONDS",
        DEFAULT_CODING_SESSION_TIMEOUT_SECONDS,
    )
    if not enabled:
        return CodingSettings(False, absent, absent, session_timeout_seconds)
    provider = (
        environment.get("ALX_CODING_PROVIDER", GROK_SUBSCRIPTION_PROVIDER)
        .strip()
        .lower()
        or GROK_SUBSCRIPTION_PROVIDER
    )
    if provider == NO_PROVIDER:
        return CodingSettings(True, absent, absent, session_timeout_seconds)
    if provider not in (GROK_SUBSCRIPTION_PROVIDER, CLAUDE_SUBSCRIPTION_PROVIDER, "openai"):
        raise ConfigurationError(
            f"coding provider adapter is not installed: {provider}"
        )
    if provider == CLAUDE_SUBSCRIPTION_PROVIDER:
        if environment.get("ALX_CODING_API_KEY", "").strip():
            raise ConfigurationError(
                "ALX_CODING_API_KEY must not be set for claude_subscription"
            )
        return CodingSettings(True, ReasoningSettings(
            provider=provider,
            model=_required(environment, "ALX_CODING_MODEL"),
            api_key="", base_url="",
            timeout_seconds=_positive_integer(environment, "ALX_CODING_TIMEOUT_SECONDS", 120),
            streaming=False, service_tier="default", effort="medium",
        ), _coding_reviewer_settings(environment), session_timeout_seconds)
    if provider == "openai":
        return CodingSettings(True, ReasoningSettings(
            provider=provider,
            model=_required(environment, "ALX_CODING_MODEL"),
            api_key=_credential(environment, "ALX_CODING_API_KEY", "OPENAI_API_KEY"),
            base_url=_configured(environment, "ALX_CODING_BASE_URL", "OPENAI_BASE_URL", "https://api.openai.com").rstrip("/"),
            timeout_seconds=_positive_integer(environment, "ALX_CODING_TIMEOUT_SECONDS", 120),
            streaming=False,
            service_tier=environment.get("ALX_CODING_SERVICE_TIER", "default").strip().lower(),
            effort=_coding_effort(environment),
        ), _coding_reviewer_settings(environment), session_timeout_seconds)
    return CodingSettings(
        True,
        ReasoningSettings(
            provider=GROK_SUBSCRIPTION_PROVIDER,
            model=environment.get("ALX_CODING_MODEL", "grok-4.6").strip()
            or "grok-4.6",
            api_key="",
            base_url="",
            timeout_seconds=_positive_integer(
                environment, "ALX_CODING_TIMEOUT_SECONDS", 120
            ),
            streaming=False,
            service_tier="default",
            effort=_coding_effort(environment),
        ),
        _coding_reviewer_settings(environment),
        session_timeout_seconds,
    )


def _coding_effort(environment: Mapping[str, str], name: str = "ALX_CODING_EFFORT") -> str:
    effort = environment.get(name, "medium").strip().lower()
    if effort not in ("none", "low", "medium", "high", "xhigh", "max"):
        raise ConfigurationError(
            f"{name} must be none, low, medium, high, xhigh, or max"
        )
    return effort


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
    # D-024a experiment: the Core that answers an autonomous turn. None when
    # unconfigured, which disables the experiment entirely.
    autonomous: "ReasoningSettings | None"
    # D-028 coding jobs. Independent of the conversational Core.
    coding: "CodingSettings"
    speech_to_text: SpeechToTextSettings
    text_to_speech: TextToSpeechSettings

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RuntimeSettings:
        reasoning_provider = _required(environment, "ALX_REASONING_PROVIDER")
        # The subscription path authenticates through the Claude Code
        # installation, so it has no key and no base URL to configure. Asking
        # for either would either refuse a correct configuration or invite a
        # key into the one path whose purpose is not to use one.
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
            reasoning=_core_reasoning_settings(
                environment,
                reasoning_provider,
                provider_key_name,
                provider_base_name,
                provider_base_fallback,
            ),
            specialist=_specialist_settings(environment, reasoning_provider),
            research=_research_settings(environment, reasoning_provider),
            autonomous=autonomous_reasoning_settings(environment),
            coding=_coding_settings(environment),
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


# The exact price D-025 records for the Brave Search API. Configuration must
# match it: an unverified rate charged against a ceiling sized for another one
# is not a ceiling, and D-025 requires search to fail closed rather than run at
# a price nobody approved.
APPROVED_SEARCH_USD_PER_REQUEST = 0.005


@dataclass(frozen=True, slots=True)
class WebSearchSettings:
    """Paid public web search, off until every field is configured and valid."""

    enabled: bool
    api_key: str
    usd_per_request: float
    daily_requests: int
    daily_usd: float

    @property
    def is_usable(self) -> bool:
        """Whether production search may be registered at all.

        Every condition is required. There is no degraded mode: a search that
        ran without accounting would spend against a ceiling nobody measures,
        and a fallback provider would be a second production path.
        """
        return bool(
            self.enabled
            and self.api_key.strip()
            and self.usd_per_request == APPROVED_SEARCH_USD_PER_REQUEST
            and self.daily_requests > 0
            and self.daily_usd > 0
            and self.daily_usd >= self.usd_per_request
        )


def _web_search_settings(environment: Mapping[str, str]) -> WebSearchSettings:
    """Read search configuration, defaulting to off and unpriced."""
    return WebSearchSettings(
        enabled=_boolean(environment, "ALX_WEB_SEARCH_ENABLED", False),
        api_key=environment.get("BRAVE_SEARCH_API_KEY", "").strip(),
        usd_per_request=_number_in_range(
            environment, "BRAVE_SEARCH_USD_PER_REQUEST", 0.0, 1.0, 0.0
        ),
        daily_requests=_integer_in_range(
            environment, "BRAVE_SEARCH_DAILY_REQUESTS", 0, 10_000
        )
        if environment.get("BRAVE_SEARCH_DAILY_REQUESTS")
        else 0,
        daily_usd=_number_in_range(
            environment, "BRAVE_SEARCH_DAILY_USD", 0.0, 100.0, 0.0
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
    # D-025 public web reading. Off until a runtime is told it may read, so a
    # runtime that has never been configured for the web has no such
    # capability registered at all.
    web_read_enabled: bool
    # D-025 paid public web search. Separate from web_read_enabled: reading a
    # URL costs nothing, searching costs money, so a runtime may be authorised
    # to read without being authorised to spend on discovery.
    web_search: "WebSearchSettings"
    sandbox: "SandboxSettings"

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
            web_read_enabled=_boolean(environment, "ALX_WEB_READ_ENABLED", False),
            web_search=_web_search_settings(environment),
            sandbox=sandbox_settings(environment),
        )


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """Isolated experimentation, off until it is configured.

    The storage root is deliberately separate from the runtime storage root.
    That directory holds goals, memories, continuity and a private key; a
    sandbox workspace has no business sharing a parent with any of them.
    """

    enabled: bool
    storage_root: Path | None
    daily_runs: int
    daily_wall_seconds: int

    @property
    def is_usable(self) -> bool:
        return bool(
            self.enabled
            and self.storage_root is not None
            and self.daily_runs > 0
            and self.daily_wall_seconds > 0
        )

    @property
    def workspace_root(self) -> Path | None:
        return None if self.storage_root is None else self.storage_root / "workspaces"

    @property
    def ledger_path(self) -> Path | None:
        return None if self.storage_root is None else self.storage_root / "sandbox-runs.sqlite3"


# Mirrors the D-027 ceilings. Duplicated as literals because `config` is a
# leaf boundary that imports nothing internal; a test asserts the two stay
# equal so a drift cannot pass unnoticed.
_SANDBOX_DAILY_RUNS = 20
_SANDBOX_DAILY_WALL_SECONDS = 300


def sandbox_settings(environment: Mapping[str, str]) -> SandboxSettings:
    """Read sandbox configuration, defaulting to off."""
    root = environment.get("ALX_SANDBOX_ROOT", "").strip()
    # Configuration may lower a ceiling but never raise it. D-027 records 20
    # runs and 300 wall seconds as approved maxima, so an environment value
    # above them is clamped rather than honoured: a governed limit that an
    # operator could raise by setting a variable is not a limit.
    return SandboxSettings(
        enabled=_boolean(environment, "ALX_SANDBOX_ENABLED", False),
        storage_root=Path(root).expanduser() if root else None,
        daily_runs=min(
            _positive_integer(environment, "ALX_SANDBOX_DAILY_RUNS", _SANDBOX_DAILY_RUNS),
            _SANDBOX_DAILY_RUNS,
        ),
        daily_wall_seconds=min(
            _positive_integer(
                environment, "ALX_SANDBOX_DAILY_WALL_SECONDS", _SANDBOX_DAILY_WALL_SECONDS
            ),
            _SANDBOX_DAILY_WALL_SECONDS,
        ),
    )


@dataclass(frozen=True, slots=True)
class MergeSettings:
    """Delegated merge authority, off until it is configured.

    Friedl grants this authority by configuring it and revokes it by removing
    the configuration. There is no per-merge approval: the delegation is the
    decision, and each merge is AL/X exercising it.
    """

    enabled: bool
    repository: str
    token: str

    @property
    def is_usable(self) -> bool:
        return bool(self.enabled and self.repository.strip() and self.token.strip())


def merge_settings(environment: Mapping[str, str]) -> MergeSettings:
    """Read merge configuration, defaulting to off."""
    return MergeSettings(
        enabled=_boolean(environment, "ALX_MERGE_ENABLED", False),
        repository=environment.get("ALX_MERGE_REPOSITORY", "").strip(),
        token=environment.get("GITHUB_TOKEN", "").strip(),
    )


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    """Requesting an external review, off until it is configured."""

    enabled: bool
    repository: str
    token: str

    @property
    def is_usable(self) -> bool:
        return bool(self.enabled and self.repository.strip() and self.token.strip())


def review_settings(environment: Mapping[str, str]) -> ReviewSettings:
    """Read review-request configuration, defaulting to off."""
    return ReviewSettings(
        enabled=_boolean(environment, "ALX_REVIEW_REQUEST_ENABLED", False),
        repository=environment.get("ALX_MERGE_REPOSITORY", "").strip(),
        token=environment.get("GITHUB_TOKEN", "").strip(),
    )
