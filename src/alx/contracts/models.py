"""Provider-neutral transport records for the configured reasoning model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable, Mapping
from typing import Protocol

from alx.contracts.records import StructuredData, freeze_data
from alx.contracts.trace import ReasoningPurpose


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


_PURPOSES = frozenset(item.value for item in ReasoningPurpose)


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
    # Why the Core made this call, from the fixed operator vocabulary. Rides
    # into telemetry so a console can say what each call was for; never sent
    # to the model and never part of what it reasons over.
    purpose: str = ""
    # Local delivery receipt, never serialized into prompts or telemetry.
    # A transport calls this only once the request has been handed off,
    # including a sent request whose response fails.
    on_dispatched: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    # Local accounting hook: approve and identify a fresh request before a
    # transport retries. Never sent to the model or serialized into telemetry.
    on_retry: Callable[[Exception], ModelRequest] | None = field(default=None, repr=False, compare=False)
    # D-065: the Core's native tools, each as name, description and
    # inputSchema. A request that carries them may pause on a tool call
    # within one reasoning session; a transport only relays them.
    tools: tuple[StructuredData, ...] = ()
    # D-065: continue the paused session with this result for the native call
    # it last returned, instead of starting a new one. The Core composes it.
    tool_result: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(freeze_data(item) for item in self.tools))
        names = [item.get("name") for item in self.tools]
        if any(not isinstance(name, str) or not name for name in names) or len(
                set(names)) != len(names):
            raise ValueError("each native tool has its own name")
        if self.tool_result is not None and not self.tools:
            raise ValueError("a tool result continues a native-tool session")
        if self.purpose and self.purpose not in _PURPOSES:
            raise ValueError("purpose must be a ReasoningPurpose value")
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
    # D-065: the model called one of its native tools and its session is
    # paused for the result: {"name": tool, "input": what the model gave},
    # exactly as given; `output` is then empty. Only the Core interprets it.
    tool_use: StructuredData | None = None

    def __post_init__(self) -> None:
        _required(self.provider, "provider")
        _required(self.model, "model")
        object.__setattr__(self, "output", freeze_data(self.output))
        object.__setattr__(self, "usage", freeze_data(self.usage))
        if self.tool_use is not None:
            object.__setattr__(self, "tool_use", freeze_data(self.tool_use))


class ReasoningModel(Protocol):
    """Replaceable model transport with no ownership of memory or tools.

    A transport may relay a native tool call (D-065) and pause for its result,
    but it never runs a capability: the Core does, on its own path.
    """

    def complete(self, request: ModelRequest) -> ModelCompletion: ...


# A token consumes at least one encoded byte on the supported transports, and
# the fixed allowance covers provider chat framing absent from the neutral
# request. Both make this an upper bound rather than an estimate, which is what
# a spending ceiling needs: under-counting would let a request cost more than
# its reservation.
PROTOCOL_INPUT_TOKEN_ALLOWANCE = 512


@dataclass(frozen=True, slots=True)
class CoreModelLimits:
    """What one Core model accepts and may write back, in tokens."""

    context_window: int
    max_output: int


# The limits of each Core model as the runtime reaches it. The one record of
# how much a Core request may hold: a bound on Core input is derived from here
# and nowhere else, so the Core that answers Friedl and the instance that
# answers an unprompted turn cannot come to disagree about the model they
# share.
#
# A fact about the model and the transport together, recorded from what the
# transport reports and never inferred from a model's name or an older model:
# an unlisted model has no limits here, and anything that needs them refuses
# rather than guesses. If the transport later reports different limits, this
# record is corrected.
CORE_MODEL_LIMITS: dict[tuple[str, str], CoreModelLimits] = {
    # Reported by Claude Code CLI 2.1.281 for the exact production invocation
    # (`--model claude-opus-5-5`, the Core's own flags), observed 2026-09-28:
    # modelUsage["claude-opus-5-5"].contextWindow = 1000000 and
    # .maxOutputTokens = 128000, with no `[1m]` opt-in. The same figures are
    # documented for the model.
    ("claude_subscription", "claude-opus-5-5"): CoreModelLimits(
        context_window=1_000_000, max_output=128_000
    ),
}


def core_input_ceiling(provider: str, model: str) -> int | None:
    """The most input a Core request to this model may carry, or None.

    The model's context window less its maximum output allowance, so a request
    at the ceiling still leaves room for the longest answer the transport
    permits. Compared against `input_token_upper_bound`, which counts every
    encoded byte and so over-states tokens, which keeps the check on the safe
    side. None for a model with no recorded limits.
    """
    limits = CORE_MODEL_LIMITS.get((provider.strip().lower(), model.strip()))
    if limits is None or limits.context_window <= limits.max_output:
        return None
    return limits.context_window - limits.max_output


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
        # The tool definitions travel with the request too.
        "tools": _plain(request.tools),
    }
    encoded = json.dumps(
        wire, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return len(encoded) + PROTOCOL_INPUT_TOKEN_ALLOWANCE
