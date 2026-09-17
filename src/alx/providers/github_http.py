"""One reading of what GitHub's answer means, shared by the providers.

GitHub rate-limits with `403` and a retry header rather than `429`, so a status
check that has not been told this reads throttling as a refusal. The two are not
the same fact and do not deserve the same response: one is worth waiting out,
and the other means the request will never be accepted.

It lives here because two providers ask the same question. When the publication
path was written without this, it had a subtly different interpretation of the
same status code from the review path — which is how one call would have
reported "try later" as "no" while the other did not.

This is not an HTTP framework. It answers one question about one response.
"""

from __future__ import annotations

from typing import Any


def throttled(response: Any) -> bool:
    """Whether GitHub is asking the caller to try later rather than refusing.

    A `403` counts only with the evidence GitHub actually sends with a rate
    limit. A bare `403` is an authorisation refusal and stays one: treating
    every `403` as temporary would turn a permanently rejected request into a
    silent retry.
    """
    if getattr(response, "status_code", None) != 403:
        return False
    headers = getattr(response, "headers", None) or {}
    try:
        if "Retry-After" in headers:
            return True
        return headers.get("X-RateLimit-Remaining") == "0"
    except TypeError:
        return False


def unavailable(response: Any) -> bool:
    """Whether the answer says nothing about the request except "not now".

    Throttling, a timeout, and a server fault are all availability: the request
    may succeed unchanged later. Anything else in the 4xx range is the server
    declining this request, which no amount of waiting changes.
    """
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return False
    return throttled(response) or status in (408, 429) or status >= 500


__all__ = ["throttled", "unavailable"]
