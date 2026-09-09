"""Validated runtime configuration without behavioural authority."""

from alx.config.settings import (
    WebSearchSettings,
    AUTONOMOUS_MAX_INPUT_TOKENS,
    AUTONOMOUS_MAX_OUTPUT_TOKENS,
    autonomous_cognition_daily_budget_usd,
    autonomous_commissioning_limit,
    autonomous_due_check_seconds,
    autonomous_reasoning_settings,
    CodingSettings,
    CLAUDE_SUBSCRIPTION_PROVIDER,
    GROK_SUBSCRIPTION_PROVIDER,
    ConfigurationError,
    LiveVoiceSettings,
    MailSendSettings,
    MailSettings,
    ReasoningSettings,
    ResearchLimits,
    ResearchSettings,
    RuntimeSettings,
    SpeechToTextSettings,
    TextToSpeechSettings,
    XeroSettings,
)

from alx.config.settings import SandboxSettings, sandbox_settings
from alx.config.settings import MergeSettings, merge_settings
from alx.config.settings import ReviewSettings, review_settings

__all__ = [
    "ReviewSettings",
    "review_settings",
    "MergeSettings",
    "merge_settings",
    "WebSearchSettings",
    "AUTONOMOUS_MAX_INPUT_TOKENS",
    "AUTONOMOUS_MAX_OUTPUT_TOKENS",
    "autonomous_cognition_daily_budget_usd",
    "autonomous_commissioning_limit",
    "autonomous_due_check_seconds",
    "autonomous_reasoning_settings",
    "CLAUDE_SUBSCRIPTION_PROVIDER",
    "GROK_SUBSCRIPTION_PROVIDER",
    "CodingSettings",
    "ConfigurationError",
    "LiveVoiceSettings",
    "MailSendSettings",
    "MailSettings",
    "ReasoningSettings",
    "ResearchLimits",
    "ResearchSettings",
    "RuntimeSettings",
    "SandboxSettings",
    "sandbox_settings",
    "SpeechToTextSettings",
    "TextToSpeechSettings",
    "XeroSettings",
]
