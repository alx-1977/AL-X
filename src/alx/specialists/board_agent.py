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
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition,
    ModelCompletion, ModelMessage, ModelRequest, ModelRole, ReasoningModel,
)
from alx.contracts.kicad import PresentationEditScope, presentation_edit_scope
from alx.contracts.usage import is_measured
from alx.specialists.worker import MechanicalWorker, WorkerError, _catalogue, _plain, worker_tools

logger = logging.getLogger(__name__)

REPORT_TOOL = "report_step"
RECEIPTS_KEPT = 40
# What the job has read, carried from stretch to stretch (19 Nov 2026: 405 calls in one job,
# 108 of them exact repeats; each stretch starts afresh and read the same datasheets,
# library files and settings again).
NOTEBOOK_KEPT = 80
# Reads whose answer cannot change during a job: kept, and an identical call answered from
# them without running again. Reads of the work copy are never kept: edits change it.
# find_part is not here: the library grows during a job (add_part, write-back), review of #247.
UNCHANGING_READS = ("read_reference", "fetch_datasheet")


def _read_key(capability_id: Any, arguments: Mapping[str, Any]) -> str | None:
    """The key of a read whose answer cannot change during the job, or None."""
    if capability_id != "kicad" or arguments.get("action") not in UNCHANGING_READS:
        return None
    path = str(arguments.get("path", ""))
    if "runtime/kicad/" in path or "runtime/kicad.nosync/" in path or arguments.get("work_copy_id"):
        return None
    return json.dumps([capability_id, dict(arguments)], sort_keys=True, default=str)


