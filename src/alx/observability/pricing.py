"""Explicit per-model prices, and nothing inferred.

A price is a fact about a vendor's published rate card, not something to derive
from a model's name or family. An unknown model therefore has no price here and
cannot take part in paid autonomous research; it is not quietly charged at a
neighbouring model's rate. Adding a model to this table is a deliberate act.

Rates are USD per million tokens as (uncached input, cached input, output).
Reasoning tokens are billed as output by every provider configured here, so
they are added to the output count rather than priced separately.
"""

from __future__ import annotations

from typing import Mapping


# Keyed by (provider, model) so two vendors serving a same-named model cannot
# be confused for one another.
USD_PER_MILLION: dict[tuple[str, str], tuple[float, float, float]] = {}


def price_of(provider: str, model: str) -> tuple[float, float, float] | None:
    """The rate for one model, or None when no price is configured."""
    return USD_PER_MILLION.get((provider.strip().lower(), model.strip()))


def is_priced(provider: str, model: str) -> bool:
    return price_of(provider, model) is not None


# A usage report must contain at least one of these to be a measurement. An
# empty report is silence, not a free call.
MEASURED_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens")


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
    if not any(_count(usage, name) for name in MEASURED_FIELDS):
        return None
    uncached_rate, cached_rate, output_rate = rate
    input_tokens = _count(usage, "input_tokens")
    cached_tokens = min(_count(usage, "cached_tokens"), input_tokens)
    output_tokens = _count(usage, "output_tokens") + _count(usage, "reasoning_tokens")
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
