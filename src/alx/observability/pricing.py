"""Explicit per-model prices, and nothing inferred.

A price is a fact about a vendor's published rate card, not something to derive
from a model's name or family. An unknown model therefore has no price here and
cannot take part in paid autonomous research; it is not quietly charged at a
neighbouring model's rate. Adding a model to this table is a deliberate act.

Rates are USD per million tokens as (uncached input, cached input, output).
Reasoning tokens are a detail within the billed output total and are therefore
recorded but not added to that total a second time.
"""

from __future__ import annotations

from typing import Mapping


# Keyed by (provider, model) so two vendors serving a same-named model cannot
# be confused for one another.
# Mirrors alx.contracts.usage.MEASURED_FIELDS. Observability may not import
# contracts, so the list is restated and a test proves it has not drifted.
MEASURED_FIELDS = ("input_tokens", "output_tokens")


USD_PER_MILLION: dict[tuple[str, str], tuple[float, float, float]] = {
    # Verified standard short-context rates, recorded by Friedl on 2026-09-01
    # for the first live research test. Each entry is (uncached input, cached
    # input, output) in USD per million tokens.
    #
    # Only SURVEY is exercised by that first test. COMPARE and JUDGE are priced
    # here so that a mistaken selection is refused for exceeding the ceiling
    # rather than for being unpriced: an unpriced model and a too-expensive one
    # are different faults and should not look alike.
    ("openai", "gpt-5.4-nano"): (0.20, 0.02, 1.25),
    ("openai", "gpt-5.4-mini"): (0.75, 0.075, 4.50),
    ("openai", "gpt-5.4"): (2.50, 0.25, 15.00),
}


def price_of(provider: str, model: str) -> tuple[float, float, float] | None:
    """The rate for one model, or None when no price is configured."""
    return USD_PER_MILLION.get((provider.strip().lower(), model.strip()))


def is_priced(provider: str, model: str) -> bool:
    return price_of(provider, model) is not None


def cost_usd(provider: str, model: str, usage: Mapping[str, object]) -> float | None:
    """Cost of one call from its measured usage, or None when it cannot be known.

    Returns None both for an unpriced model and for a usage report that carries
    no token counts at all. A provider that reported nothing has not told us the
    call was free, and pricing that silence at zero would let an unlimited
    number of unmeasured calls run inside one day's budget.

    Cached input is charged at its own lower rate, so it is subtracted from the
    uncached count rather than counted twice.
    """
    rate = price_of(provider, model)
    if rate is None:
        return None
    # A report with no token counts is silence, not a free call. Returning None
    # makes the caller charge the full reservation instead of nothing.
    #
    # Observability is a leaf module and may not import contracts, so this
    # restates alx.contracts.usage.MEASURED_FIELDS rather than importing it. A
    # test asserts the two stay identical.
    measured = _measured_counts(usage)
    if measured is None:
        return None
    uncached_rate, cached_rate, output_rate = rate
    # Canonical field names only. The adapter normalised the provider's own
    # layout at its boundary, so nothing here parses a vendor shape.
    input_tokens, cached_tokens, output_tokens = measured
    # Reasoning tokens are already inside output_tokens: the provider reports
    # how many of the billed output tokens were internal reasoning, not an
    # extra charge beside them. Adding them again billed a bounded response
    # above its own reservation, which read as a provider-bound violation and
    # halted research on a call that had done nothing wrong. They stay in the
    # canonical shape as telemetry detail and are not priced separately.
    uncached_tokens = max(0, input_tokens - cached_tokens)
    return round(
        uncached_tokens / 1e6 * uncached_rate
        + cached_tokens / 1e6 * cached_rate
        + output_tokens / 1e6 * output_rate,
        6,
    )


def _count(usage: Mapping[str, object], name: str) -> int:
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _measured_counts(
    usage: Mapping[str, object],
) -> tuple[int, int, int] | None:
    """Validate canonical billing totals without parsing a provider shape."""
    values: dict[str, int] = {}
    for name in MEASURED_FIELDS:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[name] = value
    for name in ("cached_tokens", "reasoning_tokens"):
        value = usage.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[name] = value
    if values["cached_tokens"] > values["input_tokens"]:
        return None
    if values["reasoning_tokens"] > values["output_tokens"]:
        return None
    if not values["input_tokens"] and not values["output_tokens"]:
        return None
    return values["input_tokens"], values["cached_tokens"], values["output_tokens"]


class ConfiguredPricing:
    """The configured price table, as the pricing contract.

    An object rather than module functions so the bounded-question path depends
    on a contract it is given, not on this module.
    """

    def is_priced(self, provider: str, model: str) -> bool:
        return is_priced(provider, model)

    def cost_usd(
        self, provider: str, model: str, usage: Mapping[str, object]
    ) -> float | None:
        return cost_usd(provider, model, usage)


def worst_case_usd(
    provider: str,
    model: str,
    max_input_tokens: int,
    max_output_tokens: int,
) -> float | None:
    """The most one bounded request can cost, or None when unpriced.

    Every token is priced at the uncached input rate or the output rate, with no
    cache discount assumed: a cache miss is the expensive case, and a ceiling
    must hold in the expensive case. Reasoning tokens bill as output, so the
    output bound covers them.
    """
    rate = price_of(provider, model)
    if rate is None:
        return None
    uncached_rate, _cached_rate, output_rate = rate
    if max_input_tokens < 0 or max_output_tokens <= 0:
        raise ValueError("token bounds must be non-negative and output positive")
    return round(
        max_input_tokens / 1e6 * uncached_rate
        + max_output_tokens / 1e6 * output_rate,
        6,
    )


class ConfiguredPricingWorstCase(ConfiguredPricing):
    """Pricing that can also answer what a bounded request may cost at most."""

    def worst_case_usd(
        self,
        provider: str,
        model: str,
        max_input_tokens: int,
        max_output_tokens: int,
    ) -> float | None:
        return worst_case_usd(provider, model, max_input_tokens, max_output_tokens)
