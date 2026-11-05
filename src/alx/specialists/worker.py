"""A worker that carries out one brief of mechanical work for AL/X (D-070).

AL/X writes the brief: what to run or measure, on which of her copies, what
counts as checked, and when to stop. The worker runs on a lighter model with
its own small context. It calls only the capabilities it is given, each
through the same broker and safety gate as her own calls, and reports once.
It decides no meaning: results, how each was checked, and anything it could
not settle go back to her, and she decides what they mean.

The loop is mechanical: send the brief, relay each capability call, answer
with its bounded outcome, stop at the report or a bound. Nothing here reads
what the brief or a result says.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from threading import Lock
from time import monotonic
from typing import Any

from alx.contracts import (
    CapabilityAttempt, CapabilityCall, CapabilityDefinition, ModelMessage, ModelRequest,
    ModelRole, ReasoningModel,
)
from alx.contracts.result_summary import bound_result

CALL_TOOL = "call_capability"

# The worker's own rules. They describe its role, never a domain.
WORKER_INSTRUCTIONS = """You are a worker carrying out one brief for AL/X, an assistant who \
works with Friedl, an electronics engineer. AL/X wrote the brief. You do mechanical work \
only: run, measure, read, edit her work copies and check results exactly as the brief says.

- Use only the capabilities listed below, through the call_capability tool. Each call's \
outcome comes back to you; a refused call says why. You have no approvals: anything that \
needs one is refused, and you report it rather than work around it.
- Check every result the way the brief says before you report it. Say how you checked it.
- Do not decide what results mean for Friedl or for the design, and do not choose between \
options that need judgement. Put those, and anything you could not settle, in open_points.
- Stop when the brief's stop condition is met, or when you cannot go further.
- Report once, in the report format. Be concise: values, units, how checked, open points.
"""

REPORT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "stopped", "blocked"]},
        "results": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"type": "string"},
                "how_checked": {"type": "string"},
            },
            "required": ["name", "value", "how_checked"],
            "additionalProperties": False,
        }},
        "open_points": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "results", "open_points", "files"],
    "additionalProperties": False,
}


def worker_tools() -> tuple[dict[str, Any], ...]:
    return ({
        "name": CALL_TOOL,
        "description": "Call one listed capability and see its outcome.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability_id": {"type": "string",
                                  "description": "One id from the list, exactly."},
                "arguments_json": {"type": "string",
                                   "description": "The call's arguments as a JSON object."},
            },
            "required": ["capability_id", "arguments_json"],
            "additionalProperties": False,
        },
    },)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _schema(schema: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": schema.kind.value}
    if schema.properties:
        payload["properties"] = {key: _schema(value) for key, value in schema.properties.items()}
    if schema.required:
        payload["required"] = list(schema.required)
    if schema.items is not None:
        payload["items"] = _schema(schema.items)
    if not schema.extra_properties:
        payload["extra_properties"] = False
    return payload


def _catalogue(definitions: Sequence[CapabilityDefinition]) -> str:
    return json.dumps([{
        "id": item.capability_id,
        "purpose": item.purpose,
        "input_schema": _schema(item.input_schema),
        "failure_codes": sorted(item.possible_failure_codes),
    } for item in definitions], ensure_ascii=False, sort_keys=True)


class WorkerError(Exception):
    """The worker could not run, or ended without a usable report."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MechanicalWorker:
    """Carries out one brief at a time on its own model session."""

    def __init__(
        self,
        model: ReasoningModel,
        dispatch: Callable[[CapabilityCall], CapabilityAttempt],
        definitions: Sequence[CapabilityDefinition],
        *,
        max_seconds: float = 1200.0,
    ) -> None:
        if not definitions:
            raise ValueError("a worker needs at least one capability")
        self._model = model
        self._dispatch = dispatch
        self._definitions = {item.capability_id: item for item in definitions}
        self._max_seconds = max_seconds
        # One model session per worker; a second brief waits for none, it is refused.
        self._busy = Lock()

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def run(self, brief: str, job_id: str, max_calls: int) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise WorkerError("worker_busy")
        try:
            return self._run(brief, job_id, max_calls)
        finally:
            self._busy.release()

    def _run(self, brief: str, job_id: str, max_calls: int) -> dict[str, Any]:
        system = (f"{WORKER_INSTRUCTIONS}\nCapabilities you may call:\n"
                  f"{_catalogue(tuple(self._definitions.values()))}")
        request = ModelRequest(
            (ModelMessage(ModelRole.SYSTEM, system), ModelMessage(ModelRole.USER, brief)),
            "worker_report", REPORT_SCHEMA, affinity_key=job_id, kind="worker",
            tools=worker_tools(),
        )
        started = monotonic()
        calls: list[dict[str, Any]] = []
        completion = self._model.complete(request)
        while completion.tool_use is not None:
            if len(calls) >= max_calls or monotonic() - started > self._max_seconds:
                self._abandon()
                return {"status": "stopped",
                        "stop_reason": "call_limit" if len(calls) >= max_calls else "time_limit",
                        "report": None, "calls": calls}
            answer, summary = self._call(completion.tool_use, f"{job_id}-{len(calls) + 1}")
            calls.append(summary)
            completion = self._model.complete(replace(request, tool_result=answer))
        output = _plain(completion.output)
        if not isinstance(output, dict) or output.get("status") not in ("done", "stopped", "blocked"):
            raise WorkerError("report_malformed")
        return {"status": output["status"], "stop_reason": None, "report": output, "calls": calls}

    def _call(self, tool_use: Mapping[str, Any], call_id: str) -> tuple[str, dict[str, Any]]:
        arguments = tool_use.get("input")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        capability_id = arguments.get("capability_id")
        summary: dict[str, Any] = {"call_id": call_id, "capability_id": capability_id}
        if tool_use.get("name") != CALL_TOOL or capability_id not in self._definitions:
            summary["outcome"] = "not_listed"
            return json.dumps({"refused": "capability_not_listed",
                               "listed": list(self.capability_ids)}), summary
        try:
            parsed = json.loads(arguments.get("arguments_json") or "{}")
            if not isinstance(parsed, dict):
                raise ValueError
        except (TypeError, ValueError):
            summary["outcome"] = "arguments_unusable"
            return json.dumps({"refused": "arguments_json must be a JSON object"}), summary
        attempt = self._dispatch(CapabilityCall(call_id, capability_id, parsed))
        result = attempt.result
        summary["outcome"] = (attempt.reason_code if result is None
                              else result.state.value)
        return json.dumps(bound_result({
            "disposition": attempt.disposition.value,
            "reason_code": attempt.reason_code,
            "values": None if result is None else _plain(result.values),
            "failure": None if result is None or result.failure is None else _plain(result.failure),
        }), ensure_ascii=False, default=str), summary

    def _abandon(self) -> None:
        abandon = getattr(self._model, "abandon_session", None)
        if callable(abandon):
            abandon()


__all__ = ["CALL_TOOL", "MechanicalWorker", "REPORT_SCHEMA", "WorkerError", "worker_tools"]
