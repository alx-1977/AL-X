"""Run one bounded coding job and return structured evidence.

The loop is mechanical: ask the configured coding model for one structured
action, execute it inside the assigned worktree, or finish. The model is not
AL/X. It holds no catalogue, no goal, no merge or review authority, and cannot
widen the task. Core interprets the evidence that comes back.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Mapping

from alx.contracts import ModelMessage, ModelRequest, ModelRole, ReasoningModel
from alx.contracts.coding import (
    MAX_REPORTED_COMMANDS,
    MAX_REPORTED_FILES,
    CodingCommandRecord,
    CodingError,
    CodingOutcome,
    CodingRequest,
)
from alx.providers.coding_process import (
    files_from_git_status,
    inspect_git,
    is_test_command,
    run_permitted_command,
)
from alx.providers.coding_workspace import CodingWorkspace
from alx.providers.errors import ProviderError


PLAN_INSTRUCTION = (
    "You are a bounded coding worker preparing an implementation plan for one "
    "assigned software-engineering job. You are not AL/X and have no product, "
    "merge, deploy, push, review or governance authority. Produce only the "
    "requested structured plan. The operation contract is exhaustive: do not "
    "plan shell commands or authority the executor cannot perform."
)

INSTRUCTION = (
    "You are a bounded coding worker executing one assigned software-engineering "
    "job. You are not AL/X and have no product, merge, deploy, push, review or "
    "governance authority. Stay inside the assigned worktree and task. You may "
    "read, list and write files there, and run permitted development commands "
    "(tests and read-only git inspection). Blocked paths in the job, and all of "
    "their descendants, must not be read or written; the executor enforces that. "
    "When the job is done or blocked, finish with status, summary, unresolved "
    "issues and whether an external review is recommended. Do not claim success "
    "if tests failed or the change is incomplete."
)

OPERATION_CONTRACT = {
    "read_file": "read UTF-8 file inside assigned worktree",
    "list_dir": "list directory inside assigned worktree",
    "write_file": "write UTF-8 file inside assigned worktree",
    "run_command": "argv-only permitted tests or read-only git inspection",
    "refused": ["generic shell", "commit", "push", "merge", "deploy", "review"],
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "problem_understanding": {"type": "string"},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "inspection_targets": {"type": "array", "items": {"type": "string"}},
        "intended_changes": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "array", "items": {"type": "string"}},
        "risks_constraints": {"type": "array", "items": {"type": "string"}},
        "more_context_required": {"type": "boolean"},
    },
    "required": [
        "problem_understanding", "hypotheses", "inspection_targets",
        "intended_changes", "verification", "risks_constraints",
        "more_context_required",
    ],
    "additionalProperties": False,
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["act", "finish"]},
        "action_kind": {
            "type": "string",
            "enum": ["read_file", "list_dir", "write_file", "run_command", "none"],
        },
        "path": {"type": "string"},
        "content": {"type": "string"},
        "command": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "unresolved_issues": {"type": "array", "items": {"type": "string"}},
        "external_review_recommended": {"type": "boolean"},
        "status": {"type": "string", "enum": ["succeeded", "failed", "blocked"]},
    },
    "required": [
        "decision",
        "action_kind",
        "path",
        "content",
        "command",
        "summary",
        "unresolved_issues",
        "external_review_recommended",
        "status",
    ],
    "additionalProperties": False,
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodingAgent:
    """One coding job through one model and one worktree-bounded executor."""

    def __init__(self, model: ReasoningModel) -> None:
        self._model = model

    def run(self, request: CodingRequest) -> CodingOutcome:
        workspace = CodingWorkspace(request.worktree, request.blocked_paths)
        observations: list[str] = []
        commands: list[CodingCommandRecord] = []
        written: list[str] = []
        tests_run = False
        tests_passed: bool | None = None
        stop_reason = "step_budget_exhausted"
        preexisting_status, _ = self._git_evidence(workspace)
        preexisting_dirty = files_from_git_status(preexisting_status)
        try:
            plan = self._plan(request, workspace)
        except CodingError as error:
            git_status, git_diff = self._git_evidence(workspace)
            return self._outcome(
                status="failed", summary="the coding model did not produce a usable plan",
                files=(), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=(error.code,), review=False,
                failure_status=True, diagnostics=error.details,
            )
        plan_summary = str(plan["problem_understanding"])
        observations.append("plan accepted: " + plan_summary[:2_000])

        for _step in range(request.step_budget):
            try:
                answer = self._ask(request, observations)
            except CodingError as error:
                git_status, git_diff = self._git_evidence(workspace)
                return self._outcome(
                    status="failed",
                    summary="the coding model failed before the job finished",
                    files=self._files_changed(
                        written, git_status, preexisting_dirty
                    ),
                    preexisting_dirty=preexisting_dirty,
                    commands=commands,
                    tests_run=tests_run,
                    tests_passed=tests_passed,
                    git_status=git_status,
                    git_diff=git_diff,
                    issues=(error.code,),
                    review=False,
                    failure_status=True,
                    diagnostics=error.details,
                    plan_summary=plan_summary,
                )
            decision = str(answer.get("decision") or "")
            if decision == "finish":
                status = str(answer.get("status") or "failed")
                if status == "succeeded" and tests_run and tests_passed is False:
                    status = "failed"
                git_status, git_diff = self._git_evidence(workspace)
                files = self._files_changed(
                    written, git_status, preexisting_dirty
                )
                issues = _strings(answer.get("unresolved_issues"))
                if status == "succeeded" and issues:
                    status = "failed"
                return self._outcome(
                    status=status,
                    summary=str(answer.get("summary") or "coding job finished").strip()
                    or "coding job finished",
                    files=files,
                    preexisting_dirty=preexisting_dirty,
                    commands=commands,
                    tests_run=tests_run,
                    tests_passed=tests_passed,
                    git_status=git_status,
                    git_diff=git_diff,
                    issues=issues,
                    review=bool(answer.get("external_review_recommended")),
                    plan_summary=plan_summary,
                )
            if decision != "act":
                observations.append("invalid_decision")
                continue
            try:
                observation, command = self._act(workspace, answer)
            except CodingError as error:
                observations.append(f"error:{error.code}")
                if str(answer.get("action_kind") or "") == "run_command":
                    commands.append(
                        CodingCommandRecord(
                            tuple(str(item) for item in (answer.get("command") or ())),
                            -1,
                            "",
                            error.code,
                            False,
                            error.code != "command_not_permitted",
                        )
                    )
                continue
            observations.append(observation[:8_000])
            if command is not None:
                commands.append(command)
                if is_test_command(command.argv):
                    tests_run = True
                    passed = command.exit_status == 0 and not command.timed_out
                    if not passed:
                        tests_passed = False
                    elif tests_passed is None:
                        tests_passed = True
            kind = str(answer.get("action_kind") or "")
            if kind == "write_file":
                path = str(answer.get("path") or "").strip()
                if path and path not in written:
                    written.append(path)
            if len(commands) >= MAX_REPORTED_COMMANDS:
                stop_reason = "command_budget_exhausted"
                break
        else:
            stop_reason = "step_budget_exhausted"

        git_status, git_diff = self._git_evidence(workspace)
        summary = (
            "the coding job reached its command budget without finishing"
            if stop_reason == "command_budget_exhausted"
            else "the coding job reached its step budget without finishing"
        )
        return self._outcome(
            status="failed",
            summary=summary,
            files=self._files_changed(
                written, git_status, preexisting_dirty
            ),
            preexisting_dirty=preexisting_dirty,
            commands=commands,
            tests_run=tests_run,
            tests_passed=tests_passed,
            git_status=git_status,
            git_diff=git_diff,
            issues=(stop_reason,),
            review=False,
            failure_status=True,
            plan_summary=plan_summary,
        )

    def _plan(
        self, request: CodingRequest, workspace: CodingWorkspace
    ) -> Mapping[str, Any]:
        material = {
            "task": request.task,
            "worktree": str(workspace.root),
            # A bounded, blocked-path-filtered root listing grounds the plan in
            # the assigned repository without granting the planning turn a
            # shell or an unbounded file search.
            "worktree_entries": list(workspace.list_dir(".")),
            "acceptance_criteria": list(request.acceptance_criteria),
            "context": request.context,
            "test_guidance": request.test_guidance,
            "blocked_paths": list(request.blocked_paths),
            "operation_contract": OPERATION_CONTRACT,
        }
        values = self._complete(
            PLAN_INSTRUCTION, material, "alx_coding_plan", PLAN_SCHEMA
        )
        required_lists = (
            "hypotheses", "inspection_targets", "intended_changes",
            "verification", "risks_constraints",
        )
        if (not isinstance(values.get("problem_understanding"), str)
                or not values["problem_understanding"].strip()
                or not isinstance(values.get("more_context_required"), bool)
                or any(not isinstance(values.get(field), (list, tuple))
                       or any(not isinstance(item, str) or not item.strip()
                              for item in values[field])
                       for field in required_lists)):
            raise CodingError("plan_unusable", reason_code="plan_schema_invalid")
        try:
            for target in values["inspection_targets"]:
                workspace.validate_inspection_target(target)
        except CodingError as error:
            raise CodingError("plan_unusable", reason_code=error.code) from error
        return values

    def _ask(
        self, request: CodingRequest, observations: list[str]
    ) -> Mapping[str, Any]:
        material = {
            "task": request.task,
            "acceptance_criteria": list(request.acceptance_criteria),
            "context": request.context,
            "test_guidance": request.test_guidance,
            "blocked_paths": list(request.blocked_paths),
            "observations": observations[-12:],
        }
        return self._complete(INSTRUCTION, material, "alx_coding_decision", ANSWER_SCHEMA)

    def _complete(
        self, instruction: str, material: Mapping[str, Any], affinity: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        model_request = ModelRequest(
            (
                ModelMessage(ModelRole.SYSTEM, instruction),
                ModelMessage(ModelRole.USER, json.dumps(material, ensure_ascii=False)),
            ),
            affinity,
            schema,
            None,
            "alx-coding-v1",
            kind="coding",
        )
        details: dict[str, object] = {}
        try:
            completion = self._model.complete(model_request)
        except ProviderError as error:
            details = {
                "reason_code": error.reason,
                "provider": error.provider,
                **error.details,
            }
        except Exception:
            details = {"reason_code": "provider_failed"}
        else:
            values = completion.output
            if not isinstance(values, Mapping):
                raise CodingError("provider_failed", reason_code="output_not_object")
            return values
        raise CodingError("provider_failed", **details)

    def _act(
        self, workspace: CodingWorkspace, answer: Mapping[str, Any]
    ) -> tuple[str, CodingCommandRecord | None]:
        kind = str(answer.get("action_kind") or "")
        path = str(answer.get("path") or "")
        if kind == "read_file":
            return f"read:{path}\n{workspace.read_text(path)}", None
        if kind == "list_dir":
            names = workspace.list_dir(path or ".")
            return f"list:{path or '.'}\n" + "\n".join(names), None
        if kind == "write_file":
            written = workspace.write_text(path, str(answer.get("content") or ""))
            return f"wrote:{written}", None
        if kind == "run_command":
            raw = answer.get("command") or []
            if not isinstance(raw, (list, tuple)) or any(
                not isinstance(item, str) for item in raw
            ):
                raise CodingError("arguments_unusable")
            record = run_permitted_command(
                list(raw),
                workspace.root,
                blocked_paths=workspace.blocked_paths,
            )
            return (
                f"command:{' '.join(record.argv)} exit={record.exit_status}",
                record,
            )
        raise CodingError("arguments_unusable")

    def _git_evidence(self, workspace: CodingWorkspace) -> tuple[str, str]:
        try:
            return inspect_git(workspace.root)
        except CodingError:
            return "", ""

    def _files_changed(
        self,
        written: list[str],
        git_status: str,
        preexisting_dirty: tuple[str, ...],
    ) -> tuple[str, ...]:
        preexisting = set(preexisting_dirty)
        names = list(written)
        for item in files_from_git_status(git_status):
            if item in preexisting:
                continue
            if item not in names:
                names.append(item)
        return tuple(names[:MAX_REPORTED_FILES])

    def _outcome(
        self,
        *,
        status: str,
        summary: str,
        files: tuple[str, ...],
        preexisting_dirty: tuple[str, ...] = (),
        commands: list[CodingCommandRecord],
        tests_run: bool,
        tests_passed: bool | None,
        git_status: str,
        git_diff: str,
        issues: tuple[str, ...],
        review: bool,
        failure_status: bool = False,
        diagnostics: dict[str, object] | None = None,
        plan_summary: str = "",
    ) -> CodingOutcome:
        if status not in ("succeeded", "failed", "blocked"):
            status = "failed"
        if failure_status:
            status = "failed"
        return CodingOutcome(
            status,
            summary,
            files,
            tuple(commands[:MAX_REPORTED_COMMANDS]),
            tests_run,
            tests_passed,
            git_status,
            git_diff,
            issues,
            review,
            datetime.now(UTC),
            _digest(git_diff),
            preexisting_dirty,
            diagnostics,
            plan_summary,
        )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())
