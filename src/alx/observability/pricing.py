"""Explicit per-model prices, and nothing inferred.

A price is a fact about a vendor's published rate card, not something to derive
from a model's name or family. An unknown model therefore has no price here and
cannot take part in paid autonomous research; it is not quietly charged at a
neighbouring model's rate. Adding a model to this table is a deliberate act.

Rates are USD per million tokens as
(uncached input, cached input, output, cache write).

Reasoning tokens are a detail within the billed output total and are therefore
recorded but not added to that total a second time.

The cache-write rate is `None` when writing to the prompt cache is not billed
separately for that model, and a number when it is. `None` means "not
applicable for this model", never "unknown": an unverified rate is not recorded
at all, because a cache-write rate carried over from another model generation
would understate spend against a hard ceiling exactly like a guessed price
would. Rates are not inferred across model families under any circumstances.
"""

from __future__ import annotations

from typing import Mapping, NamedTuple


class ModelPrice(NamedTuple):
    """One model's published rates, in USD per million tokens.

    `cache_write` is None when writing to the prompt cache is not billed
    separately for this model. It is never a placeholder for an unknown rate:
    a model whose cache-write billing has not been verified is either recorded
    as not applicable, deliberately, or not recorded at all.
    """

    uncached_input: float
    cached_input: float
    output: float
    cache_write: float | None = None


# Keyed by (provider, model) so two vendors serving a same-named model cannot
# be confused for one another.
# Mirrors alx.contracts.usage.MEASURED_FIELDS. Observability may not import
# contracts, so the list is restated and a test proves it has not drifted.
MEASURED_FIELDS = ("input_tokens", "output_tokens")


USD_PER_MILLION: dict[tuple[str, str], ModelPrice] = {
    # Verified standard short-context rates, recorded by Friedl on 2026-09-01
    # for the first live research test. Each entry is (uncached input, cached
    # input, output, cache write) in USD per million tokens.
    #
    # Only SURVEY is exercised by that first test. COMPARE and JUDGE are priced
    # here so that a mistaken selection is refused for exceeding the ceiling
    # rather than for being unpriced: an unpriced model and a too-expensive one
    # are different faults and should not look alike.
    #
    # The GPT-5.4 entries carry no cache-write rate. No authoritative
    # cache-write price for this generation has been recorded, and GPT-5.6's
    # documented 1.25x multiplier is a fact about GPT-5.6, not about these
    # models. Deriving one from it would be exactly the cross-generation
    # inference this module exists to refuse.
    ("openai", "gpt-5.4-nano"): ModelPrice(0.20, 0.02, 1.25, None),
    ("openai", "gpt-5.4-mini"): ModelPrice(0.75, 0.075, 4.50, None),
    ("openai", "gpt-5.4"): ModelPrice(2.50, 0.25, 15.00, None),
    # Verified from OpenAI's published rate card by Friedl on 2026-09-02, for
    # the authoritative Core under D-005. Cache writes are billed separately on
    # this model at 1.25x the uncached input rate, and the Core enables prompt
    # caching on every call, so the write rate is recorded and charged.
    ("openai", "gpt-5.6-sol"): ModelPrice(4.00, 0.40, 20.00, 5.00),
    # Verified from OpenAI's published rate card by Friedl on 2026-09-02, for
    # the recorded autonomous-cognition evaluation. Same generation as Sol, so
    # cache writes bill separately at 1.25x the uncached input rate. This is a
    # rate verified for this model, not one derived from Sol's.
    ("openai", "gpt-5.6-luna"): ModelPrice(0.20, 0.02, 1.20, 0.25),
}

# Exact provider identities that Friedl approved to inherit an existing price.
# This is deliberately an allowlist rather than model-family parsing: a new or
# unexpected snapshot remains unpriced until it is explicitly recorded here.
APPROVED_PRICE_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("openai", "gpt-5.4-nano-2026-03-17"): ("openai", "gpt-5.4-nano"),
}


