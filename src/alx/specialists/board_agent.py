"""The board agent: does one KiCad board job for AL/X in one continuous session (D-083).

AL/X hands it the whole job. It works as an engineer would, making the layout
choices itself, in big steps, in Friedl's order (D-082). At the end of each
step it reports and waits, its session paused, until AL/X passes on what
Friedl said; then it carries on where it was. Its calls go through her broker
and gate with none of her approvals: it cannot approve a step or write to his
project. A job's journal (the brief, every report and every message) is kept,
so a job whose session has ended carries on from it in a new one.

The loop here is mechanical: relay calls, pause at a report, resume with a
message. Nothing here reads what a brief, a report or a message says.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any

from alx.contracts import (
    CapabilityAttempt, CapabilityCall, CapabilityDefinition, ModelMessage, ModelRequest, ModelRole,
    ReasoningModel,
)
from alx.specialists.worker import MechanicalWorker, WorkerError, _catalogue, _plain, worker_tools

REPORT_TOOL = "report_step"

AGENT_INSTRUCTIONS = """You are AL/X's board agent. AL/X works with Friedl, an electronics \
engineer, and has handed you one KiCad board job: an edit of one of his boards, or a new board. \
You do the job as a careful engineer would, and the layout choices are yours to make.
- Work in his order: the schematic first, then the placement of the parts, then the routing. \
At the end of each step call report_step with status step_ready, and wait: his answer comes \
back as the result of that call, through AL/X. The tools refuse work that gets ahead of his \
approvals, so never try to.
- Work in big steps: one edit script does a whole change, using the tested helpers. Render \
your work with the kicad render action and look at it before you report; a clean DRC is not \
enough. Judge by compared_to_original: nothing new against his original, nothing unconnected, \
and the schematic and board agreeing pin by pin.
- The datasheet is the reference for every part: pins, unused and NC pins, ground pins and \
land patterns. Read it before you decide.
- Place parts from where their pins point, so signals run short and straight. RF traces: \
straight and short where possible, curved bends (add_track radius), P and N matched \
(track_length).
- When something you need is not in his drawings or the datasheets (which connector pins or \
channels are used, whether a firmware change is acceptable), call report_step with status \
question and ask, rather than guess. Do not ask what a datasheet answers.
- You cannot approve a step, open windows on his screen or write to his project: AL/X does \
those with him. Name in each report the work_copy_id and the revision he should see.
- Report briefly and plainly: what you changed, how you checked it, what is open.
"""

REPORT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["step_ready", "question", "done", "blocked"]},
        "step": {"type": "string", "enum": ["schematic", "placement", "routing", "none"]},
        "summary": {"type": "string"},
        "work_copy_id": {"type": "string"},
        "work_copy_revision": {"type": "string"},
        "checks": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "open_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "step", "summary", "work_copy_id", "work_copy_revision", "checks",
                 "questions", "open_points"],
    "additionalProperties": False,
}


def agent_tools() -> tuple[dict[str, Any], ...]:
    return (*worker_tools(), {
        "name": REPORT_TOOL,
        "description": "Report the end of a step, a question, or the end of the job, and wait for "
                       "Friedl's answer through AL/X, which comes back as this call's result.",
        "inputSchema": REPORT_SCHEMA,
    })


class BoardJobAgent(MechanicalWorker):
    """One board job at a time, paused between steps, resumed with AL/X's message."""

    def __init__(
        self,
        model: ReasoningModel,
        dispatch: Callable[[CapabilityCall], CapabilityAttempt],
        definitions: Sequence[CapabilityDefinition],
        journal_root: Path,
        *,
        max_seconds: float = 1800.0,
        image_source: Callable[[str], bytes | None] | None = None,
    ) -> None:
        super().__init__(model, dispatch, definitions, max_seconds=max_seconds, image_source=image_source)
        self._journal_root = Path(journal_root)
        # The job whose session is paused on a report, if any.
        self._paused: str | None = None

    def run(self, brief: str, job_id: str, max_calls: int) -> dict[str, Any]:  # noqa: D102
        return self.work(job_id=None, brief=brief, message=None, max_calls=max_calls)

    def work(self, *, job_id: str | None, brief: str | None, message: str | None,
             max_calls: int) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise WorkerError("worker_busy")
        try:
            return self._work(job_id, brief, message, max_calls)
        finally:
            self._busy.release()

    def _journal_path(self, job_id: str) -> Path:
        return self._journal_root / (job_id + ".json")

    def _journal(self, job_id: str) -> dict[str, Any]:
        path = self._journal_path(job_id)
        if not path.is_file():
            raise WorkerError("job_unknown")
        return json.loads(path.read_text())

    def _save(self, job_id: str, journal: Mapping[str, Any]) -> None:
        self._journal_root.mkdir(parents=True, exist_ok=True)
        target = self._journal_path(job_id).with_suffix(".tmp")
        target.write_text(json.dumps(journal, ensure_ascii=False))
        target.replace(self._journal_path(job_id))

    def _request(self, job_id: str, journal: Mapping[str, Any]) -> ModelRequest:
        system = (f"{AGENT_INSTRUCTIONS}\nCapabilities you may call:\n"
                  f"{_catalogue(tuple(self._definitions.values()))}")
        messages = [ModelMessage(ModelRole.SYSTEM, system), ModelMessage(ModelRole.USER, journal["brief"])]
        if journal["events"]:
            # Only a new session reads this: a paused one already holds it.
            messages.append(ModelMessage(ModelRole.USER, json.dumps({
                "transport_fact": "earlier_in_this_job",
                "detail": "Your earlier session of this job ended. This is what you reported and what "
                          "AL/X answered, in order. Check your work copy's current state before you "
                          "carry on.",
                "events": journal["events"]}, ensure_ascii=False)))
        return ModelRequest(tuple(messages), "board_report", REPORT_SCHEMA, affinity_key=job_id,
                            kind="board_agent", tools=agent_tools())

    def _work(self, job_id: str | None, brief: str | None, message: str | None,
              max_calls: int) -> dict[str, Any]:
        if job_id is None:
            job_id = uuid.uuid4().hex[:12]
            journal = {"brief": brief, "events": []}
            self._save(job_id, journal)
            resumed = False
        else:
            journal = self._journal(job_id)
            resumed = True
        request = self._request(job_id, journal)
        continued = resumed and self._paused == job_id
        if resumed:
            journal["events"].append({"alx": message})
            self._save(job_id, journal)
        if not continued:
            # Another job's paused session, or none: this one starts afresh.
            self._abandon()
            self._paused = None
        started = monotonic()
        calls: list[dict[str, Any]] = []
        returned: list[dict[str, Any]] = []
        try:
            if continued:
                completion = self._model.complete(replace(
                    request, tool_result=json.dumps({"from_alx": message}, ensure_ascii=False)))
            else:
                if resumed:
                    request = self._request(job_id, journal)
                completion = self._model.complete(request)
            self._paused = None
            while completion.tool_use is not None:
                if completion.tool_use.get("name") == REPORT_TOOL:
                    report = _plain(completion.tool_use.get("input"))
                    if not isinstance(report, dict) or report.get("status") not in (
                            "step_ready", "question", "done", "blocked"):
                        raise WorkerError("report_malformed", returned)
                    journal["events"].append({"report": report})
                    self._save(job_id, journal)
                    # The session waits on this call for AL/X's answer.
                    self._paused = job_id
                    return {"job_id": job_id, "status": report["status"], "report": report,
                            "calls": calls, "session": "continued" if continued else "new"}
                if len(calls) >= max_calls or monotonic() - started > self._max_seconds:
                    reason = "call_limit" if len(calls) >= max_calls else "time_limit"
                    self._abandon()
                    journal["events"].append({"stopped": reason, "calls": len(calls)})
                    self._save(job_id, journal)
                    return {"job_id": job_id, "status": "stopped", "stop_reason": reason, "calls": calls,
                            "session": "continued" if continued else "new"}
                answer, summary, images = self._call(completion.tool_use, f"{job_id}-{len(calls) + 1}")
                calls.append(summary)
                returned.append({**summary, "returned": json.loads(answer)})
                completion = self._model.complete(replace(request, tool_result=answer, tool_images=images))
        except WorkerError:
            self._abandon()
            raise
        except Exception as error:  # noqa: BLE001 - reported with what was completed
            self._abandon()
            raise WorkerError("worker_failed", returned) from error
        # The session ended without a report call: its final answer is the report.
        report = _plain(completion.output)
        if not isinstance(report, dict) or report.get("status") not in ("step_ready", "question", "done", "blocked"):
            raise WorkerError("report_malformed", returned)
        journal["events"].append({"report": report})
        self._save(job_id, journal)
        return {"job_id": job_id, "status": report["status"], "report": report, "calls": calls,
                "session": "continued" if continued else "new"}


__all__ = ["AGENT_INSTRUCTIONS", "BoardJobAgent", "REPORT_SCHEMA", "REPORT_TOOL", "agent_tools"]