def _note(arguments: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """One notebook line: what was asked and the gist of what came back."""
    asked = {k: arguments[k] for k in ("action", "path", "page", "offset", "part_number", "manufacturer",
                                       "query", "value", "package", "kind", "refs", "work_copy_id")
             if arguments.get(k) not in (None, "", [], {})}
    found: dict[str, Any] = {}
    for key in ("path", "pages", "page_count", "next_offset", "count", "document"):
        if isinstance(result.get(key), (str, int, float)):
            found[key] = result[key]
    if isinstance(result.get("parts"), list):
        found["parts"] = [p.get("symbol") or p.get("ref") for p in result["parts"][:12] if isinstance(p, Mapping)]
    return {"asked": asked, "found": found}


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
- Read schematics with kicad inspect_schematic. Use sheet and area for a region, net for its \
members, or ref for one symbol's pins and connections. Use inspect for the board and \
read_reference for datasheets and other evidence files.
- At the start of a schematic job read /Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/SCHEMATIC_STANDARD.md. \
Use a FireFli design block when one fits.
- Define a block_grid, place parts with place_in_slot and mirror twin blocks with mirror_block.
- Use route for wires inside a block and labels or ports between blocks. Put block information in info_table.
- Place supplies and grounds with s.place_power and check symbol orientation with s.upright. +If it reports pins that need rewiring, plan their reconnection before flipping the symbol.
- After adding or moving a root sheet symbol, call arrange_root with all root sheets in signal order.
- Edit the work copy through kicad edit with a script over the drawing helpers. Declare the parts \
and connections you intend to change. Check the gate and inspect each returned crop before reporting. \
If a crop is unavailable, use render and look. Use revert_to with a kept revision or start to undo.
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

PRESENTATION_BRIEF = (
    "Redraw this job's existing schematic sheets on the same work copy to the FireFli standard. "
    "Read /Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/SCHEMATIC_STANDARD.md. "
    "The accepted P0005 redraw scripts are reference material in "
    "/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/reference_p0005/. "
    "Make no electrical change. Use block_grid and place_in_slot, route, info_table, mirror_block, "
    "place_power, upright, arrange_root and FireFli design blocks as applicable to the existing circuit. "
    "Use kicad edit with intent: [] for every edit and look at every crop. Render the finished schematic "
    "and use look on every page, including sheets without a crop. Return the structured schematic step_ready "
    "report with the current work copy and revision and a short summary of what you tidied. Report any blocker."
)

PRESENTATION_ACTIONS = frozenset({"check", "edit", "inspect_schematic", "read_reference", "render", "select"})
PRESENTATION_INSTRUCTIONS = (
    "You are AL/X's board agent carrying out the presentation brief. Earlier job events are evidence "
    "of the finished engineering. Use look and the listed KiCad fields for check, edit, "
    "inspect_schematic, read_reference, render and select. Work on the job's current work copy. "
    "Report to AL/X in the structured output format."
)


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
        presentation_model: ReasoningModel | None = None,
        preferences: Path | None = None,
        max_seconds: float = 1800.0,
        image_source: Callable[[str], bytes | None] | None = None,
        block_catalog: Callable[[], Mapping[str, str]] | None = None,
        status_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(model, self._dispatch_board_call, definitions,
                         max_seconds=max_seconds, image_source=image_source)
        self._board_dispatch = dispatch
        self._presentation_model = presentation_model
        self._presentation_scope: PresentationEditScope | None = None
        self._journal_root = Path(journal_root)
        self._preferences = preferences
        self._block_catalog = block_catalog
        self._status_sink = status_sink
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
             max_calls: int, presentation: bool = False) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise WorkerError("worker_busy")
        engineering_model = self._model
        try:
            self._new_run()
            return self._work(job_id, brief, message, max_calls, presentation=presentation)
        finally:
            if self._running is not None:
                self._publish_status(self._running, terminal=True)
            self._running = None
            self._presentation_scope = None
            self._model = engineering_model
            self._busy.release()

    def _dispatch_board_call(self, call: CapabilityCall) -> CapabilityAttempt:
        scope = self._presentation_scope
        if scope is None or call.capability_id != "kicad":
            return self._board_dispatch(call)
        operation = call.arguments.get("action")
        if (not isinstance(operation, str) or operation not in PRESENTATION_ACTIONS
                or (operation != "read_reference" and (
                    call.arguments.get("work_copy_id") != scope.work_copy_id or "project_dir" in call.arguments))):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.REJECTED, False,
                                     reason_code="presentation_work_copy_only")
        token = presentation_edit_scope.set(scope)
        try:
            return self._board_dispatch(call)
        finally:
            presentation_edit_scope.reset(token)

    def _journal_path(self, job_id: str) -> Path:
        if not job_id or Path(job_id).name != job_id or job_id in (".", ".."):
            raise WorkerError("job_unknown")
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

    def _reads_path(self, job_id: str) -> Path:
        return self._journal_root / (job_id + ".reads.json")

    def _load_reads(self, job_id: str) -> dict[str, str]:
        """Answers of the job's unchanging reads, beside its journal, never shown to the model whole."""
        try:
            kept = json.loads(self._reads_path(job_id).read_text())
        except (OSError, ValueError):
            return {}
        return kept if isinstance(kept, dict) else {}

    def _save_reads(self, job_id: str, reads: Mapping[str, str]) -> None:
        try:
            self._journal_root.mkdir(parents=True, exist_ok=True)
            self._reads_path(job_id).write_text(json.dumps(reads, ensure_ascii=False))
        except OSError:
            logger.warning("board job %s: reads not kept", job_id)

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
        scope = self._presentation_scope
        if (scope is not None and receipt.get("action") == "edit" and not receipt.get("previewed")
                and receipt.get("work_copy_id") == scope.work_copy_id and receipt.get("work_copy_revision")):
            self._presentation_scope = replace(scope, work_copy_revision=receipt["work_copy_revision"])
            journal["work_copy"] = {"work_copy_id": scope.work_copy_id,
                                    "work_copy_revision": receipt["work_copy_revision"]}
        elif scope is None and receipt.get("work_copy_id"):
            journal["work_copy"] = {"work_copy_id": receipt["work_copy_id"],
                                    "work_copy_revision": receipt.get("work_copy_revision", "")}
        journal["receipts"] = (journal.get("receipts", []) + [receipt])[-RECEIPTS_KEPT:]
        progress = journal["activity"]
        if isinstance(result, Mapping):
            change = result.get("change_report")
            if isinstance(change, Mapping):
                files = [Path(name).stem for name in change.get("files_changed", [])
                         if isinstance(name, str) and name.endswith(".kicad_sch")]
                if len(files) == 1:
                    progress["sheet"] = files[0]
                gate = change.get("gate")
                if isinstance(gate, Mapping):
                    progress["last_check"] = "findings" if change.get("unmet") or any(gate.values()) else "clean"
            elif receipt.get("action") == "check" and isinstance(result.get("totals"), Mapping):
                progress["last_check"] = "findings" if result["totals"].get("total") else "clean"
        if progress.get("operation") in ("edit", "check") and summary.get("outcome") != "succeeded":
            progress["last_check"] = "refused"
        self._save(job_id, journal)

    def _publish_status(self, job_id, activity=None, *, terminal=False):
        if self._status_sink is None:
            return
        try:
            self._status_sink({"code": "board_job.status", "job_id": job_id,
                               "terminal": terminal, **(activity or {})})
        except Exception:  # A diagnostic listener cannot interrupt a board job.
            logger.info("board job %s: activity not published", job_id)

    def _remember(self, journal: dict[str, Any], arguments: Mapping[str, Any], answer: str) -> None:
        """A successful read goes in the job's notebook (what was asked, the gist of the answer)."""
        if arguments.get("action") not in UNCHANGING_READS + ("inspect_schematic", "inspect", "render"):
            return
        try:
            values = json.loads(answer).get("values") or {}
        except (ValueError, AttributeError):
            return
        result = values.get("result") if isinstance(values.get("result"), Mapping) else values
        note = _note(arguments, result if isinstance(result, Mapping) else {})
        kept = [n for n in journal.get("notebook", []) if n.get("asked") != note["asked"]]
        journal["notebook"] = (kept + [note])[-NOTEBOOK_KEPT:]

    def _log_call(self, job_id: str, tool_use: Mapping[str, Any], summary: Mapping[str, Any], answer: str,
                  call_number: int) -> None:
        """Every call in full, beside the journal and never shown to the model: what was sent
        and, when it failed, why (16 Nov: refusals could not be read while a job ran)."""
        try:
            returned = json.loads(answer)
        except ValueError:
            returned = {"unparsed": answer[:2000]}
        failure = returned.get("failure") if isinstance(returned, Mapping) else None
        if isinstance(failure, Mapping):
            recovery = failure.get("recovery") if isinstance(failure.get("recovery"), Mapping) else {}
            failure = {"code": failure.get("code"), "fault": failure.get("fault"),
                       "detail": recovery.get("detail") or failure.get("detail"),
                       "unmet": recovery.get("unmet"), "stderr": str(recovery.get("stderr") or "")[-1500:],
                       # A refused drawing prints its reason (16 Nov: 30 refusals logged without one).
                       "stdout": str(recovery.get("stdout") or "")[-3000:], "hint": recovery.get("hint")}
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "call_id": summary.get("call_id"),
                 "call_number": call_number,
                 "capability_id": summary.get("capability_id"), "outcome": summary.get("outcome"),
                 "sent": str((tool_use.get("input") or {}).get("arguments_json", ""))[:6000],
                 "refused": returned.get("refused") if isinstance(returned, Mapping) else None,
                 "reason_code": returned.get("reason_code") if isinstance(returned, Mapping) else None,
                 "failure": failure}
        if self._presentation_scope is not None:
            entry["pass"] = "presentation"
        self._append_log(job_id, "calls", entry)

    def _append_log(self, job_id: str, stream_name: str, entry: Mapping[str, Any]) -> None:
        try:
            with (self._journal_root / f"{job_id}.{stream_name}.jsonl").open("a") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.warning("board job %s: %s log not written", job_id, stream_name)

    def _complete(self, job_id: str, request: ModelRequest) -> ModelCompletion:
        if self._presentation_scope is None:
            return self._model.complete(request)
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pass": "presentation"}
        try:
            completion = self._model.complete(request)
        except Exception:
            self._append_log(job_id, "model-calls", {**entry, "outcome": "failed", "session_usage": None})
            raise
        paused = completion.tool_use is not None
        measured = not paused and is_measured(completion.usage)
        self._append_log(job_id, "model-calls", {
            **entry, "provider": completion.provider, "model": completion.model,
            "outcome": "tool" if paused else "completed",
            # The provider reports the native session's totals at its final response.
            "session_usage": dict(completion.usage) if measured else None,
        })
        return completion

    def _request(self, job_id: str, journal: Mapping[str, Any]) -> ModelRequest:
        blocks = self._block_catalog() if self._block_catalog is not None else {}
        catalog = "\n".join(f"- {name} — {' '.join(description.split())}"
                            for name, description in blocks.items())
        presenting = self._presentation_scope is not None
        instructions = PRESENTATION_INSTRUCTIONS if presenting else AGENT_INSTRUCTIONS
        system = (f"{instructions}\nFireFli design blocks:\n{catalog or '(none available)'}\n"
                  f"His preferences:\n{self._preferences_text()}\n\n"
                  f"Capabilities you may call:\n{_catalogue(tuple(self._definitions.values()))}")
        messages = [ModelMessage(ModelRole.SYSTEM, system),
                    ModelMessage(ModelRole.USER, PRESENTATION_BRIEF if presenting else journal["brief"])]
        if journal["events"] or journal.get("receipts"):
            messages.append(ModelMessage(ModelRole.USER, json.dumps({
                "transport_fact": "earlier_in_this_job",
                "detail": "What you reported in earlier stretches and what AL/X answered, in order, and "
                          "your latest calls; the last event is what to do now. Your work copy holds your "
                          "work so far: carry on with it, never make another, and check its current "
                          "state before you change it.",
                "work_copy": journal.get("work_copy"),
                "events": journal["events"],
                "latest_calls": journal.get("receipts", []),
                "already_read": journal.get("notebook", []),
                "already_read_note": "what this job has read so far and what came back: use it rather than "
                                     "reading again; an identical read is answered from the earlier one"},
                ensure_ascii=False)))
        return ModelRequest(tuple(messages), "board_report", REPORT_SCHEMA,
                            affinity_key=f"{job_id}:presentation" if presenting else job_id,
                            kind="board_agent", tools=worker_tools() if presenting else agent_tools(),
                            on_retry=self._renewal(job_id, journal))

    def _finish(self, job_id: str, journal: dict[str, Any], calls: list, outcome: dict[str, Any]) -> dict[str, Any]:
        self._save(job_id, journal)
        return {"job_id": job_id, "calls": calls, **outcome}

    def _work(self, job_id: str | None, brief: str | None, message: str | None,
              max_calls: int, *, presentation: bool) -> dict[str, Any]:
        if job_id is None:
            if presentation:
                raise WorkerError("schematic_not_ready")
            job_id = uuid.uuid4().hex[:12]
            journal = {"brief": brief, "events": []}
        else:
            journal = self._journal(job_id)
            if presentation:
                events = journal["events"]
                if any(event.get("pass") == "presentation" for event in events):
                    raise WorkerError("presentation_already_started")
                report = events[-1].get("report", {}) if events else {}
                copy = report.get("work_copy_id")
                revision = report.get("work_copy_revision")
                if (report.get("status") != "step_ready" or report.get("step") != "schematic"
                        or not isinstance(copy, str) or not copy
                        or not isinstance(revision, str) or not revision):
                    raise WorkerError("schematic_not_ready")
                if self._presentation_model is None:
                    raise WorkerError("worker_failed")
                self._model = self._presentation_model
                self._presentation_scope = PresentationEditScope(copy, revision)
                journal["work_copy"] = {"work_copy_id": copy, "work_copy_revision": revision}
                events.append({"pass": "presentation"})
            else:
                journal["events"].append({"alx": message})
        if "activity" not in journal:
            count = 0
            if journal["events"]:
                try:
                    with (self._journal_root / f"{job_id}.calls.jsonl").open() as stream:
                        count = sum(1 for line in stream if line.strip())
                except FileNotFoundError:
                    pass
            journal["activity"] = {"call_number": count}
        self._save(job_id, journal)
        self._running = job_id
        self._stop.clear()
        self._publish_status(job_id, journal["activity"])
        request = self._request(job_id, journal)
        reads = self._load_reads(job_id)
        started = monotonic()
        calls: list[dict[str, Any]] = []
        returned: list[dict[str, Any]] = []

        def stopped(reason: str) -> dict[str, Any]:
            self._abandon()
            journal["events"].append({"stopped": reason, "calls": len(calls),
                                      "work_copy": journal.get("work_copy")})
            return self._finish(job_id, journal, calls, {"status": "stopped", "stop_reason": reason})
        try:
            completion = self._complete(job_id, request)
            while completion.tool_use is not None:
                if self._stop.is_set():
                    return stopped("stopped_by_alx")
                if not presentation and completion.tool_use.get("name") == REPORT_TOOL:
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
                tool_input = completion.tool_use.get("input") if isinstance(completion.tool_use.get("input"), Mapping) else {}
                try:
                    asked = json.loads(tool_input.get("arguments_json") or "{}")
                except (TypeError, ValueError):
                    asked = {}
                asked = asked if isinstance(asked, dict) else {}
                activity = journal["activity"]
                activity["operation"] = asked.get("action") if tool_input.get("capability_id") == "kicad" else None
                activity["call_number"] += 1
                project = asked.get("project_dir")
                if isinstance(project, str):
                    parts = Path(project).parts
                    identifier = next((part.split("-", 1)[0] for part in reversed(parts)
                                       if part.startswith("P") and part[1:5].isdigit()), None)
                    activity["project"] = identifier or Path(project).stem
                if isinstance(asked.get("sheet"), str):
                    activity["sheet"] = Path(asked["sheet"]).stem
                self._publish_status(job_id, activity)
                key = _read_key(tool_input.get("capability_id"), asked)
                if presentation and asked.get("action") != "read_reference":
                    key = None
                if key is not None and key in reads:
                    # Read before in this job and unchanged since: the same answer, not run again.
                    answer, images = reads[key], ()
                    summary = {"call_id": f"{job_id}-{len(calls) + 1}", "capability_id": "kicad",
                               "outcome": "succeeded", "repeated_read": True}
                else:
                    answer, summary, images = self._call(completion.tool_use, f"{job_id}-{len(calls) + 1}")
                    if summary.get("outcome") == "succeeded":
                        if key is not None:
                            reads[key] = answer
                            self._save_reads(job_id, reads)
                        self._remember(journal, asked, answer)
                calls.append(summary)
                returned.append({**summary, "returned": json.loads(answer)})
                self._receipt(job_id, journal, summary, answer)
                self._log_call(job_id, completion.tool_use, summary, answer, activity["call_number"])
                self._publish_status(job_id, activity)
                if self._stop.is_set():
                    return stopped("stopped_by_alx")
                completion = self._complete(job_id, replace(request, tool_result=answer, tool_images=images))
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
        if presentation and self._stop.is_set():
            return stopped("stopped_by_alx")
        report = _plain(completion.output)
        if not isinstance(report, dict) or report.get("status") not in STATUSES:
            raise WorkerError("report_malformed", returned)
        if presentation and report["status"] in ("step_ready", "done"):
            if (report["status"] != "step_ready" or report.get("step") != "schematic"
                    or any(report.get(key) != value for key, value in journal["work_copy"].items())
                    or not isinstance(report.get("summary"), str) or not report["summary"].strip()):
                raise WorkerError("report_malformed", returned)
        journal["events"].append({"report": report})
        return self._finish(job_id, journal, calls, {"status": report["status"], "report": report})


__all__ = ["AGENT_INSTRUCTIONS", "BoardJobAgent", "BoardJobStopped", "REPORT_SCHEMA", "REPORT_TOOL", "agent_tools"]
