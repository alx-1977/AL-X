"""One canonical shape for what a model call consumed.

Providers report usage differently: OpenAI nests cached input under
`input_tokens_details` and reasoning under `output_tokens_details`, while
OpenAI-style chat completions use `prompt_tokens` and `completion_tokens`.
Flattening that in each consumer meant telemetry read one shape and cost
settlement read another, so a real response priced 8,000 cached tokens at the
uncached rate and missed 1,500 reasoning tokens entirely.

Normalising happens once, in the adapter, before the completion leaves the
provider boundary. Everything downstream reads these names and nothing else
parses a provider's own field layout.
"""

from __future__ import annotations

from typing import Any, Mapping


# The canonical field names. A consumer reads these; a provider produces them.
CANONICAL_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

# Whether the report carried any measurement at all. An empty or unparseable
# report is not evidence a call was free, so callers that spend money treat a
# report failing this as unmeasured and charge the full reservation.
MEASURED_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens")


def _integer(values: Mapping[str, Any], *path: str) -> int:
    """A non-negative integer at a nested path, or zero."""
    current: Any = values
    for key in path:
        if not isinstance(current, Mapping):
            return 0
        current = current.get(key)
    if isinstance(current, bool) or not isinstance(current, int):
        return 0
    return max(0, current)


def normalise_usage(usage: Any) -> dict[str, int]:
    """Flatten one provider's usage report into the canonical shape.

    Accepts both layouts AL/X's adapters produce. Unknown or malformed input
    yields zeros rather than raising: a usage report is a measurement, and a
    failure to measure must not fail the call that already happened. Callers
    that spend money check `is_measured` instead.
    """
    if not isinstance(usage, Mapping):
        return {name: 0 for name in CANONICAL_FIELDS}

    # Responses-style first, then chat-completions names for the same quantity.
    input_tokens = _integer(usage, "input_tokens") or _integer(usage, "prompt_tokens")
    output_tokens = _integer(usage, "output_tokens") or _integer(
        usage, "completion_tokens"
    )
    cached = (
        _integer(usage, "input_tokens_details", "cached_tokens")
        or _integer(usage, "prompt_tokens_details", "cached_tokens")
        or _integer(usage, "cached_tokens")
    )
    cache_write = (
        _integer(usage, "input_tokens_details", "cache_write_tokens")
        or _integer(usage, "prompt_tokens_details", "cache_write_tokens")
        or _integer(usage, "cache_write_tokens")
    )
    reasoning = (
        _integer(usage, "output_tokens_details", "reasoning_tokens")
        or _integer(usage, "completion_tokens_details", "reasoning_tokens")
        or _integer(usage, "reasoning_tokens")
    )
    # Cached input is a subset of input. A provider reporting more cached than
    # input would otherwise produce a negative uncached count and undercharge.
    cached = min(cached, input_tokens)
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": _integer(usage, "total_tokens"),
    }


def is_measured(usage: Mapping[str, Any]) -> bool:
    """Whether a normalised report carries any token count at all."""
    return any(_integer(usage, name) for name in MEASURED_FIELDS)
