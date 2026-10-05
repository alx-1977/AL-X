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

# Where each breakdown field may appear in a provider's own report. A
# provider that names none of these did not report the breakdown, which is not
# the same as reporting zero. The last path of each cache field is Anthropic's
# beside-input layout, read separately below.
_DETAIL_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "cached_tokens": (
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
        ("cached_tokens",),
        # Codex names it this way, already counted inside input_tokens.
        ("cached_input_tokens",),
        ("cache_read_input_tokens",),
    ),
    "cache_write_tokens": (
        ("input_tokens_details", "cache_write_tokens"),
        ("prompt_tokens_details", "cache_write_tokens"),
        ("cache_write_tokens",),
        ("cache_write_input_tokens",),
        ("cache_creation_input_tokens",),
    ),
    "reasoning_tokens": (
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
        ("reasoning_tokens",),
        ("reasoning_output_tokens",),
    ),
}


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
    # Every path but the last: that one is Anthropic's beside-input layout,
    # which is folded into the input count separately below.
    cached = _aliased_count(usage, _DETAIL_PATHS["cached_tokens"][:-1])
    cache_write = _aliased_count(usage, _DETAIL_PATHS["cache_write_tokens"][:-1])
    reasoning = _aliased_count(usage, _DETAIL_PATHS["reasoning_tokens"])
    total = _aliased_count(usage, (("total_tokens",),))
    # Anthropic's layout, which the Claude CLI reports. Its `input_tokens` is
    # only the uncached remainder: cache reads and cache writes are counted
    # beside it, not inside it. Read as the other layouts, a Core call that
    # carried 40,000 tokens of context was recorded as "input 2, cached 0".
    cache_read = _aliased_count(usage, (("cache_read_input_tokens",),))
    cache_creation = _aliased_count(usage, (("cache_creation_input_tokens",),))
    counts = (
        input_tokens, output_tokens, cached, cache_write, reasoning, total,
        cache_read, cache_creation,
    )
    if any(value is _INVALID for value in counts):
        return _unmeasured()
    assert all(isinstance(value, int) for value in counts)
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        beside = input_tokens + cache_read + cache_creation
        # A reported total arbitrates which way the provider counted. Only
        # when it says the cache counts were already inside `input_tokens` is
        # the beside reading refused; without a total, the documented layout
        # is the one Anthropic defines.
        inside = total and total in (
            input_tokens + output_tokens, input_tokens + output_tokens + reasoning,
        ) and total not in (beside + output_tokens, beside + output_tokens + reasoning)
        if not inside:
            input_tokens = beside
        cached = cache_read
        cache_write = cache_creation
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
    if not total and (input_tokens or output_tokens):
        # Not a default: canonical output already includes reasoning in every
        # layout above, so the total is exactly their sum. Leaving it zero is
        # how a measured call came to display "total 0".
        total = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def usage_telemetry(
    usage: Mapping[str, Any], reported: Any = None,
) -> dict[str, Any]:
    """What a telemetry event may say about a call's usage.

    A measured report carries its canonical counts. An unmeasured one carries
    only the fact that it was not measured: its zeros are the absence of a
    measurement, and publishing them as counts is how a console came to show
    a forty-thousand-token call as "input 0".

    Given the provider's own report as `reported`, a breakdown it never named
    is left out rather than published as zero: Anthropic reports no reasoning
    breakdown, and "reasoning 0" claimed one.
    """
    if not is_measured(usage):
        return {"usage_measured": False}
    omitted = (
        set() if not isinstance(reported, Mapping)
        else {
            name for name, paths in _DETAIL_PATHS.items()
            if all(_value(reported, *path) is _MISSING for path in paths)
        }
    )
    return {
        "usage_measured": True,
        **{
            name: usage[name]
            for name in CANONICAL_FIELDS
            if name not in omitted
            and isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)
        },
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
