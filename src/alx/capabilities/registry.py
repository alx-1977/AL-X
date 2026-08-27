"""An explicit catalogue of reusable capability descriptions."""

from __future__ import annotations

from typing import Mapping

from alx.contracts import CapabilityDefinition


class DuplicateCapability(Exception):
    pass


class UnknownCapability(Exception):
    pass


class CapabilityRegistry:
    """Catalogue metadata; SafetyGate's injected policy map is its separate authority source."""
    def __init__(self, definitions: tuple[CapabilityDefinition, ...] = ()) -> None:
        self._definitions: dict[str, CapabilityDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: CapabilityDefinition) -> None:
        if definition.capability_id in self._definitions:
            raise DuplicateCapability(definition.capability_id)
        self._definitions[definition.capability_id] = definition

    def lookup(self, capability_id: str) -> CapabilityDefinition:
        try:
            return self._definitions[capability_id]
        except KeyError as error:
            raise UnknownCapability(capability_id) from error

    def list_definitions(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(self._definitions.values())
