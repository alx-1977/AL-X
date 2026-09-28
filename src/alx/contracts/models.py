"""Provider-neutral transport records for the configured reasoning model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Mapping
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
    affinity_key: str | None = None
    # Routes a request to a cache holding the same stable prefix. Every
    # conversation sends an identical prefix, so keying this per conversation
    # would split one reusable cache into many that never get reused.
    cache_key: str | None = None
    # A hard provider-side ceiling on generated tokens. None leaves the
    # provider's own default in place, which is right for Core conversation and
    # wrong for anything spending against a dollar ceiling: without a bound
    # there is no worst-case price, so no reservation can be honest. Declared
    # last so existing positional callers keep their argument order.
    max_output_tokens: int | None = None
    # Call classification is transport metadata, not model authority. Research
    # carries its reservation identity through the provider event so usage and
    # settlement update one durable lifecycle rather than creating two rows.
    kind: str = "core"
    tier: str = ""
    reservation_id: str = ""
    reserved_usd: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if not self.messages:
            raise ValueError("a model request requires at least one message")
        _required(self.output_schema_name, "output_schema_name")
        object.__setattr__(self, "output_schema", freeze_data(self.output_schema))
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive when set")
        if self.kind not in ("core", "specialist", "research", "coding"):
            raise ValueError("kind must be core, specialist, research, or coding")
        if self.reserved_usd < 0:
            raise ValueError("reserved_usd must not be negative")
        if self.kind == "research" and not self.tier:
            raise ValueError("research requests require a cognition tier")
        if self.reservation_id and self.kind != "research":
            raise ValueError("only research requests carry reservations")
        if self.affinity_key is not None:
            _required(self.affinity_key, "affinity_key")


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


# A token consumes at least one encoded byte on the supported transports, and
# the fixed allowance covers provider chat framing absent from the neutral
# request. Both make this an upper bound rather than an estimate, which is what
# a spending ceiling needs: under-counting would let a request cost more than
# its reservation.
PROTOCOL_INPUT_TOKEN_ALLOWANCE = 512


# The context window, in tokens, of each Core model as the runtime reaches it.
# The one record of how much a Core request may hold: a bound on Core input is
# derived from here and nowhere else, so the Core that answers Friedl and the
# instance that answers an unprompted turn cannot come to disagree about the
# model they share.
#
# A fact about the model and the transport together, recorded deliberately and
# never inferred from a model's name: an unlisted model has no window here, and
# anything that needs one refuses rather than guesses.
CORE_CONTEXT_WINDOW_TOKENS: dict[tuple[str, str], int] = {
    # Claude Opus 5.5 is documented with a 1,000,000-token window. The
    # subscription reaches it through the Claude Code CLI as `--model
    # claude-opus-5-5`, without the `[1m]` opt-in to the long window, so the
    # standard 200,000-token window is recorded: the smaller of the two, and
    # the one this transport is not known to exceed.
    ("claude_subscription", "claude-opus-5-5"): 200_000,
}


def core_input_ceiling(provider: str, model: str, output_reserve: int) -> int | None:
    """The most input a Core request to this model may carry, or None.

    The model's context window less a reserve kept free for what the model
    writes back: a request that filled the window would leave no room for its
    own answer. Compared against `input_token_upper_bound`, which counts every
    encoded byte and so over-states tokens, which keeps the check on the safe
    side. None for a model with no recorded window.
    """
    if output_reserve <= 0:
        raise ValueError("output_reserve must be positive")
    window = CORE_CONTEXT_WINDOW_TOKENS.get((provider.strip().lower(), model.strip()))
    if window is None or window <= output_reserve:
        return None
    return window - output_reserve


def input_token_upper_bound(request: "ModelRequest") -> int:
    """Conservatively bound the complete request, including schema and framing."""
    import json

    def _plain(value):
        if isinstance(value, Mapping):
            return {key: _plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_plain(item) for item in value]
        return value

    wire = {
        "messages": [
            {"role": item.role.value, "content": item.content}
            for item in request.messages
        ],
        "output_schema_name": request.output_schema_name,
        "output_schema": _plain(request.output_schema),
    }
    encoded = json.dumps(
        wire, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return len(encoded) + PROTOCOL_INPUT_TOKEN_ALLOWANCE
