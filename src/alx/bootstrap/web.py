"""Compose public web reading, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot propose a
page read at all. That is the difference between web access being off and web
access merely failing: an unregistered capability is honestly absent, while a
registered one that always fails would look like a broken world rather than a
runtime that was never given the authority.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pathlib import Path

from alx.config import WebSearchSettings
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.observability import SearchBudget, SQLiteSearchLedger
from alx.providers import BraveWebSearchProvider, HttpWebFetchProvider
from alx.providers.web_search import BRAVE_PROVIDER
from alx.safety import AuthorityPolicy
from alx.tools import (
    ASK_WEB_PAGE,
    ASK_WEB_SEARCH,
    WEB_DEFINITION,
    WEB_SEARCH_DEFINITION,
    build_web_executors,
    build_web_search_executors,
)


LOGGER = logging.getLogger(__name__)

# Reading the public web is its own authority under D-025. Holding it does not
# follow from any other permission: research.spend buys model tokens and grants
# no network access, and this grants no model spend and no authenticated
# browsing.
WEB_READ_PERMISSION = "web.read"


@dataclass(frozen=True, slots=True)
class WebRuntime:
    """The one public-web capability, or nothing at all."""

    provider: Any
    searcher: Any
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_web_runtime(
    enabled: bool,
    call_id_source: Callable[[], str],
    search_settings: WebSearchSettings | None = None,
    storage_root: Path | None = None,
) -> WebRuntime | None:
    """Compose public web reading, and search when it is separately authorised.

    Reading and searching share one authority — both are the public,
    unauthenticated, read-only access D-025 grants — so both carry
    `web.read`. They are separate capabilities because they are separate
    outcomes, not separate permissions.
    """
    if not enabled:
        LOGGER.info("Public web reading is not enabled: no web capability")
        return None
    provider = HttpWebFetchProvider()
    definitions: list[CapabilityDefinition] = [WEB_DEFINITION]
    policies = {
        # Not approval gated. The network boundary, the resource bounds
        # and the read-only method are the control; asking Friedl to
        # approve each page would make reading something he directs
        # rather than something she does while thinking.
        ASK_WEB_PAGE: AuthorityPolicy(frozenset({WEB_READ_PERMISSION})),
    }
    executors = dict(build_web_executors(provider, call_id_source))
    LOGGER.info("Public web reading enabled: %s", ASK_WEB_PAGE)

    searcher = _search_runtime(
        search_settings, storage_root, call_id_source, definitions, policies, executors
    )
    return WebRuntime(
        provider=provider,
        searcher=searcher,
        definitions=tuple(definitions),
        policies=policies,
        executors=executors,
        permissions=frozenset({WEB_READ_PERMISSION}),
    )


def _search_runtime(
    settings: WebSearchSettings | None,
    storage_root: Path | None,
    call_id_source: Callable[[], str],
    definitions: list[CapabilityDefinition],
    policies: dict,
    executors: dict,
) -> Any:
    """Register paid search only when every requirement holds.

    Anything missing leaves the capability unregistered, so AL/X cannot
    propose a search at all. There is no fallback provider and no unaccounted
    mode: a search that ran without a working ledger would spend against a
    ceiling nobody is measuring.
    """
    if settings is None or not settings.is_usable:
        LOGGER.info("Public web search is not enabled: %s", ASK_WEB_SEARCH)
        return None
    if storage_root is None:
        LOGGER.info("Public web search has nowhere to record spend; not enabled")
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
    definitions.append(WEB_SEARCH_DEFINITION)
    policies[ASK_WEB_SEARCH] = AuthorityPolicy(frozenset({WEB_READ_PERMISSION}))
    executors.update(
        build_web_search_executors(
            searcher, ledger, call_id_source, BRAVE_PROVIDER
        )
    )
    LOGGER.info(
        "Public web search enabled: %s (%d requests, %.4f USD per day)",
        ASK_WEB_SEARCH, settings.daily_requests, settings.daily_usd,
    )
    return searcher


__all__ = ["WEB_READ_PERMISSION", "WebRuntime", "build_web_runtime"]
