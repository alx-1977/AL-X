"""One bounded retry policy for subscription transports, without error categories."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import wraps
from time import sleep
from typing import Callable

from alx.contracts.coding import CodingError, CodingSessionResult
from alx.contracts.diagnostics import mask_credentials
from alx.providers.errors import ProviderError


def error_event_message(event: Mapping[str, object]) -> str:
    """Decode a CLI error field without rewriting or interpreting its text."""
    error = event.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return error["message"]
    for value in (event.get("message"), error, event.get("data")):
        if isinstance(value, str):
            return value
    return json.dumps(dict(event), sort_keys=True)


def cli_failure_message(stderr: str, stdout: str) -> str:
    """Prefer the CLI's structured error field to envelope syntax or warnings."""
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError):
        envelope = None
    if isinstance(envelope, Mapping) and envelope.get("is_error"):
        result = envelope.get("result")
        if isinstance(result, str):
            return result
    # Codex emits JSONL, including error/turn.failed events on nonzero exits.
    events = []
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, Mapping) and event.get("type") in ("error", "turn.failed"):
            events.append(error_event_message(event))
    return "\n".join(events) if events else stderr or stdout or ""


def retry_delay(message: str, attempt: int, *, now: datetime | None = None) -> float:
    """Extract only the stated local reset clock; otherwise use capped backoff."""
    # Parse the clock's syntax, without assigning meaning to the failure.
    before, separator, clock = message.lower().partition("resets ")
    if separator and (not before or not before[-1].isalnum()):
        clock = clock.lstrip()
        if clock.startswith("at "):
            clock = clock[3:].lstrip()
        length = 0
        while length < len(clock) and clock[length] in "0123456789:":
            length += 1
        digits = clock[:length].split(":")
        suffix = clock[length:].lstrip()
        period = suffix[:2] if suffix[:2] in ("am", "pm") else ""
        # Refuse malformed clock tokens instead of accepting their prefix.
        tail = suffix[2:] if period else clock[length:]
        valid_tail = not tail or not (tail[0].isalnum() or tail[0] == ":")
        if (1 <= len(digits) <= 2 and digits[0].isdigit()
                and len(digits[0]) <= 2
                and (len(digits) == 1 or (len(digits[1]) == 2 and digits[1].isdigit()))
                and valid_tail):
            hour, minute = int(digits[0]), int(digits[1]) if len(digits) == 2 else 0
            if minute < 60 and ((period and 1 <= hour <= 12) or (not period and hour < 24)):
                if period:
                    hour = hour % 12 + (12 if period == "pm" else 0)
                local = now if now is not None else datetime.now()
                reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if reset < local:
                    reset += timedelta(days=1)
                return reset.timestamp() - local.timestamp()
    return min(2.0 ** min(attempt, 5), 30.0)


@dataclass(frozen=True)
class SubscriptionRetryPolicy:
    """Three attempts by default; clock and waiting are injectable for tests."""

    max_attempts: int = 3
    wait: Callable[[float], None] | None = None
    now: Callable[[], datetime] | None = None

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 2:
            raise ValueError("subscription retry requires at least two attempts")

    def pause(self, message: str, attempt: int) -> None:
        delay = retry_delay(message, attempt, now=self.now() if self.now else None)
        (self.wait or sleep)(delay)


def retry_subscription(operation):
    """The sole retry owner; transport attempts raise structured failures.

    Cancellation is control flow and must propagate immediately. No failure
    message is inspected except by retry_delay and the existing secret masker.
    """
    @wraps(operation)
    def run(self, *args, **kwargs):
        policy = self._retry_policy
        for attempt in range(policy.max_attempts):
            try:
                result = operation(self, *args, **kwargs)
                if not isinstance(result, CodingSessionResult) or result.completed:
                    return result
                message = mask_credentials(str(
                    result.diagnostics.get("service_message", result.report)
                ))
                result = replace(result, report=mask_credentials(result.report), diagnostics={
                    **result.diagnostics, "service_message": message, "retry_exhausted": True,
                })
                failure = None
            except (ProviderError, CodingError) as error:
                if isinstance(error, CodingError) and error.code == "coding_cancelled":
                    raise
                raw_message = error.details.get(
                    "service_message", getattr(error, "reason", str(error))
                )
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8", errors="replace")
                message = mask_credentials(str(raw_message))
                details = {**error.details, "service_message": message, "retry_exhausted": True}
                if isinstance(error, ProviderError):
                    failure = ProviderError(error.provider, error.reason, details)
                else:
                    failure = CodingError(error.code, **details)
            # Outside the handler: discard payload-bearing exception chains.
            if attempt == policy.max_attempts - 1:
                if failure is None:
                    return result
                raise failure
            policy.pause(message, attempt)
    return run
