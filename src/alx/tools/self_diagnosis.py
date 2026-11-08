"""AL/X reads her own execution trace, runtime log and session records (D-050).

Three read-only primitives. They report what happened; deciding what it means,
and whether and how to put it right, is hers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from alx.contracts.diagnostics import mask_credentials

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)

READ_OWN_TRACE = "read_own_trace"
READ_OWN_LOG = "read_own_log"
READ_OWN_SESSIONS = "read_own_sessions"
_SESSIONS_MAX = 20
_SESSIONS_DEFAULT = 5
_MAX = 500
_DEFAULT_WINDOW = timedelta(hours=24)
_FAILURES = ("arguments_unusable", "unavailable")

_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_OBJECTS = StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT))
_RESULT = StructuredSchema(
    ValueKind.OBJECT, {"entries": _OBJECTS, "truncated": _BOOLEAN},
    ("entries", "truncated"), extra_properties=False)

TRACE_DEFINITION = CapabilityDefinition(
    READ_OWN_TRACE,
    "Read your own execution trace: every step you took, as shown on Friedl's screen "
    "(subsystem, status, label, capability or goal reference, reason code, duration). "
    "Each step's time is local, with its offset. Also returns reasoning_usage: "
    "per-call input/output/cache tokens and cost_usd, plus full-window totals; "
    "null means unknown, not zero. Usage is for the time window across conversations, "
    "independent of trace filters. "
    "A usage read failure is reported in reasoning_usage.failure; trace entries remain available. "
    "Window from/to, ISO 8601 (a time without an offset is local); default the "
    "last 24 hours, kept for 30 days. "
    "problems_only returns only failed and refused steps; subsystem narrows to one "
    "(core, mail, notebook, research, readers, xero, coding, review ...). Up to 500, "
    "oldest first; truncated says when more exist.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"from": _STRING, "to": _STRING, "problems_only": _BOOLEAN,
         "subsystem": _STRING, "limit": _INTEGER},
        (), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT,
        {"entries": _OBJECTS, "truncated": _BOOLEAN,
         "reasoning_usage": StructuredSchema(ValueKind.OBJECT)},
        ("entries", "truncated"), extra_properties=False),
    SideEffect.NONE,
    _FAILURES,
)

LOG_DEFINITION = CapabilityDefinition(
    READ_OWN_LOG,
    "Read your own runtime log: what this process did and any warnings, errors and "
    "tracebacks, including launcher output before logging starts. Logs are kept "
    "for 30 days. Window from/to uses ISO 8601; a time without an offset is local. "
    "The default window is the last 24 hours. Untimestamped process output uses "
    "the file modification time, labelled timestamp_source=file_mtime. "
    "Process output is shown at ERROR level. "
    "minimum_level is INFO, WARNING or ERROR (default "
    "WARNING); contains keeps entries with that text. Anything shaped like a key is "
    "masked. Up to 500 entries, the newest kept; truncated says when more matched.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"from": _STRING, "to": _STRING, "minimum_level": _STRING,
         "contains": _STRING, "limit": _INTEGER},
        (), extra_properties=False),
    _RESULT,
    SideEffect.NONE,
    _FAILURES,
)

SESSIONS_DEFINITION = CapabilityDefinition(
    READ_OWN_SESSIONS,
    "Read your own reasoning sessions as recorded: what started each one (Friedl's "
    "words or an event), every tool and capability you called with its result - "
    "including each change of a goal's status, with its stop reason - and what you "
    "answered. This is the full history; read_own_goals shows only a goal's latest "
    "state. Use it to find out why something you did happened. Window from/to, ISO "
    "8601 (a time without an offset is local); default the last 24 hours, kept for "
    "7 days. goal_id or contains keeps sessions that mention it. Default 5 sessions, "
    "up to 20, the newest kept; truncated says when more matched.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"from": _STRING, "to": _STRING, "goal_id": _STRING, "contains": _STRING,
         "limit": _INTEGER},
        (), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT, {"sessions": _OBJECTS, "truncated": _BOOLEAN},
        ("sessions", "truncated"), extra_properties=False),
    SideEffect.NONE,
    _FAILURES,
)

DEFINITIONS = (TRACE_DEFINITION, LOG_DEFINITION, SESSIONS_DEFINITION)


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = mask_credentials(" ".join(text.split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result_summary(raw: Any) -> Any:
    """The part of a tool result that says what happened, before any detail."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return _short(raw, 300)
    if isinstance(value, Mapping) and isinstance(value.get("goal_updated"), Mapping):
        goal = value["goal_updated"]
        return {key: goal.get(key) for key in ("goal_id", "status", "stop_reason", "blockers")}
    if isinstance(value, Mapping) and "result_state" in value:
        return {"state": value.get("result_state") or value.get("disposition"),
                "failure": value.get("failure") or value.get("reason_code"),
                "values": _short(value.get("result_values"), 240)}
    return _short(value, 300)


