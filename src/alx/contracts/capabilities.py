"""Provider-neutral schemas and capability descriptions, without dispatch authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ValueKind(str, Enum):
    ANY = "any"
    NULL = "null"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    OBJECT = "object"
    ARRAY = "array"


@dataclass(frozen=True, slots=True)
class StructuredSchema:
    """A compact recursive schema for language-blind structured values."""

    kind: ValueKind
    properties: Mapping[str, "StructuredSchema"] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    items: "StructuredSchema | None" = None
    extra_properties: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ValueKind):
            raise TypeError("schema kind must be a ValueKind")
        if self.items is not None and not isinstance(self.items, StructuredSchema):
            raise TypeError("schema items must be a StructuredSchema")
        if not isinstance(self.extra_properties, bool):
            raise TypeError("extra_properties must be a bool")
        properties = dict(self.properties)
        if any(not isinstance(key, str) or not key.strip() for key in properties):
            raise ValueError("schema property names must be non-blank strings")
        if any(not isinstance(value, StructuredSchema) for value in properties.values()):
            raise TypeError("schema properties must be schemas")
        object.__setattr__(self, "properties", MappingProxyType(properties))
        object.__setattr__(self, "required", tuple(self.required))
        if any(not isinstance(name, str) or not name.strip() for name in self.required):
            raise ValueError("required property names must be non-blank strings")
        if self.kind is not ValueKind.OBJECT and (self.properties or self.required):
            raise ValueError("properties apply only to object schemas")
        if self.kind is not ValueKind.ARRAY and self.items is not None:
            raise ValueError("items apply only to array schemas")
        if set(self.required) - set(self.properties):
            raise ValueError("required properties must be declared")

    def accepts(self, value: Any) -> bool:
        if self.kind is ValueKind.ANY:
            return _structured(value)
        if self.kind is ValueKind.NULL:
            return value is None
        if self.kind is ValueKind.BOOLEAN:
            return isinstance(value, bool)
        if self.kind is ValueKind.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if self.kind is ValueKind.NUMBER:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if self.kind is ValueKind.STRING:
            return isinstance(value, str)
        if self.kind is ValueKind.ARRAY:
            return isinstance(value, (tuple, list)) and all(
                _structured(item) and (self.items is None or self.items.accepts(item)) for item in value
            )
        if self.kind is ValueKind.OBJECT:
            if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
                return False
            if not set(self.required) <= set(value):
                return False
            if not self.extra_properties and not set(value) <= set(self.properties):
                return False
            return all(_structured(item) and (key not in self.properties or self.properties[key].accepts(item)) for key, item in value.items())
        raise AssertionError("unknown value kind")


def _structured(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _structured(item) for key, item in value.items())
    return isinstance(value, (tuple, list)) and all(_structured(item) for item in value)


class SideEffect(str, Enum):
    """Technical effect class; never conversational intent or workflow routing."""

    NONE = "none"
    ATTENTION_STATE = "attention_state"
    EFFECTFUL = "effectful"


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    purpose: str
    input_schema: StructuredSchema
    output_schema: StructuredSchema
    side_effect: SideEffect
    possible_failure_codes: tuple[str, ...] = ()
    # None persists every structured argument in the goal's execution audit.
    # A capability whose input contains content owned by another durable store
    # names only the identity fields needed for restart continuity.
    durable_input_fields: tuple[str, ...] | None = None
    # Whether this capability can carry wording AL/X composed to someone else.
    # It is what scopes the rule that she may not send Friedl text he has never
    # heard: only a capability that actually transmits her words is checked
    # against what she last said.
    #
    # Declared rather than inferred, and false by default. `side_effect` says
    # whether there is an effect, never what kind, so a web search and a mail
    # send are both EFFECTFUL. Argument names cannot say it either: naming a
    # search argument `subject` once made every search look like unsent mail
    # and be refused. A capability that sends must therefore say so.
    transmits_authored_text: bool = False
    # Whether executing this capability changes something outside AL/X that
    # Friedl could observe: mail moved or sent, a file written, code pushed, a
    # device flashed. It scopes the rule that she may not say an action is done
    # unless the attempt that does it actually succeeded.
    #
    # Declared rather than inferred, for the same reason as the field above.
    # `side_effect` distinguishes whether there is an effect, not what kind: a
    # web search and a mail send are both EFFECTFUL, and only one of them
    # changes the world. Reading EFFECTFUL as "mutation" would make a search
    # claimable as a completed action and hold every search to a rule about
    # external change. A capability that mutates must therefore say so.
    externally_observable_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.capability_id.strip() or not self.purpose.strip():
            raise ValueError("capability identifiers and purposes must not be blank")
        if self.input_schema.kind is not ValueKind.OBJECT or self.output_schema.kind is not ValueKind.OBJECT:
            raise ValueError("capability input and output schemas must be object schemas")
        if not isinstance(self.side_effect, SideEffect):
            raise TypeError("side_effect must be a SideEffect")
        if not isinstance(self.transmits_authored_text, bool):
            raise TypeError("transmits_authored_text must be a bool")
        if not isinstance(self.externally_observable_mutation, bool):
            raise TypeError("externally_observable_mutation must be a bool")
        if self.externally_observable_mutation and self.side_effect is not SideEffect.EFFECTFUL:
            # Changing the world outside AL/X is an effect by definition.
            raise ValueError("an externally observable mutation must be effectful")
        codes = tuple(self.possible_failure_codes)
        object.__setattr__(self, "possible_failure_codes", codes)
        if any(not isinstance(item, str) or not item.strip() for item in codes):
            raise ValueError("failure codes must not be blank")
        if len(set(codes)) != len(codes):
            raise ValueError("failure codes must be unique")
        if self.durable_input_fields is not None:
            fields = tuple(self.durable_input_fields)
            if len(fields) != len(set(fields)):
                raise ValueError("durable input fields must be unique")
            if set(fields) - set(self.input_schema.properties):
                raise ValueError("durable input fields must be declared inputs")
            object.__setattr__(self, "durable_input_fields", fields)
