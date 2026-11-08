"""AL/X reads her own execution trace, runtime log and session records (D-050).

Three read-only primitives. They report what happened; deciding what it means,
and whether and how to put it right, is hers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from alx.contracts.diagnostics import mask_credentials
from alx.contracts.mail import MailReference
from alx.contracts.provenance import ContentOrigin, ContentProvenance

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
_SESSION_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}/\d{6}-[0-9a-f]{8}\.jsonl$")
# Text kept per field: a list of sessions is brief; one named session is read
# in full, so a long detail can be read back (with read_full_result beyond the
# per-string bound).
_BRIEF = {"said": 400, "reference": 200, "arguments": 200, "result": 300,
          "values": 240, "response": 600, "detail": 1200}
_FULL = {name: 20000 for name in _BRIEF}
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
    "up to 20, the newest kept; truncated says when more matched. Each is brief; "
    "session (its name, as returned) reads that one session in full, including the "
    "whole detail of your answer.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"from": _STRING, "to": _STRING, "goal_id": _STRING, "contains": _STRING,
         "limit": _INTEGER, "session": _STRING},
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


def _masked(value: Any) -> Any:
    """A structured value with anything key-shaped masked, structure kept."""
    if value is None:
        return None
    try:
        return json.loads(mask_credentials(json.dumps(value, ensure_ascii=False, default=str)))
    except ValueError:
        return _short(value, 300)


def _result_summary(raw: Any, limits: Mapping[str, int]) -> Any:
    """The part of a tool result that says what happened, before any detail."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return _short(raw, limits["result"])
    if isinstance(value, Mapping) and isinstance(value.get("goal_updated"), Mapping):
        goal = value["goal_updated"]
        return {key: _masked(goal.get(key)) for key in ("goal_id", "status", "stop_reason", "blockers")}
    if isinstance(value, Mapping) and "result_state" in value:
        return {"state": value.get("result_state") or value.get("disposition"),
                "failure": _masked(value.get("failure") or value.get("reason_code")),
                "values": _short(value.get("result_values"), limits["values"])}
    return _short(value, limits["result"])


