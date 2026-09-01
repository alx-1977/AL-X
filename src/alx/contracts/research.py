"""The spending boundary a research call must respect, as a contract.

The ledger itself is durable storage and lives in observability. What the
specialist layer needs is only the shape of the promise: reserve before a call,
settle afterwards, and refuse when the day cannot cover another request. Keeping
that promise here lets the bounded-question path depend on a contract rather
than on storage, which is what the module boundaries require.
"""

from __future__ import annotations

from typing import Mapping, Protocol


class ResearchModelUnpriced(Exception):
    """Raised when a model has no trustworthy price for paid research.

    Recording a guessed cost would understate spend against a hard ceiling.
    The model is refused until its price is configured or its provider reports
    the actual cost of the request.
    """

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(f"no configured price for {provider} model {model}")


class Reservation(Protocol):
    """A withdrawal held against the day until the real cost is known."""

    reservation_id: str
    reserved_usd: float


class ResearchLedger(Protocol):
    """Reserves spend before a research call and reconciles it after."""

    def reserve(
        self, tier: str, provider: str, model: str, kind: str = "research"
    ) -> Reservation: ...

    def settle(self, reservation: Reservation, actual_usd: float) -> float: ...

    def abandon(self, reservation: Reservation) -> float: ...

    def remaining_usd(self, day: str | None = None) -> float: ...


class ResearchPricing(Protocol):
    """Explicit per-model prices; never a guess for an unknown model."""

    def is_priced(self, provider: str, model: str) -> bool: ...

    def cost_usd(
        self, provider: str, model: str, usage: Mapping[str, object]
    ) -> float | None: ...
