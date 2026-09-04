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


__all__ = ["ASK_WEB_PAGE", "DEFINITION", "build_web_executors"]
