"""Compose the one `research` capability: web, paid questions and notebook (D-074).

Each job is an action, offered only when this runtime is authorised for it.
Reading the public web carries `web.read` (D-025), paid questions
`research.spend`, and the notebook `research.notebook`; none follows from
another. Only Friedl's correction of a notebook entry needs his approval.

Only the research tiers this runtime is authorised to spend on are built.
A tier that is not enabled is not constructed. That is deliberately stronger
than making it unaffordable: every configured model currently fits the ceiling,
so price is not a permission. An unbuilt tier has no transport, so a question
sent to it is refused before any reservation exists.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alx.config import ConfigurationError, ResearchSettings, WebSearchSettings
from alx.contracts import Cognition, RESEARCH, ResearchAction
from alx.observability import (
    ConfiguredPricingWorstCase,
    ResearchBudget,
    SearchBudget,
    SQLiteResearchLedger,
    SQLiteSearchLedger,
)
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers import (
    BraveWebSearchProvider,
    ClaudeSubscriptionResearchModel,
    HttpWebFetchProvider,
    OpenAIReasoningModel,
    XAIReasoningModel,
)
from alx.providers.web_search import BRAVE_PROVIDER
from alx.research import SQLiteResearchStore
from alx.safety import AuthorityPolicy
from alx.specialists import ResearchSpecialist, ResearchTierModel
from alx.tools.notebook import build_notebook_executors
from alx.tools.research import (
    build_model_question,
    build_research_executors,
    offered_research_actions,
    research_definition,
)
from alx.tools.web import build_page_reader, build_web_searcher


LOGGER = logging.getLogger(__name__)

# The provider-side ceiling for one research request. The input side must hold
# a bounded question's material, instruction and schema; the output side is the
# smallest that still returns a structured answer. Both are enforced by the
# provider, and the reservation is the worst-case price of exactly this bound.
RESEARCH_MAX_INPUT_TOKENS = 8_000
RESEARCH_MAX_OUTPUT_TOKENS = 4_000

# Paid research is its own authority. Holding it does not follow from any other
# permission, so a runtime that never grants it cannot spend even if a tier is
# configured and priced.
RESEARCH_SPEND_PERMISSION = "research.spend"
# Reading the public web is its own authority under D-025: it buys no model
# tokens and no authenticated browsing, and research.spend reaches no network.
WEB_READ_PERMISSION = "web.read"
NOTEBOOK_PERMISSION = "research.notebook"


def _transport(
    settings: Any, telemetry_sink: Any, claude_identity: tuple[str, str] | None = None,
    claude_oauth_token: str | None = None,
) -> Any:
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
    if settings.provider == "claude_subscription":
        # Her own Claude subscription (D-023, amended 2026-10-14).
        if claude_identity is None:
            raise ConfigurationError(
                "research on the Claude subscription requires AL/X's own Claude login"
            )
        return ClaudeSubscriptionResearchModel(
            settings.model,
            settings.timeout_seconds,
            telemetry_sink=telemetry_sink,
            config_dir=claude_identity[0],
            expected_account=claude_identity[1],
            oauth_token=claude_oauth_token,
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
            _transport(tier_settings, telemetry_sink, settings.claude_identity,
                       settings.claude_oauth_token),
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
            daily_calls=settings.limits.daily_calls,
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
    """The one `research` capability and what it holds open."""

    store: SQLiteResearchStore
    fetcher: Any
    searcher: Any
    specialist: ResearchSpecialist | None
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]
    offered: tuple[ResearchAction, ...]

    def close(self) -> None:
        self.store.close()
        if self.fetcher is not None:
            self.fetcher.close()
        if self.searcher is not None:
            self.searcher.close()


def build_research_runtime(
    settings: ResearchSettings,
    storage_root: Path,
    call_id_source: Callable[[], str],
    *,
    retention_days: int,
    web_read_enabled: bool,
    search_settings: WebSearchSettings | None = None,
    provenance_of: Callable[[tuple[str, ...], datetime], Any] | None = None,
    telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> ResearchRuntime:
    """Compose `research` with every action this runtime is authorised for.

    The notebook is always there. A page read needs web reading enabled, a
    search its provider and spend ledger, and a paid question an enabled tier
    and budget. An action without its authority is not offered at all, which
    is the difference between it being off and merely failing.
    """
    store = SQLiteResearchStore(storage_root / "research-notebook.sqlite3")
    actions: dict[ResearchAction, Callable[[StructuredData], CapabilityResult]] = dict(
        build_notebook_executors(store, retention_days, call_id_source,
                                 provenance_of=provenance_of)
    )
    permission_of = {action: NOTEBOOK_PERMISSION for action in actions}

    specialist = build_research_specialist(settings, storage_root, telemetry_sink)
    if specialist is not None:
        # Spending is consequential and irreversible once the provider has been
        # called, so it carries its own permission. It is not approval gated:
        # the budget, the bound and the ceiling are the control.
        actions[ResearchAction.ASK_MODEL] = build_model_question(specialist, call_id_source)
        permission_of[ResearchAction.ASK_MODEL] = RESEARCH_SPEND_PERMISSION
    else:
        LOGGER.info("Research is not enabled: no paid question")

    fetcher = searcher = None
    if web_read_enabled:
        # Not approval gated. The network boundary, the resource bounds and
        # the read-only method are the control.
        fetcher = HttpWebFetchProvider()
        actions[ResearchAction.READ_PAGE] = build_page_reader(fetcher, call_id_source)
        permission_of[ResearchAction.READ_PAGE] = WEB_READ_PERMISSION
        searcher = _searcher(search_settings, storage_root, call_id_source, actions)
        if searcher is not None:
            permission_of[ResearchAction.SEARCH_WEB] = WEB_READ_PERMISSION
    else:
        LOGGER.info("Public web reading is not enabled: no page read or search")

    offered = offered_research_actions(
        web=ResearchAction.READ_PAGE in actions,
        search=ResearchAction.SEARCH_WEB in actions,
        model=ResearchAction.ASK_MODEL in actions,
    )
    permissions = frozenset(permission_of[item] for item in offered)
    LOGGER.info("Research composed: %s", ", ".join(item.value for item in offered))
    return ResearchRuntime(
        store=store,
        fetcher=fetcher,
        searcher=searcher,
        specialist=specialist,
        definitions=(research_definition(offered),),
        policies={
            RESEARCH: AuthorityPolicy(
                permissions,
                # Friedl's corrections stay his to approve; her own notebook,
                # deleting included, is hers (D-051).
                approval_required=True,
                approval_exempt_when=tuple(
                    (("action", item.value),) for item in offered
                    if item is not ResearchAction.CORRECT_ENTRY
                ),
            ),
        },
        executors=build_research_executors(actions, call_id_source),
        permissions=permissions,
        offered=offered,
    )


def _searcher(
    settings: WebSearchSettings | None,
    storage_root: Path,
    call_id_source: Callable[[], str],
    actions: dict,
) -> Any:
    """Offer paid search only when every requirement holds.

    There is no fallback provider and no unaccounted mode: a search that ran
    without a working ledger would spend against a ceiling nobody is measuring.
    """
    if settings is None or not settings.is_usable:
        LOGGER.info("Public web search is not enabled")
        return None
    try:
        ledger = SQLiteSearchLedger(
            storage_root / "search-spend.sqlite3",
            SearchBudget(
                daily_usd=settings.daily_usd,
                daily_requests=settings.daily_requests,
                usd_per_request=settings.usd_per_request,
            ),
        )
    except Exception as error:
        LOGGER.info("Public web search ledger unavailable, not enabled: %s", error)
        return None
    searcher = BraveWebSearchProvider(settings.api_key)
    actions[ResearchAction.SEARCH_WEB] = build_web_searcher(
        searcher, ledger, call_id_source, BRAVE_PROVIDER
    )
    LOGGER.info(
        "Public web search enabled (%d requests, %.4f USD per day)",
        settings.daily_requests, settings.daily_usd,
    )
    return searcher


__all__ = [
    "NOTEBOOK_PERMISSION",
    "RESEARCH_SPEND_PERMISSION",
    "ResearchRuntime",
    "WEB_READ_PERMISSION",
    "build_research_runtime",
    "build_research_specialist",
]
