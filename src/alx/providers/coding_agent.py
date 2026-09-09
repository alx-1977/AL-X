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


INSTRUCTION = (
    "You are a bounded coding worker executing one assigned software-engineering "
    "job. You are not AL/X and have no product, merge, deploy, push, review or "
    "governance authority. Stay inside the assigned worktree and task. You may "
    "read, list and write files there, and run permitted development commands "
    "(tests and read-only git inspection). When the job is done or blocked, "
    "finish with status, summary, unresolved issues and whether an external "
    "review is recommended. Do not claim success if tests failed or the change "
    "is incomplete."
)

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
        workspace = CodingWorkspace(request.worktree)
        observations: list[str] = []
        commands: list[CodingCommandRecord] = []
        written: list[str] = []
        tests_run = False
        tests_passed: bool | None = None

        for _step in range(request.step_budget):
            answer = self._ask(request, observations)
            decision = str(answer.get("decision") or "")
            if decision == "finish":
                status = str(answer.get("status") or "failed")
                if status == "succeeded" and tests_run and tests_passed is False:
                    status = "failed"
                git_status, git_diff = self._git_evidence(workspace)
                files = self._files_changed(written, git_status)
                issues = _strings(answer.get("unresolved_issues"))
                if status == "succeeded" and issues:
                    status = "failed"
                return self._outcome(
                    status=status,
                    summary=str(answer.get("summary") or "coding job finished").strip()
                    or "coding job finished",
                    files=files,
                    commands=commands,
                    tests_run=tests_run,
                    tests_passed=tests_passed,
                    git_status=git_status,
                    git_diff=git_diff,
                    issues=issues,
                    review=bool(answer.get("external_review_recommended")),
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
                    tests_passed = command.exit_status == 0 and not command.timed_out
            kind = str(answer.get("action_kind") or "")
            if kind == "write_file":
                path = str(answer.get("path") or "").strip()
                if path and path not in written:
                    written.append(path)
            if len(commands) >= MAX_REPORTED_COMMANDS:
                break

        git_status, git_diff = self._git_evidence(workspace)
        return self._outcome(
            status="failed",
            summary="the coding job reached its step budget without finishing",
            files=self._files_changed(written, git_status),
            commands=commands,
            tests_run=tests_run,
            tests_passed=tests_passed,
            git_status=git_status,
            git_diff=git_diff,
            issues=("step_budget_exhausted",),
            review=False,
            failure_status=True,
        )

    def _ask(
        self, request: CodingRequest, observations: list[str]
    ) -> Mapping[str, Any]:
        material = {
            "task": request.task,
            "acceptance_criteria": list(request.acceptance_criteria),
            "context": request.context,
            "test_guidance": request.test_guidance,
            "observations": observations[-12:],
        }
        model_request = ModelRequest(
            (
                ModelMessage(ModelRole.SYSTEM, INSTRUCTION),
                ModelMessage(ModelRole.USER, json.dumps(material, ensure_ascii=False)),
            ),
            "alx_coding_decision",
            ANSWER_SCHEMA,
            None,
            "alx-coding-v1",
            kind="coding",
        )
        try:
            completion = self._model.complete(model_request)
        except Exception as error:
            raise CodingError("provider_failed") from error
        values = completion.output
        if not isinstance(values, Mapping):
            raise CodingError("provider_failed")
        return values

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
            record = run_permitted_command(list(raw), workspace.root)
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

    def _files_changed(self, written: list[str], git_status: str) -> tuple[str, ...]:
        names = list(written)
        for item in files_from_git_status(git_status):
            if item not in names:
                names.append(item)
        return tuple(names[:MAX_REPORTED_FILES])

    def _outcome(
        self,
        *,
        status: str,
        summary: str,
        files: tuple[str, ...],
        commands: list[CodingCommandRecord],
        tests_run: bool,
        tests_passed: bool | None,
        git_status: str,
        git_diff: str,
        issues: tuple[str, ...],
        review: bool,
        failure_status: bool = False,
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
        )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())