def _collect_mail_references(value: Any, found: set[MailReference]) -> None:
    """Every typed mail bookmark a record carries, wherever it sits in it."""
    if isinstance(value, str):
        if value[:1] in "{[":
            try:
                _collect_mail_references(json.loads(value), found)
            except ValueError:
                pass
        return
    if isinstance(value, Mapping):
        parts = [value.get(name) for name in ("mailbox_id", "uid_validity", "uid")]
        if all(isinstance(part, str) and part for part in parts):
            found.add(MailReference(*parts))
        for item in value.values():
            _collect_mail_references(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_mail_references(item, found)


_WITHHELD = "[withheld: drawn from mail content that has expired]"


def _withheld(summary: dict[str, Any]) -> dict[str, Any]:
    """A session whose context included expired mail content keeps its shape
    - which calls, which states, which goal statuses - but none of its words."""
    steps = []
    for step in summary["steps"]:
        result = step.get("result")
        if isinstance(result, Mapping) and "state" in result:
            result = {**result, "values": _WITHHELD}
        elif not (isinstance(result, Mapping) and "status" in result):
            result = _WITHHELD
        steps.append({**step, "arguments": _WITHHELD, "result": result})
    answer = summary["answer"]
    if isinstance(answer, Mapping):
        answer = {**answer, "response": _WITHHELD, "detail": _WITHHELD}
    trigger = {key: (_WITHHELD if key in ("said", "reference") else value)
               for key, value in summary["trigger"].items()}
    return {**summary, "trigger": trigger, "steps": steps, "answer": answer, "withheld": True}


def _session_summary(path: Path, limits: Mapping[str, int] = _BRIEF,
                     ) -> tuple[dict[str, Any], set[MailReference], list[tuple[str, str]]] | None:
    """One recorded session, reduced to its trigger, steps and answer, with the
    mail bookmarks in it and the conversation turns it saw, so mail-derived
    content keeps its D-013 expiry."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return None
    header = json.loads(lines[0])
    mail: set[MailReference] = set()
    _collect_mail_references(header.get("user_prompt"), mail)
    seen: list[tuple[str, str]] = []
    trigger: dict[str, Any] = {}
    try:
        state = json.loads(header.get("user_prompt") or "{}")
        current = state.get("current_trigger") or {}
        trigger = {"kind": current.get("kind")}
        conversation = state.get("conversation") or []
        seen = [(str(turn.get("conversation_id")), str(turn.get("turn_id")))
                for turn in conversation
                if isinstance(turn, Mapping) and turn.get("conversation_id") and turn.get("turn_id")]
        if current.get("kind") == "conversation_turn" and conversation:
            trigger["said"] = _short(conversation[-1].get("content", ""), limits["said"])
        elif current.get("reference") is not None:
            trigger["reference"] = _short(current.get("reference"), limits["reference"])
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
        _collect_mail_references(event, mail)
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
                              "response": _short((action or {}).get("response", ""), limits["response"]),
                              "detail": _short((action or {}).get("detail", ""), limits["detail"])}
                    continue
                step = {"tool": name}
                if isinstance(arguments, Mapping) and arguments.get("capability_id"):
                    step["capability"] = arguments["capability_id"]
                    arguments = arguments.get("arguments_json", "")
                step["arguments"] = _short(arguments, limits["arguments"])
                pending[str(item.get("id"))] = step
                steps.append(step)
            elif item.get("type") == "tool_result":
                step = pending.get(str(item.get("tool_use_id")))
                if step is None:
                    continue
                result = item.get("content")
                if isinstance(result, list):
                    result = " ".join(part.get("text", "") for part in result if isinstance(part, Mapping))
                step["result"] = _result_summary(result, limits)
    return ({"session": f"{path.parent.name}/{path.name}", "started_at": header.get("started_at"),
             "ended_at": header.get("ended_at"), "purpose": header.get("purpose"),
             "outcome": header.get("outcome"), "trigger": trigger, "steps": steps,
             "answer": answer}, mail, seen)


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
    session_days: int = 7,
    turn_provenance: Callable[[list[tuple[str, str]]],
                              Mapping[tuple[str, str], tuple[bool, ContentProvenance | None]]] | None = None,
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
            named = str(arguments.get("session") or "").strip()
            if named and not _SESSION_NAME.match(named):
                raise ValueError("session must be a name read_own_sessions returned")
        except (TypeError, ValueError) as error:
            return failed(READ_OWN_SESSIONS, "arguments_unusable", str(error))
        if sessions_dir is None:
            return failed(READ_OWN_SESSIONS, "unavailable", "no session records are kept")

        def recorded(path: Path) -> datetime:
            # Named for when the session ended; the record is deleted after
            # session_days, which bounds anything derived from it.
            return datetime.strptime(
                f"{path.parent.name} {path.name[:6]}", "%Y-%m-%d %H%M%S").replace(tzinfo=UTC)

        try:
            if named:
                path = sessions_dir / named
                if not path.is_file():
                    return failed(READ_OWN_SESSIONS, "arguments_unusable", "no such session")
                matched = kept = [path]
                limits = _FULL
            else:
                matched = []
                for folder in sorted(sessions_dir.glob("????-??-??")):
                    for path in sorted(folder.glob("*.jsonl")):
                        if not start <= recorded(path) <= end:
                            continue
                        if needles:
                            raw = path.read_text(encoding="utf-8", errors="replace")
                            if not all(needle in raw for needle in needles):
                                continue
                        matched.append(path)
                kept = matched[-limit:]
                limits = _BRIEF
            moment = now()
            sessions: list[dict[str, Any]] = []
            mail: set[MailReference] = set()
            expiries: list[datetime] = []
            reads = [(path, _session_summary(path, limits)) for path in kept]
            # Every turn the kept sessions saw, looked up together.
            looked_up = {} if turn_provenance is None else turn_provenance(
                sorted({key for _, read in reads if read is not None for key in read[2]}))
            for path, read in reads:
                if read is None:
                    continue
                summary, references, seen = read
                # The turns a session saw carry their own D-013 sources in the
                # conversation store; its words inherit them. A turn gone or
                # expired means its content has expired, so the session's
                # words are withheld and only its shape remains.
                expired = False
                for key in (seen if turn_provenance is not None else ()):
                    found, source = looked_up.get(key, (False, None))
                    if not found or (source is not None and source.is_expired(moment)):
                        expired = True
                    elif source is not None and source.governed_by_retention():
                        references |= set(source.mail_references)
                        expiries.append(source.content_expires_at)
                if expired:
                    sessions.append(_withheld(summary))
                    continue
                sessions.append(summary)
                if references:
                    mail.update(references)
                    expiries.append(recorded(path) + timedelta(days=session_days))
        except (OSError, ValueError) as error:
            return failed(READ_OWN_SESSIONS, "unavailable", type(error).__name__)
        # A session that read mail may quote it. Whatever she derives from it
        # carries those bookmarks and ends no later than the record does (D-013).
        provenance = None if not mail else ContentProvenance(
            origins=frozenset({ContentOrigin.MAIL_MESSAGE, ContentOrigin.ALX}),
            recorded_at=moment,
            mail_references=tuple(sorted(mail, key=lambda item: (item.mailbox_id, item.uid_validity, item.uid))),
            content_expires_at=max(moment, min(expiries)) if expiries else moment,
        )
        truncated = len(matched) > len(kept)
        return CapabilityResult(
            call_id_source(), READ_OWN_SESSIONS, CapabilityResultState.SUCCEEDED,
            {"sessions": sessions, "truncated": truncated},
            # The words stay in the session records; the goal's audit keeps
            # only which sessions were read.
            durable_values={"sessions": [item["session"] for item in sessions],
                            "truncated": truncated},
            provenance=provenance)

    return {READ_OWN_TRACE: read_trace, READ_OWN_LOG: read_own_log,
            READ_OWN_SESSIONS: read_own_sessions}


__all__ = ["DEFINITIONS", "READ_OWN_LOG", "READ_OWN_SESSIONS", "READ_OWN_TRACE",
           "build_self_diagnosis_executors"]
