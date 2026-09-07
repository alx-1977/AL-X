"""Every kind of occasion, offered through one producer.

There is one tick, one runner and one Core-turn lock, and adding a second of
any of them would be a competing production path to the same outcome. So when
a new kind of occasion appears — a matured request, a finished external task —
it joins here rather than bringing its own tick along.

This decides nothing about the occasions it carries. It does not order them by
anything but the order its producers returned them, does not read them, and
cannot skip one. Each occasion is claimed, released and closed by the producer
that made it, so the producers keep their own idempotence and this adds none.
"""

from __future__ import annotations

from typing import Any

from alx.contracts.continuity import CognitionOpportunity


class CombinedOccasionSource:
    """One producer over several, delegating each occasion back to its own."""

    def __init__(self, *sources: Any) -> None:
        self._sources = tuple(sources)

    @property
    def enabled(self) -> bool:
        return any(getattr(source, "enabled", False) for source in self._sources)

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        occasions: list[CognitionOpportunity] = []
        for source in self._sources:
            occasions.extend(source.due_opportunities())
        return tuple(occasions)

    def _owner(self, opportunity: CognitionOpportunity) -> Any:
        """The producer that made this occasion.

        Found by asking rather than by parsing the identifier: a producer knows
        its own occasions, and a rule here about which prefix belongs to whom
        would be a second place to keep that knowledge correct.
        """
        for source in self._sources:
            if source.owns(opportunity):
                return source
        return None

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        owner = self._owner(opportunity)
        # An occasion nobody owns is not claimed, so nothing is spent on it.
        return False if owner is None else owner.claim(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        owner = self._owner(opportunity)
        if owner is not None:
            owner.release(opportunity)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        owner = self._owner(opportunity)
        if owner is not None:
            owner.mark_honoured(opportunity)