def _session_summary(path: Path) -> dict[str, Any] | None:
    """One recorded session, reduced to its trigger, steps and answer."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return None
    header = json.loads(lines[0])
    trigger: dict[str, Any] = {}
    try:
        state = json.loads(header.get("user_prompt") or "{}")
        current = state.get("current_trigger") or {}
        trigger = {"kind": current.get("kind")}
        conversation = state.get("conversation") or []
        if current.get("kind") == "conversation_turn" and conversation:
            trigger["said"] = _short(conversation[-1].get("content", ""), 400)
        elif current.get("reference") is not None:
            trigger["reference"] = _short(current.get("reference"), 200)
    except (ValueError, AttributeError, TypeError):
        trigger = {}
    steps: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    answer: Any = None
    for line in lines[1:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        content = (event.get("message") or {}).get("content") if isinstance(event, Mapping) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "tool_use":
                name = str(item.get("name", "")).removeprefix("mcp__alx__")
                arguments = item.get("input") or {}
                if name == "StructuredOutput":
                    action = arguments.get("action") if isinstance(arguments, Mapping) else None
                    answer = {"type": (action or {}).get("type"),
                              "response": _short((action or {}).get("response", ""), 600),
                              "detail": _short((action or {}).get("detail", ""), 1200)}
                    continue
                step = {"tool": name}
                if isinstance(arguments, Mapping) and arguments.get("capability_id"):
                    step["capability"] = arguments["capability_id"]
                    arguments = arguments.get("arguments_json", "")
                step["arguments"] = _short(arguments, 200)
                pending[str(item.get("id"))] = step
                steps.append(step)
            elif item.get("type") == "tool_result":
                step = pending.get(str(item.get("tool_use_id")))
                if step is None:
                    continue
                result = item.get("content")
                if isinstance(result, list):
                    result = " ".join(part.get("text", "") for part in result if isinstance(part, Mapping))
                step["result"] = _result_summary(result)
    return {"session": f"{path.parent.name}/{path.name}", "started_at": header.get("started_at"),
            "ended_at": header.get("ended_at"), "purpose": header.get("purpose"),
            "outcome": header.get("outcome"), "trigger": trigger, "steps": steps,
            "answer": answer}


def _window(arguments: Mapping[str, Any], now: datetime) -> tuple[datetime, datetime, int]:
    def moment(name: str) -> datetime | None:
        value = arguments.get(name)
        if value in (None, ""):
            return None
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.astimezone()

    end = moment("to") or now
    start = moment("from") or (end - _DEFAULT_WINDOW)
    if end <= start:
        raise ValueError("to must be later than from")
    limit = int(arguments.get("limit", _MAX))
    if not 0 < limit <= _MAX:
        raise ValueError(f"limit must be 1 to {_MAX}")
    return start, end, limit


def build_self_diagnosis_executors(
    trace: Any,
    read_log: Callable[..., tuple[list[dict], bool]],
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
    read_usage: Callable[..., dict[str, Any]] | None = None,
    sessions_dir: Path | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def failed(name: str, code: str, reason: str = "") -> CapabilityResult:
        failure = {"code": code}
        if reason:
            failure["reason"] = reason[:300]
        return CapabilityResult(call_id_source(), name, CapabilityResultState.FAILED,
                                failure=failure)

    def read_trace(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            start, end, limit = _window(arguments, now())
            subsystem = str(arguments.get("subsystem") or "").strip().lower()
            problems = bool(arguments.get("problems_only", False))
        except (TypeError, ValueError) as error:
            return failed(READ_OWN_TRACE, "arguments_unusable", str(error))
        try:
            entries, truncated = trace.between(start, end, problems, subsystem, limit)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return failed(READ_OWN_TRACE, "unavailable", type(error).__name__)
        values = {"entries": entries, "truncated": truncated}
        if read_usage is not None:
            try:
                usage = read_usage(start, end, limit)
            except Exception as error:  # noqa: BLE001 - preserve the successful trace read
                usage = {"failure": {"code": "unavailable", "reason": type(error).__name__}}
            if usage is not None:
                values["reasoning_usage"] = usage
        return CapabilityResult(call_id_source(), READ_OWN_TRACE, CapabilityResultState.SUCCEEDED,
                                values)

    def read_own_log(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            start, end, limit = _window(arguments, now())
            level = str(arguments.get("minimum_level") or "WARNING").upper()
            if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                raise ValueError("minimum_level must be INFO, WARNING or ERROR")
            contains = str(arguments.get("contains") or "")
        except (TypeError, ValueError) as error:
            return failed(READ_OWN_LOG, "arguments_unusable", str(error))
        try:
            entries, truncated = read_log(start, end, level, contains, limit)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return failed(READ_OWN_LOG, "unavailable", type(error).__name__)
        return CapabilityResult(call_id_source(), READ_OWN_LOG, CapabilityResultState.SUCCEEDED,
                                {"entries": entries, "truncated": truncated})

    def read_own_sessions(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            start, end, _ = _window(arguments, now())
            limit = int(arguments.get("limit", _SESSIONS_DEFAULT))
            if not 0 < limit <= _SESSIONS_MAX:
                raise ValueError(f"limit must be 1 to {_SESSIONS_MAX}")
            needles = [str(arguments[name]) for name in ("goal_id", "contains")
                       if str(arguments.get(name) or "").strip()]
        except (TypeError, ValueError) as error:
            return failed(READ_OWN_SESSIONS, "arguments_unusable", str(error))
        if sessions_dir is None:
            return failed(READ_OWN_SESSIONS, "unavailable", "no session records are kept")
        try:
            matched: list[Path] = []
            for folder in sorted(sessions_dir.glob("????-??-??")):
                for path in sorted(folder.glob("*.jsonl")):
                    stamp = datetime.strptime(
                        f"{folder.name} {path.name[:6]}", "%Y-%m-%d %H%M%S").replace(tzinfo=UTC)
                    if not start <= stamp <= end:
                        continue
                    if needles:
                        raw = path.read_text(encoding="utf-8", errors="replace")
                        if not all(needle in raw for needle in needles):
                            continue
                    matched.append(path)
            kept = matched[-limit:]
            sessions = [summary for summary in map(_session_summary, kept) if summary]
        except (OSError, ValueError) as error:
            return failed(READ_OWN_SESSIONS, "unavailable", type(error).__name__)
        return CapabilityResult(call_id_source(), READ_OWN_SESSIONS, CapabilityResultState.SUCCEEDED,
                                {"sessions": sessions, "truncated": len(matched) > len(kept)})

    return {READ_OWN_TRACE: read_trace, READ_OWN_LOG: read_own_log,
            READ_OWN_SESSIONS: read_own_sessions}


__all__ = ["DEFINITIONS", "READ_OWN_LOG", "READ_OWN_SESSIONS", "READ_OWN_TRACE",
           "build_self_diagnosis_executors"]
