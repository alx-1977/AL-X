"""Brave Search discovery for D-025: candidate pages, never conclusions.

This adapter asks Brave's **web search** endpoint for candidate pages and
returns them in the order Brave gave them. It does not use Brave Answers or
any summarised endpoint: an answer endpoint returns an external model's
conclusion, which would put a second system between the sources and AL/X.

Nothing here ranks, scores, filters, prefers or drops a result on what it
appears to be about. Provider order is preserved exactly, because that order
is a mechanical fact about Brave rather than a judgement this code adopts.
The subject AL/X composed is sent as she wrote it — expanding or rewriting it
would be code deciding what she meant.

The reservation happens before dispatch, in the executor that owns the ledger,
so a request is never sent that the day cannot pay for.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from alx.contracts.web import (
    MAX_AGE_CHARACTERS,
    MAX_SEARCH_RESULTS,
    MAX_SNIPPET_CHARACTERS,
    MAX_SUBJECT_CHARACTERS,
    MAX_TITLE_CHARACTERS,
    MAX_URL_CHARACTERS,
    SEARCH_AUTH_FAILED,
    SEARCH_NO_RESULTS,
    SEARCH_PROVIDER_FAILED,
    SEARCH_RATE_LIMITED,
    SEARCH_TIMEOUT,
    WebSearchError,
    WebSearchResult,
    WebSearchResults,
)


BRAVE_PROVIDER = "brave"
BRAVE_BASE_URL = "https://api.search.brave.com"
BRAVE_SEARCH_PATH = "/res/v1/web/search"

# Brave's own price snapshot recorded in D-025. Configuration must match it, so
# a silently changed rate cannot be charged against a ceiling sized for another.
BRAVE_USD_PER_REQUEST = 0.005


def _text(value: Any, ceiling: int) -> str:
    """Provider text, cut to its bound. Never trusted, never interpreted."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:ceiling]


def _strip_markup(value: str) -> str:
    """Remove the <strong> emphasis Brave wraps around matched terms.

    Purely mechanical: the tags are presentation, and leaving them would put
    markup into evidence. Nothing about the words themselves is inspected.
    """
    out: list[str] = []
    depth = 0
    for character in value:
        if character == "<":
            depth += 1
        elif character == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(character)
    return "".join(out)


class BraveWebSearchProvider:
    """The one production path from a search subject to candidate pages."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BRAVE_BASE_URL,
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Brave search requires an API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    def search(self, subject: str, max_results: int) -> WebSearchResults:
        """Run exactly one Brave web search and return its candidates in order."""
        wording = subject.strip()
        if not wording:
            raise WebSearchError(SEARCH_PROVIDER_FAILED, "empty subject")
        count = max(1, min(int(max_results), MAX_SEARCH_RESULTS))

        try:
            response = self._client.get(
                f"{self._base_url}{BRAVE_SEARCH_PATH}",
                params={
                    # Sent exactly as AL/X composed it, cut only to the
                    # transport bound.
                    "q": wording[:MAX_SUBJECT_CHARACTERS],
                    "count": count,
                    # Discovery only. Brave's summarised answer is explicitly
                    # excluded by D-025, so it is never requested.
                    "result_filter": "web",
                },
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self._api_key,
                },
            )
        except httpx.TimeoutException:
            raise WebSearchError(SEARCH_TIMEOUT, "search timed out") from None
        except httpx.HTTPError as error:
            raise WebSearchError(
                SEARCH_PROVIDER_FAILED, type(error).__name__
            ) from None

        status = response.status_code
        if status in (401, 403):
            raise WebSearchError(SEARCH_AUTH_FAILED, f"status {status}")
        if status == 429:
            raise WebSearchError(SEARCH_RATE_LIMITED, f"status {status}")
        if status >= 400:
            raise WebSearchError(SEARCH_PROVIDER_FAILED, f"status {status}")

        try:
            body = response.json()
        except (ValueError, TypeError):
            raise WebSearchError(SEARCH_PROVIDER_FAILED, "unreadable body") from None
        if not isinstance(body, dict):
            raise WebSearchError(SEARCH_PROVIDER_FAILED, "unexpected body")

        web = body.get("web")
        raw = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw, list):
            raw = []

        results: list[WebSearchResult] = []
        for item in raw:
            candidate = self._candidate(item)
            if candidate is not None:
                results.append(candidate)
            if len(results) == count:
                break

        if not results:
            raise WebSearchError(SEARCH_NO_RESULTS, "no candidates returned")

        return WebSearchResults(
            retrieved_at=self._now(),
            # Provider order, untouched. Sorting or promoting anything here
            # would make this code decide which source matters.
            results=tuple(results),
        )

    @staticmethod
    def _candidate(item: Any) -> WebSearchResult | None:
        """One provider row, bounded. Unusable rows are dropped, not repaired."""
        if not isinstance(item, dict):
            return None
        url = _text(item.get("url"), MAX_URL_CHARACTERS)
        if not url:
            return None
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            # A candidate AL/X could not hand to ask_web_page is not a
            # candidate. Dropping it is mechanical, not a judgement.
            return None
        snippet = _strip_markup(_text(item.get("description"), MAX_SNIPPET_CHARACTERS * 4))
        return WebSearchResult(
            url=url,
            title=_strip_markup(_text(item.get("title"), MAX_TITLE_CHARACTERS * 4))[
                :MAX_TITLE_CHARACTERS
            ],
            snippet=snippet[:MAX_SNIPPET_CHARACTERS],
            source_domain=parts.hostname.rstrip(".").lower()[:MAX_URL_CHARACTERS],
            age=(_text(item.get("age"), MAX_AGE_CHARACTERS) or None),
        )

    def close(self) -> None:
        self._client.close()


__all__ = [
    "BRAVE_BASE_URL",
    "BRAVE_PROVIDER",
    "BRAVE_SEARCH_PATH",
    "BRAVE_USD_PER_REQUEST",
    "BraveWebSearchProvider",
]
