"""One language-blind primitive for reading a public web page, under D-025.

Retrieval is reached the way every other capability is: AL/X proposes a
structured call, the broker validates it, the safety gate authorises it under
`web.read`, and the executor runs it. The capability retrieves one page and
reports what it found. It does not decide whether the page is any good,
whether its claims are true, whether it answers the question, or whether
anything should be recorded. Those judgements are hers.

What comes back is evidence, not instruction. The Core already presents a
capability result as `external_untrusted_data`, so text inside a page travels
on the evidence channel and never becomes a second instruction channel. That
protection is structural: nothing here scans a page for what it appears to be
asking for, because deciding what text is really trying to do is exactly the
semantic judgement Law 1 keeps in the Core.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    DEFAULT_SEARCH_RESULTS,
    MAX_SEARCH_RESULTS,
    MAX_SUBJECT_CHARACTERS,
    WEB_SEARCH_FAILURES,
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    RetentionPolicy,
    SideEffect,
    StructuredSchema,
    ValueKind,
    WEB_FETCH_FAILURES,
    MAX_EXTRACTED_CHARACTERS,
)


ASK_WEB_PAGE = "ask_web_page"
ASK_WEB_SEARCH = "ask_web_search"

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)

# "arguments_unusable" joins the declared retrieval refusals: a malformed call
# is a different fact from a page that would not load.
_FAILURES = ("arguments_unusable", *WEB_FETCH_FAILURES)

DEFINITION = CapabilityDefinition(
    ASK_WEB_PAGE,
    "Retrieve one public web page by exact URL and return its readable text "
    "with the source URL, title and retrieval time. Reads only; follows no "
    "search, logs in to nothing, and judges nothing about what it finds.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "page_id": _STRING,
            "url": _STRING,
            "max_characters": _INTEGER,
        },
        ("page_id", "url"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "requested_url": _STRING,
            "final_url": _STRING,
            "source_domain": _STRING,
            "retrieved_at": _STRING,
            "http_status": _INTEGER,
            "content": _STRING,
            # Present only when the page was longer than the bound allowed.
            # Its presence says the text was read in part; what that is worth
            # is AL/X's judgement.
            "content_omitted_characters": _INTEGER,
            "title": _STRING,
            "publisher": _STRING,
            # The publisher's own claim about this page's address, reported
            # beside the fetched URL and never in place of it.
            "canonical_url": _STRING,
        },
        (
            "requested_url",
            "final_url",
            "source_domain",
            "retrieved_at",
            "http_status",
            "content",
        ),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
    # The page body is deliberately not durable. Goal state carries the
    # identity of what was read so a restart can still cite it; the text
    # itself belongs to the turn that reasoned over it, and persisting every
    # retrieved page would make the goal store a second evidence store.
    durable_input_fields=("page_id", "url"),
)


def build_web_executors(
    fetcher: Any,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Any], CapabilityResult]]:
    """Bind the one page-read primitive to the one fetch provider."""

    def read_page(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            url = str(arguments["url"])
            requested_characters = arguments.get(
                "max_characters", MAX_EXTRACTED_CHARACTERS
            )
            if isinstance(requested_characters, bool):
                raise TypeError("max_characters must be an integer")
            bound = int(requested_characters)
            if bound <= 0:
                raise ValueError("max_characters must be positive")
        except (KeyError, TypeError, ValueError):
            return CapabilityResult(
                call_id,
                ASK_WEB_PAGE,
                CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )

        try:
            page = fetcher.fetch(url, bound)
        except Exception as error:
            # A declared refusal keeps its own code so AL/X can tell a blocked
            # page from one that does not exist. Anything undeclared becomes
            # provider_failed rather than leaking an exception's wording.
            code = getattr(error, "code", None)
            return CapabilityResult(
                call_id,
                ASK_WEB_PAGE,
                CapabilityResultState.FAILED,
                failure={
                    "code": code if code in WEB_FETCH_FAILURES else "provider_failed"
                },
            )

        values: dict[str, Any] = {
            "requested_url": page.requested_url,
            "final_url": page.final_url,
            "source_domain": page.source_domain,
            "retrieved_at": page.retrieved_at.isoformat(),
            "http_status": page.http_status,
            "content": page.content,
        }
        if page.title:
            values["title"] = page.title
        if page.publisher:
            values["publisher"] = page.publisher
        if page.canonical_url:
            values["canonical_url"] = page.canonical_url
        if page.content_omitted_characters > 0:
            values["content_omitted_characters"] = page.content_omitted_characters

        return CapabilityResult(
            call_id,
            ASK_WEB_PAGE,
            CapabilityResultState.SUCCEEDED,
            values,
            # Metadata only. The retrieval stays citable across a restart
            # through attempt:<call_id> without the page body ever entering
            # durable goal state.
            durable_values={
                key: values[key]
                for key in (
                    "requested_url",
                    "final_url",
                    "source_domain",
                    "retrieved_at",
                    "http_status",
                    "title",
                    "publisher",
                    "canonical_url",
                    "content_omitted_characters",
                )
                if key in values
            },
            # Web content is external and is not mail-derived, so it carries no
            # D-013 expiry.
            provenance=RetentionPolicy().non_mail(
                ContentOrigin.EXTERNAL, page.retrieved_at
            ),
        )

    return {ASK_WEB_PAGE: read_page}


_SEARCH_FAILURES = ("arguments_unusable", *WEB_SEARCH_FAILURES)

SEARCH_DEFINITION = CapabilityDefinition(
    ASK_WEB_SEARCH,
    "Search the public web for candidate pages matching a subject and return "
    "them in the provider's own order with their URLs, titles and snippets. "
    "Discovery only: it reads no page, follows nothing, and judges nothing "
    "about which candidate is worth reading.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "search_id": _STRING,
            # Language AL/X composed, carried to the provider exactly as she
            # wrote it. Nothing here reads it, rewrites it or classifies it.
            "subject": _STRING,
            "max_results": _INTEGER,
        },
        ("search_id", "subject"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "search_id": _STRING,
            "retrieved_at": _STRING,
            "result_count": _INTEGER,
            "results": StructuredSchema(
                ValueKind.ARRAY,
                items=StructuredSchema(
                    ValueKind.OBJECT,
                    {
                        "url": _STRING,
                        "title": _STRING,
                        "snippet": _STRING,
                        "source_domain": _STRING,
                        "age": _STRING,
                    },
                    ("url", "title", "snippet", "source_domain"),
                    extra_properties=False,
                ),
            ),
        },
        ("search_id", "retrieved_at", "result_count", "results"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _SEARCH_FAILURES,
    # The subject leaves this system and is read by the search provider, so it
    # is recorded: what was disclosed to a third party is a durable fact about
    # the day, not a transient detail. The candidate list is not durable; it
    # is a working set for the turn that chooses among it.
    durable_input_fields=("search_id", "subject"),
)


def build_web_search_executors(
    searcher: Any,
    ledger: Any,
    call_id_source: Callable[[], str],
    provider_name: str,
) -> Mapping[str, Callable[[Any], CapabilityResult]]:
    """Bind the one search primitive to one provider and one spend ledger."""

    def run_search(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()

        def failed(code: str) -> CapabilityResult:
            return CapabilityResult(
                call_id, ASK_WEB_SEARCH, CapabilityResultState.FAILED,
                failure={"code": code},
            )

        try:
            search_id = str(arguments["search_id"])
            subject = str(arguments["subject"])
            if not search_id.strip() or not subject.strip():
                raise ValueError("search_id and subject must not be blank")
            if len(subject) > MAX_SUBJECT_CHARACTERS:
                raise ValueError("subject exceeds its transport bound")
            requested = arguments.get("max_results", DEFAULT_SEARCH_RESULTS)
            if isinstance(requested, bool):
                raise TypeError("max_results must be an integer")
            bound = int(requested)
            if bound <= 0:
                raise ValueError("max_results must be positive")
        except (KeyError, TypeError, ValueError):
            return failed("arguments_unusable")
        bound = min(bound, MAX_SEARCH_RESULTS)

        # Money is withdrawn before the request exists. A dispatch that
        # preceded its reservation could spend past a ceiling and only
        # discover it afterwards, which is not a ceiling.
        try:
            reservation = ledger.reserve(provider_name)
        except Exception as error:
            # The ledger names its own refusals. An exhausted allowance is a
            # different fact from a ledger that could not account at all, and
            # both stop the request before it is sent.
            exhausted = type(error).__name__ == "SearchBudgetExceeded"
            return failed(
                "search_budget_exhausted" if exhausted else "search_unavailable"
            )

        try:
            found = searcher.search(subject, bound)
        except Exception as error:
            code = getattr(error, "code", None)
            # Conservative local accounting rule; provider billing semantics
            # not independently verified. Anything the provider answered is
            # treated as billable, including an error response, because a
            # refusal still consumed a request. Only a failure with no response
            # at all releases the reservation.
            no_response = code in ("search_timeout", "search_provider_failed") and (
                "timed out" in str(getattr(error, "detail", ""))
                or not str(getattr(error, "detail", "")).startswith("status")
            )
            if no_response:
                _release(ledger, reservation, str(code or "provider_failed"))
            else:
                _charge(ledger, reservation)
            return failed(code if code in WEB_SEARCH_FAILURES else "search_provider_failed")

        _charge(ledger, reservation)

        results = [
            {
                "url": item.url,
                "title": item.title,
                "snippet": item.snippet,
                "source_domain": item.source_domain,
                **({"age": item.age} if item.age else {}),
            }
            # Provider order preserved exactly.
            for item in found.results
        ]
        values: dict[str, Any] = {
            "search_id": search_id,
            "retrieved_at": found.retrieved_at.isoformat(),
            "result_count": len(results),
            "results": results,
        }
        return CapabilityResult(
            call_id,
            ASK_WEB_SEARCH,
            CapabilityResultState.SUCCEEDED,
            values,
            # Candidates are a working set for the turn that chooses among
            # them. What survives is that a search happened, what was
            # disclosed, and how many candidates came back — enough to
            # continue after a restart without storing pages nobody read.
            durable_values={
                "search_id": search_id,
                "retrieved_at": values["retrieved_at"],
                "result_count": len(results),
            },
            provenance=RetentionPolicy().non_mail(
                ContentOrigin.EXTERNAL, found.retrieved_at
            ),
        )

    return {ASK_WEB_SEARCH: run_search}


def _charge(ledger: Any, reservation: Any) -> None:
    """Record the request as billed, tolerating a ledger that then fails.

    The reservation is already withdrawn, so a settlement failure cannot let
    spend escape the ceiling; it only leaves the row unreconciled, which is
    the safe direction.
    """
    try:
        ledger.settle(reservation)
    except Exception:
        pass


def _release(ledger: Any, reservation: Any, failure_code: str) -> None:
    try:
        ledger.abandon(reservation, failure_code=failure_code)
    except Exception:
        pass


__all__ = [
    "ASK_WEB_PAGE",
    "ASK_WEB_SEARCH",
    "DEFINITION",
    "SEARCH_DEFINITION",
    "build_web_executors",
    "build_web_search_executors",
]
