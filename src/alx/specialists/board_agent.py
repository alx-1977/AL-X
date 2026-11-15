"""The board agent: does one KiCad board job for AL/X, one stretch at a time (D-083).

AL/X hands it the whole job. It works as an engineer would, making the layout
choices itself, in big steps, in Friedl's order (D-082). Each stretch of work
(a step, or his changes to one) runs in a fresh session and ends with a
report. The next stretch starts from the job's journal (the brief, every
report and every answer) and his preferences, not from the whole of the last
session: on 15 Nov one session carried twenty full-page pictures and every
result forward, and its steps re-read 28 million tokens. Its calls go through
her broker and gate with none of her approvals: it cannot approve a step or
write to his project. AL/X can stop a stretch at any time.

The loop here is mechanical: relay calls, end at a report, start the next
stretch from the journal. Nothing here reads what a brief, a report or a
message says.
"""
from __future__ import annotations

import json
import threading
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
RECEIPTS_KEPT = 40


class BoardJobStopped(Exception):
    """Raised where a model call would otherwise be retried after AL/X stopped the job."""

AGENT_INSTRUCTIONS = """You are AL/X's board agent. AL/X works with Friedl, an electronics \
engineer, and has handed you one KiCad board job: an edit of one of his boards, or a new board. \
You do the job as a careful engineer would, and the layout choices are yours to make.
- Work in his order: the schematic first, then the placement of the parts, then the routing. \
Each stretch of your work ends with report_step: status step_ready at the end of a step. AL/X \
shows him that step and starts your next stretch with his answer. The tools refuse work that \
gets ahead of his approvals, so never try to.
- His preferences, below, are how he draws. Follow every one without being told, and check \
your work against them before you report.
- Read before you change, cheaply: kicad inspect_schematic gives every part, pin and net of the \
schematic, kicad inspect the board; the helper manual below says what every edit helper takes \
and does. Never use an edit script to read, print sheet text or look at the helpers' code.
- Work in big steps: one edit script does a whole change, using the tested helpers. To try a \
change before keeping it, run the edit with dry_run: it shows everything the change would do and \
puts the copy back. Never raise an error to undo or to see output: a failed script is a fault, \
not a tool. A script cannot start programs; check your copy with kicad check work_copy_id.
- Look before you report, and look sparingly: every picture stays with you for the rest of the \
stretch. Render once, look at the whole page once, then look only at sections of it.
- Judge by compared_to_original: nothing new against his original, nothing unconnected, and the \
schematic and board agreeing pin by pin. A clean DRC alone is not enough.
- The datasheet is the reference for every part: pins, unused and NC pins, ground pins and \
land patterns. Read it before you decide.
- When something you need is not in his drawings or the datasheets (which connector pins or \
channels are used, whether a firmware change is acceptable), call report_step with status \
question and ask, rather than guess. Do not ask what a datasheet answers.
- Keep to the job: change only what it needs and what he asked for.
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
STATUSES = ("step_ready", "question", "done", "blocked")


def agent_tools() -> tuple[dict[str, Any], ...]:
    return (*worker_tools(), {
        "name": REPORT_TOOL,
        "description": "End this stretch with a report: the end of a step, a question, the end of "
                       "the job, or a dead end. AL/X shows him your work and starts your next stretch.",
        "inputSchema": REPORT_SCHEMA,
    })


class BoardJobAgent(MechanicalWorker):
    """One board job at a time; each stretch a fresh session started from its journal."""

    def __init__(
        self,
        model: ReasoningModel,
        dispatch: Callable[[CapabilityCall], CapabilityAttempt],
        definitions: Sequence[CapabilityDefinition],
        journal_root: Path,
        *,
        preferences: Path | None = None,
        manual: str = "",
        max_seconds: float = 1800.0,
        image_source: Callable[[str], bytes | None] | None = None,
    ) -> None:
        super().__init__(model, dispatch, definitions, max_seconds=max_seconds, image_source=image_source)
        self._journal_root = Path(journal_root)
        self._preferences = preferences
        self._manual = manual
        self._stop = threading.Event()
        self._running: str | None = None

    def run(self, brief: str, job_id: str, max_calls: int) -> dict[str, Any]:  # noqa: D102
        return self.work(job_id=None, brief=brief, message=None, max_calls=max_calls)

    def stop(self, job_id: str | None = None) -> bool:
        """Stop the running stretch (of this job, when named); True when one was running.

        The stop holds against the provider's retry: a model call ended by it
        is never started again (15 Nov: a stopped agent came back as a retry,
        from its first brief, and made a second copy).
        """
        if self._running is None or (job_id is not None and self._running != job_id):
            return False
        self._stop.set()
        # Ends the model session now, rather than after its current step.
        self._abandon()
        return True

    def work(self, *, job_id: str | None, brief: str | None, message: str | None,
             max_calls: int) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise WorkerError("worker_busy")
        try:
            return self._work(job_id, brief, message, max_calls)
        finally:
            self._running = None
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

    def _preferences_text(self) -> str:
        try:
            text = self._preferences.read_text() if self._preferences is not None else ""
        except OSError:
            text = ""
        return text.strip() or "(none recorded yet)"

    def _renewal(self, job_id: str, journal: Mapping[str, Any]):
        """What a retried model call starts from: the journal as it is now, or nothing after a stop."""
        def renew(_failure: Exception) -> ModelRequest:
            if self._stop.is_set():
                raise BoardJobStopped()
            return self._request(job_id, journal)
        return renew

    def _receipt(self, job_id: str, journal: dict[str, Any], summary: Mapping[str, Any], answer: str) -> None:
        """A compact record of one call, kept as it happens, so no session loses the work done."""
        try:
            values = (json.loads(answer).get("values") or {})
        except (ValueError, AttributeError):
            values = {}
        result = values.get("result") if isinstance(values.get("result"), Mapping) else values
        receipt = {"call": summary.get("capability_id"), "outcome": summary.get("outcome")}
        for key in ("action",):
            if isinstance(values.get(key), str):
                receipt[key] = values[key]
        for key in ("work_copy_id", "work_copy_revision", "previewed"):
            if isinstance(result, Mapping) and result.get(key) is not None:
                receipt[key] = result[key]
        if receipt.get("work_copy_id"):
            journal["work_copy"] = {"work_copy_id": receipt["work_copy_id"],
                                    "work_copy_revision": receipt.get("work_copy_revision", "")}
        journal["receipts"] = (journal.get("receipts", []) + [receipt])[-RECEIPTS_KEPT:]
        self._save(job_id, journal)

    def _request(self, job_id: str, journal: Mapping[str, Any]) -> ModelRequest:
        system = (f"{AGENT_INSTRUCTIONS}\nHis preferences:\n{self._preferences_text()}\n\n"
                  f"Helper manual (edit scripts):\n{self._manual or '(see the edit action)'}\n\n"
                  f"Capabilities you may call:\n{_catalogue(tuple(self._definitions.values()))}")
        messages = [ModelMessage(ModelRole.SYSTEM, system), ModelMessage(ModelRole.USER, journal["brief"])]
        if journal["events"] or journal.get("receipts"):
            messages.append(ModelMessage(ModelRole.USER, json.dumps({
                "transport_fact": "earlier_in_this_job",
                "detail": "What you reported in earlier stretches and what AL/X answered, in order, and "
                          "your latest calls; the last event is what to do now. Your work copy holds your "
                          "work so far: carry on with it, never make another, and check its current "
                          "state before you change it.",
                "work_copy": journal.get("work_copy"),
                "events": journal["events"],
                "latest_calls": journal.get("receipts", [])}, ensure_ascii=False)))
        return ModelRequest(tuple(messages), "board_report", REPORT_SCHEMA, affinity_key=job_id,
                            kind="board_agent", tools=agent_tools(),
                            on_retry=self._renewal(job_id, journal))

    def _finish(self, job_id: str, journal: dict[str, Any], calls: list, outcome: dict[str, Any]) -> dict[str, Any]:
        self._save(job_id, journal)
        return {"job_id": job_id, "calls": calls, **outcome}

    def _work(self, job_id: str | None, brief: str | None, message: str | None,
              max_calls: int) -> dict[str, Any]:
        if job_id is None:
            job_id = uuid.uuid4().hex[:12]
            journal = {"brief": brief, "events": []}
        else:
            journal = self._journal(job_id)
            journal["events"].append({"alx": message})
        self._save(job_id, journal)
        self._running = job_id
        self._stop.clear()
        request = self._request(job_id, journal)
        started = monotonic()
        calls: list[dict[str, Any]] = []
        returned: list[dict[str, Any]] = []

        def stopped(reason: str) -> dict[str, Any]:
            self._abandon()
            journal["events"].append({"stopped": reason, "calls": len(calls),
                                      "work_copy": journal.get("work_copy")})
            return self._finish(job_id, journal, calls, {"status": "stopped", "stop_reason": reason})
        try:
            completion = self._model.complete(request)
            while completion.tool_use is not None:
                if self._stop.is_set():
                    return stopped("stopped_by_alx")
                if completion.tool_use.get("name") == REPORT_TOOL:
                    report = _plain(completion.tool_use.get("input"))
                    if not isinstance(report, dict) or report.get("status") not in STATUSES:
                        raise WorkerError("report_malformed", returned)
                    # The stretch ends here: the next starts fresh from the journal.
                    self._abandon()
                    journal["events"].append({"report": report})
                    return self._finish(job_id, journal, calls, {"status": report["status"], "report": report})
                if len(calls) >= max_calls:
                    return stopped("call_limit")
                if monotonic() - started > self._max_seconds:
                    return stopped("time_limit")
                answer, summary, images = self._call(completion.tool_use, f"{job_id}-{len(calls) + 1}")
                calls.append(summary)
                returned.append({**summary, "returned": json.loads(answer)})
                self._receipt(job_id, journal, summary, answer)
                if self._stop.is_set():
                    return stopped("stopped_by_alx")
                completion = self._model.complete(replace(request, tool_result=answer, tool_images=images))
        except BoardJobStopped:
            return stopped("stopped_by_alx")
        except WorkerError:
            self._abandon()
            raise
        except Exception as error:  # noqa: BLE001 - reported with what was completed
            if self._stop.is_set():
                return stopped("stopped_by_alx")
            self._abandon()
            raise WorkerError("worker_failed", returned) from error
        # The session ended without a report call: its final answer is the report.
        report = _plain(completion.output)
        if not isinstance(report, dict) or report.get("status") not in STATUSES:
            raise WorkerError("report_malformed", returned)
        journal["events"].append({"report": report})
        return self._finish(job_id, journal, calls, {"status": report["status"], "report": report})


__all__ = ["AGENT_INSTRUCTIONS", "BoardJobAgent", "BoardJobStopped", "REPORT_SCHEMA", "REPORT_TOOL", "agent_tools"]
