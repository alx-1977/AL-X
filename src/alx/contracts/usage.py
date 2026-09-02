"""One canonical shape for what a model call consumed.

Providers report usage differently: OpenAI nests cached input under
`input_tokens_details` and reasoning under `output_tokens_details`, while
OpenAI-style chat completions use `prompt_tokens` and `completion_tokens`.
They also disagree on whether reasoning tokens sit inside the output count or
beside it; canonical `output_tokens` is always the billed output, with
`reasoning_tokens` the breakdown within it.
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

# The two totals a complete usage measurement must contain. Detail fields can
# refine those totals, but cannot establish a measurement by themselves.
MEASURED_FIELDS = ("input_tokens", "output_tokens")

_MISSING = object()
_INVALID = object()


def _value(values: Mapping[str, Any], *path: str) -> object:
    """Return a nested value while distinguishing absence from bad structure."""
    current: Any = values
    for key in path:
        if not isinstance(current, Mapping):
            return _INVALID
        if key not in current:
            return _MISSING
        current = current[key]
    return current


def _aliased_count(
    values: Mapping[str, Any],
    paths: tuple[tuple[str, ...], ...],
    *,
    required: bool = False,
) -> int | object:
    """Read the first present alias without converting malformed values to zero."""
    for path in paths:
        value = _value(values, *path)
        if value is _MISSING:
            continue
        if value is _INVALID or isinstance(value, bool) or not isinstance(value, int):
            return _INVALID
        if value < 0:
            return _INVALID
        return value
    return _INVALID if required else 0


def _unmeasured() -> dict[str, int]:
    return {name: 0 for name in CANONICAL_FIELDS}


def normalise_usage(usage: Any) -> dict[str, int]:
    """Flatten one provider's usage report into the canonical shape.

    Accepts both layouts AL/X's adapters produce. Unknown or malformed input
    yields zeros rather than raising: a usage report is a measurement, and a
    failure to measure must not fail the call that already happened. Callers
    that spend money check `is_measured` instead.
    """
    if not isinstance(usage, Mapping):
        return _unmeasured()

    # Responses-style first, then chat-completions names for the same quantity.
    input_tokens = _aliased_count(
        usage, (("input_tokens",), ("prompt_tokens",)), required=True
    )
    output_tokens = _aliased_count(
        usage, (("output_tokens",), ("completion_tokens",)), required=True
    )
    cached = _aliased_count(
        usage,
        (
            ("input_tokens_details", "cached_tokens"),
            ("prompt_tokens_details", "cached_tokens"),
            ("cached_tokens",),
        ),
    )
    cache_write = _aliased_count(
        usage,
        (
            ("input_tokens_details", "cache_write_tokens"),
            ("prompt_tokens_details", "cache_write_tokens"),
            ("cache_write_tokens",),
        ),
    )
    reasoning = _aliased_count(
        usage,
        (
            ("output_tokens_details", "reasoning_tokens"),
            ("completion_tokens_details", "reasoning_tokens"),
            ("reasoning_tokens",),
        ),
    )
    total = _aliased_count(usage, (("total_tokens",),))
    counts = (input_tokens, output_tokens, cached, cache_write, reasoning, total)
    if any(value is _INVALID for value in counts):
        return _unmeasured()
    assert all(isinstance(value, int) for value in counts)
    if cached > input_tokens:
        return _unmeasured()
    # Two layouts report reasoning. OpenAI counts reasoning inside
    # output_tokens, so total is input plus output. xAI reports reasoning
    # beside completion_tokens, so total is input plus completion plus
    # reasoning, and reasoning can exceed completion: a Core call that thought
    # for 841 tokens and answered in 253 is a real, measured call. Treating
    # that as inconsistent zeroed every reasoning-heavy Core call in the usage
    # record while the short ones were measured. Canonical output_tokens is
    # the billed output, so the beside layout is folded into it and reasoning
    # stays as the breakdown it is in both layouts.
    if total and total == input_tokens + output_tokens + reasoning and reasoning:
        output_tokens += reasoning
    elif total and total != input_tokens + output_tokens:
        return _unmeasured()
    elif not total and reasoning > output_tokens:
        # No total to arbitrate, but reasoning cannot be a breakdown of a
        # smaller count, so it was reported beside.
        output_tokens += reasoning
    if reasoning > output_tokens:
        return _unmeasured()
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def is_measured(usage: Mapping[str, Any]) -> bool:
    """Whether a canonical report has complete, internally consistent totals."""
    input_tokens = _value(usage, "input_tokens")
    output_tokens = _value(usage, "output_tokens")
    cached = _value(usage, "cached_tokens")
    reasoning = _value(usage, "reasoning_tokens")
    counts = (input_tokens, output_tokens, cached, reasoning)
    if any(
        value in (_MISSING, _INVALID)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in counts
    ):
        return False
    assert all(isinstance(value, int) for value in counts)
    if cached > input_tokens or reasoning > output_tokens:
        return False
    return input_tokens > 0 or output_tokens > 0
