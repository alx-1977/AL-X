"""Provider-neutral transport records for the configured reasoning model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from alx.contracts.records import StructuredData, freeze_data


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


class ModelRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: ModelRole
    content: str

    def __post_init__(self) -> None:
        _required(self.content, "content")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """A model request created by AL/X reasoning, not by a frontend."""

    messages: tuple[ModelMessage, ...]
    output_schema_name: str
    output_schema: StructuredData

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if not self.messages:
            raise ValueError("a model request requires at least one message")
        _required(self.output_schema_name, "output_schema_name")
        object.__setattr__(self, "output_schema", freeze_data(self.output_schema))


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """Structured provider output; only the Core may interpret its meaning."""

    provider: str
    model: str
    output: StructuredData
    usage: StructuredData = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.provider, "provider")
        _required(self.model, "model")
        object.__setattr__(self, "output", freeze_data(self.output))
        object.__setattr__(self, "usage", freeze_data(self.usage))


class ReasoningModel(Protocol):
    """Replaceable model transport with no ownership of memory or tools."""

    def complete(self, request: ModelRequest) -> ModelCompletion: ...