def price_of(provider: str, model: str) -> ModelPrice | None:
    """The explicitly approved rate for one exact provider identity, if any."""
    identity = (provider.strip().lower(), model.strip())
    priced_identity = APPROVED_PRICE_ALIASES.get(identity, identity)
    recorded = USD_PER_MILLION.get(priced_identity)
    if recorded is None:
        return None
    # A three-rate entry predates separately billed cache writes and means the
    # write rate is not applicable, which is the conservative reading: writes
    # are charged only where a rate says they are. Normalising here keeps one
    # shape for every caller.
    if not isinstance(recorded, ModelPrice):
        return ModelPrice(*recorded)
    return recorded


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

    Cache-write tokens are charged when, and only when, the model has a
    recorded cache-write rate. A model whose writes are not separately billed
    has a rate of None and its reported write tokens cost nothing. A model that
    does bill them but reports write tokens the rate cannot price is unmeasured
    rather than free, and returns None so the caller charges the reservation in
    full.
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
    uncached_rate, cached_rate, output_rate, cache_write_rate = rate
    # Canonical field names only. The adapter normalised the provider's own
    # layout at its boundary, so nothing here parses a vendor shape.
    input_tokens, cached_tokens, output_tokens, cache_write_tokens = measured
    # Reasoning tokens are already inside output_tokens: the provider reports
    # how many of the billed output tokens were internal reasoning, not an
    # extra charge beside them. Adding them again billed a bounded response
    # above its own reservation, which read as a provider-bound violation and
    # halted research on a call that had done nothing wrong. They stay in the
    # canonical shape as telemetry detail and are not priced separately.
    uncached_tokens = max(0, input_tokens - cached_tokens)
    # Writing the prompt cache is a real charge on models that bill it, and the
    # Core writes its stable prefix on every call. Leaving it unpriced made a
    # cached-heavy call look cheaper than it was, against a hard ceiling.
    write_cost = 0.0
    if cache_write_tokens:
        if cache_write_rate is None:
            # The model does not bill writes separately, so reported write
            # tokens are already inside the input total and cost nothing more.
            write_cost = 0.0
        else:
            write_cost = cache_write_tokens / 1e6 * cache_write_rate
    return round(
        uncached_tokens / 1e6 * uncached_rate
        + cached_tokens / 1e6 * cached_rate
        + output_tokens / 1e6 * output_rate
        + write_cost,
        6,
    )


def _count(usage: Mapping[str, object], name: str) -> int:
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _measured_counts(
    usage: Mapping[str, object],
) -> tuple[int, int, int, int] | None:
    """Validate canonical billing totals without parsing a provider shape."""
    # A non-mapping report is silence in an unexpected shape, not a free call.
    # Raising here would push a provider's malformed answer into the caller as
    # a crash, when the honest reading is simply that nothing was measured.
    if not isinstance(usage, Mapping):
        return None
    values: dict[str, int] = {}
    for name in MEASURED_FIELDS:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[name] = value
    for name in ("cached_tokens", "reasoning_tokens", "cache_write_tokens"):
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
    return (
        values["input_tokens"],
        values["cached_tokens"],
        values["output_tokens"],
        values["cache_write_tokens"],
    )


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

    On a model that bills cache writes, the worst case is a cache miss whose
    every input token is also written to the cache: charged once as uncached
    input and once as a write. Anything less would leave a reservation the real
    call could exceed.
    """
    rate = price_of(provider, model)
    if rate is None:
        return None
    uncached_rate, _cached_rate, output_rate, cache_write_rate = rate
    if max_input_tokens < 0 or max_output_tokens <= 0:
        raise ValueError("token bounds must be non-negative and output positive")
    input_rate = uncached_rate + (cache_write_rate or 0.0)
    return round(
        max_input_tokens / 1e6 * input_rate
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
