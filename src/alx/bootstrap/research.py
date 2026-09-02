"""Build only the research tiers this runtime is authorised to spend on.

A tier that is not enabled is not constructed. That is deliberately stronger
than making it unaffordable: every configured model currently fits the ceiling,
so price is not a permission. An unbuilt tier has no transport, so a question
sent to it is refused before any reservation exists.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alx.config import ConfigurationError, ResearchSettings
from alx.contracts import Cognition
from alx.observability import (
    ConfiguredPricingWorstCase,
    ResearchBudget,
    SQLiteResearchLedger,
)
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers import OpenAIReasoningModel, XAIReasoningModel
from alx.safety import AuthorityPolicy
from alx.specialists import ResearchSpecialist, ResearchTierModel
from alx.tools import ASK_RESEARCH_QUESTION, RESEARCH_DEFINITION, build_research_executors


LOGGER = logging.getLogger(__name__)

# The provider-side ceiling for one research request. The input side must hold
# a bounded question's material, instruction and schema; the output side is the
# smallest that still returns a structured answer. Both are enforced by the
# provider, and the reservation is the worst-case price of exactly this bound.
RESEARCH_MAX_INPUT_TOKENS = 8_000
RESEARCH_MAX_OUTPUT_TOKENS = 1_000

# Paid research is its own authority. Holding it does not follow from any other
# permission, so a runtime that never grants it cannot spend even if a tier is
# configured and priced.
RESEARCH_SPEND_PERMISSION = "research.spend"


def _transport(settings: Any, telemetry_sink: Any) -> Any:
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
        return XAIReasoningModel(
            settings.model,
            settings.api_key,
            settings.base_url,
            settings.timeout_seconds,
            streaming=settings.streaming,
            service_tier=settings.service_tier,
            telemetry_sink=telemetry_sink,
        )
    raise ConfigurationError(
        f"research provider adapter is not installed: {settings.provider}"
    )


def build_research_specialist(
    settings: ResearchSettings,
    storage_root: Path,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> ResearchSpecialist | None:
    """Compose prepaid research, or None when no tier is authorised.

    Returning None disables research rather than falling back to anything. A
    runtime with no enabled tier, or with a budget of zero, does not research;
    it does not quietly research more cheaply.
    """
    if not settings.enabled_tiers:
        LOGGER.info("Research is disabled: no cognition tier is enabled")
        return None
    if settings.limits.daily_usd <= 0 or settings.limits.per_request_max_usd <= 0:
        LOGGER.info("Research is disabled: no spending budget is configured")
        return None

    by_name = {
        "survey": (Cognition.SURVEY, settings.survey),
        "compare": (Cognition.COMPARE, settings.compare),
        "judge": (Cognition.JUDGE, settings.judge),
    }
    tiers: dict[Cognition, ResearchTierModel] = {}
    for name in sorted(settings.enabled_tiers):
        tier, tier_settings = by_name[name]
        tiers[tier] = ResearchTierModel(
            tier_settings.provider,
            tier_settings.model,
            _transport(tier_settings, telemetry_sink),
        )
        LOGGER.info(
            "Research tier %s enabled: %s %s",
            name,
            tier_settings.provider,
            tier_settings.model,
        )
    for name in sorted(set(by_name) - settings.enabled_tiers):
        LOGGER.info("Research tier %s is not enabled and is not built", name)

    ledger = SQLiteResearchLedger(
        storage_root / "research-spend.sqlite3",
        ResearchBudget(
            daily_usd=settings.limits.daily_usd,
            per_request_max_usd=settings.limits.per_request_max_usd,
        ),
    )
    return ResearchSpecialist(
        tiers,
        ledger,
        ConfiguredPricingWorstCase(),
        RESEARCH_MAX_INPUT_TOKENS,
        RESEARCH_MAX_OUTPUT_TOKENS,
        settings.limits.per_request_max_usd,
        telemetry_sink=telemetry_sink,
    )


@dataclass(frozen=True, slots=True)
class ResearchRuntime:
    """The one research capability, or nothing at all."""

    specialist: ResearchSpecialist
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_research_runtime(
    settings: ResearchSettings,
    storage_root: Path,
    call_id_source: Callable[[], str],
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> ResearchRuntime | None:
    """Compose research as a capability, or None when it is not authorised.

    Returning None leaves the capability unregistered, so AL/X cannot propose a
    research call at all. That is the difference between research being off and
    research being merely unaffordable.
    """
    specialist = build_research_specialist(settings, storage_root, telemetry_sink)
    if specialist is None:
        return None
    return ResearchRuntime(
        specialist=specialist,
        definitions=(RESEARCH_DEFINITION,),
        policies={
            # Spending is consequential and irreversible once the provider has
            # been called, so it carries its own permission. It is not approval
            # gated: the budget, the bound and the ceiling are the control, and
            # asking Friedl to approve each question would make research
            # something he directs rather than something she pursues.
            ASK_RESEARCH_QUESTION: AuthorityPolicy(
                frozenset({RESEARCH_SPEND_PERMISSION})
            ),
        },
        executors=build_research_executors(specialist, call_id_source),
        permissions=frozenset({RESEARCH_SPEND_PERMISSION}),
    )
