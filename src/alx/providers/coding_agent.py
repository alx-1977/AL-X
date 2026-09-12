"""Run one bounded coding job and return structured evidence.

The shape of this is deliberate, and it is the second attempt. The first asked
the coding model to emit one AL/X-specific JSON operation per provider call and
executed each one here. That protocol produced valid plans and then no
actionable operation at all: the model was being asked to hand-serialise a tool
loop it already implements, statelessly, one round trip per file read.

So the model now runs as what it is. It plans, then a native coding-agent
session works inside the assigned worktree with its own tools and its own
multi-turn context. Containment moved from a per-operation Python check to the
kernel, where a generated sandbox profile denies git metadata, credentials and
every blocked path for every process the agent starts.

What did not move is authority. The agent has no terminal in this iteration, so
it cannot commit, push, merge, deploy or request a review, and it cannot run a
test either. AL/X runs verification afterwards through the allowlisted command
executor, which is the only site in this package that starts a development
process. The agent's own report is treated as an account, never as evidence:
the repository diff and the test results are what Core is given.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import ModelMessage, ModelRequest, ModelRole, ReasoningModel
from alx.contracts.coding import (
    MAX_PLANNING_ATTEMPTS,
    MAX_REPORTED_COMMANDS,
    MAX_REPORTED_FILES,
    MAX_STAGED_FILES,
    MAX_LOCAL_REVIEW_CONTEXT_CHARACTERS,
    MAX_LOCAL_REVIEW_CYCLES,
    MAX_VERIFICATION_COMMANDS,
    DEFAULT_VERIFICATION_COMMAND_SECONDS,
    CodingCommandRecord,
    CodingError,
    CodingOutcome,
    CodingRequest,
    CodingCommit,
    CodingSession,
    CodingSessionResult,
    GitWorkspaceState,
)
from alx.providers.coding_process import (
    command_permitted,
    files_from_git_status,
    inspect_git,
    is_test_command,
    run_permitted_command,
)
from alx.providers.coding_git import (
    commit_job_changes,
    create_repair_branch,
    read_workspace_state,
)
from alx.providers.coding_workspace import CodingWorkspace
from alx.providers.errors import ProviderError
import json


PLAN_INSTRUCTION = (
    "You are a bounded coding worker preparing an implementation plan for one "
    "assigned software-engineering job. You are not AL/X and have no product, "
    "merge, deploy, push, review or governance authority. Produce only the "
    "requested structured plan. You will carry the plan out yourself in an "
    "assigned worktree using ordinary file reading, searching and editing, so "
    "plan real code changes. You will not have a terminal: do not plan shell "
    "commands, and describe verification as the tests that should be run "
    "rather than as commands you will run. You are in PLAN mode."
)

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

LOCAL_REVIEW_INSTRUCTION = (
    "You are an advisory local code reviewer for one bounded coding job. "
    "You cannot edit files, run commands, commit, push, merge, deploy, or "
    "request an external review. Inspect only the supplied task, diff, bounded "
    "file context, and test evidence. Return findings only when the candidate "
    "misses the stated cause, leaves an adjacent path violating the same "
    "invariant, or lacks meaningful regression coverage. Do not make style-only "
    "findings."
)

LOCAL_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "correction": {"type": "string"},
                },
                "required": ["severity", "title", "evidence", "correction"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}

_MATERIAL_REVIEW_SEVERITIES = frozenset({"medium", "high"})


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def build_briefing(request: CodingRequest, plan: Mapping[str, Any]) -> str:
    """The human-language instruction handed to the native coding session.

    Written as prose because the agent is a coding agent, not a protocol
    endpoint. It states the task, the accepted plan and the boundaries it
    cannot cross, including the two that are enforced elsewhere regardless of
    what it reads here: it has no terminal, and denied paths are refused by the
    operating system rather than by its own restraint.
    """
    lines = [
        "You are completing one assigned software-engineering task inside a git",
        "worktree that has already been prepared for you. Work directly: read,",
        "search and edit files with your normal tools until the task is done.",
        "",
        "# Task",
        request.task.strip(),
    ]
    if request.context.strip():
        lines += ["", "# Context", request.context.strip()]
    criteria = [item for item in request.acceptance_criteria if item.strip()]
    if criteria:
        lines += ["", "# Acceptance criteria"]
        lines += [f"- {item.strip()}" for item in criteria]
    lines += ["", "# Your accepted plan", str(plan.get("problem_understanding", "")).strip()]
    for label, key in (
        ("Intended changes", "intended_changes"),
        ("Verification expected", "verification"),
        ("Risks and constraints", "risks_constraints"),
    ):
        items = _strings(plan.get(key))
        if items:
            lines += ["", f"## {label}"]
            lines += [f"- {item}" for item in items]
    if request.test_guidance.strip():
        lines += ["", "# Test guidance", request.test_guidance.strip()]
    if request.repair_branch.strip():
        # Stated so the agent knows its edits are already on the repair branch
        # and has no reason to try to arrange one. It cannot run git either way.
        lines += [
            "",
            "# Branch",
            f"This worktree is already on the branch {request.repair_branch.strip()},",
            "prepared for you. Do not attempt to change it.",
        ]
    if request.blocked_paths:
        lines += ["", "# Paths you must not read or write"]
        lines += [f"- {item}" for item in request.blocked_paths]
    lines += [
        "",
        "# Boundaries",
        "- You have no terminal in this task. You cannot run commands or tests.",
        "  AL/X runs the tests after you finish and reads the result itself.",
        "- Do not commit, push, merge, deploy, or request a code review. AL/X",
        "  manages the branch and the commit herself after the tests pass.",
        "- Stay inside this worktree. Git metadata, environment files and",
        "  credential files are denied by the operating system, not by you.",
        "- Change only what this task requires.",
        "",
        "# Finishing",
        "When the work is done or you are genuinely blocked, stop and report",
        "plainly: what you changed and why, which files, anything you could not",
        "resolve, and what should be tested. Do not claim the task is complete",
        "if it is not.",
    ]
    return "\n".join(lines)


class CodingAgent:
    """One coding job: AL/X plans it, a native session does it, AL/X verifies it."""

    def __init__(
        self, model: ReasoningModel, session: CodingSession | None,
        reviewer: ReasoningModel, activity_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._model = model
        self._session = session
        self._reviewer = reviewer
        self._activity_sink = activity_sink or (lambda _activity: None)
        self._current_activity: str | None = None

    def _report_activity(self, activity: str) -> None:
        if self._current_activity == activity:
            return
        self._current_activity = activity
        self._activity_sink(activity)

    def run(self, request: CodingRequest) -> CodingOutcome:
        """Run one job and never leave runtime telemetry at a worker state."""
        try:
            return self._run(request)
        finally:
            self._report_activity("reasoning")

    def _run(self, request: CodingRequest) -> CodingOutcome:
        workspace = CodingWorkspace(request.worktree, request.blocked_paths)
        commands: list[CodingCommandRecord] = []
        preexisting_status, _ = self._git_evidence(workspace)
        preexisting_dirty = files_from_git_status(preexisting_status)
        preexisting_fingerprints = self._file_fingerprints(
            workspace, preexisting_dirty
        )
        # The baseline is read before anything else touches the worktree, so a
        # job can prove which HEAD it started from and what dirt it inherited.
        # Git being unreadable is not fatal on its own: a job that was not
        # asked for a commit still works in a directory that is not a
        # repository, so the baseline is simply absent.
        baseline = self._read_baseline(workspace)
        # `preexisting_dirty` above comes from the bounded evidence path, which
        # clips status at 16,000 characters. It is fine for reporting. It is
        # not fine for deciding what a job may stage: a clipped inherited path
        # that later reappears in status reads as job-owned. The baseline
        # reader has the complete listing, so authorisation uses that and falls
        # back only when git could not answer at all.
        baseline_dirty = (
            baseline.inherited_dirty if baseline is not None else preexisting_dirty
        )
        # The branch is created before the session so its edits land on the
        # branch rather than on whatever was checked out. A branch that cannot
        # be created fails the job closed: the alternative is a session that
        # writes to the wrong branch and only discovers it at commit time.
        if request.repair_branch.strip():
            try:
                baseline = create_repair_branch(
                    workspace.root, request.repair_branch.strip()
                )
            except CodingError as error:
                git_status, git_diff = self._git_evidence(workspace)
                return self._outcome(
                    status="failed",
                    summary="the repair branch could not be prepared",
                    files=(), preexisting_dirty=preexisting_dirty,
                    commands=commands, tests_run=False, tests_passed=None,
                    git_status=git_status, git_diff=git_diff,
                    issues=(error.code,), review=False, failure_status=True,
                    baseline=baseline,
                    diagnostics={"phase": "git_branch", **error.details},
                )

        plan, planning_failure = self._planning_phase(request, workspace)
        if plan is None:
            git_status, git_diff = self._git_evidence(workspace)
            issue = (
                "provider_failed"
                if planning_failure.get("failure_code") == "provider_failed"
                else "planning_failed"
            )
            return self._outcome(
                status="failed",
                summary="the coding model did not produce a usable plan",
                files=(), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=(issue,), review=False,
                failure_status=True, diagnostics=planning_failure,
                baseline=baseline,
            )
        plan_summary = str(plan["problem_understanding"])

        if self._session is None:
            git_status, git_diff = self._git_evidence(workspace)
            return self._outcome(
                status="failed",
                summary="no coding session is configured to carry out the plan",
                files=(), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=("coding_unavailable",), review=False,
                failure_status=True, plan_summary=plan_summary,
                diagnostics={"phase": "execution", "reason_code": "no_session"},
                baseline=baseline,
            )

        self._report_activity("coding")
        try:
            session = self._session.run_session(
                request, build_briefing(request, plan)
            )
        except CodingError as error:
            git_status, git_diff = self._git_evidence(workspace)
            return self._outcome(
                status="failed",
                summary="the coding session could not be started or completed",
                files=self._files_changed(
                    (), git_status, preexisting_dirty,
                    self._modified_preexisting(
                        workspace, preexisting_fingerprints
                    ),
                ),
                preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=(error.code,), review=False,
                failure_status=True, plan_summary=plan_summary,
                diagnostics={"phase": "execution", **error.details},
                baseline=baseline,
            )

        post_session_status, _ = self._git_evidence(workspace)
        session_files = self._files_changed(
            (), post_session_status, preexisting_dirty,
            self._modified_preexisting(workspace, preexisting_fingerprints),
        )
        review_failure: str | None = None
        review_issues: tuple[str, ...] = ()
        reviewed_files = session_files
        # There is no candidate to review when the native session reports a
        # failed execution. Preserve that failure for AL/X's normal outcome.
        if session.completed and session_files:
            review_failure, review_issues, reviewed_files = self._local_review_loop(
                request, workspace, plan, session_files, preexisting_dirty,
                preexisting_fingerprints,
            )
        if review_failure is not None:
            git_status, git_diff = self._git_evidence(workspace, reviewed_files)
            return self._outcome(
                status="failed", summary=review_failure, files=self._files_changed(
                    (), git_status, preexisting_dirty,
                    self._modified_preexisting(
                        workspace, preexisting_fingerprints
                    ),
                ), preexisting_dirty=preexisting_dirty, commands=commands,
                tests_run=False, tests_passed=None, git_status=git_status,
                git_diff=git_diff, issues=tuple(review_issues), review=False,
                failure_status=True, plan_summary=plan_summary,
                diagnostics={"phase": "local_review"},
                baseline=baseline,
            )

        # Verification is AL/X's, not the session's. The agent has no terminal,
        # so every command below is chosen here and refused unless the
        # allowlist already permits it. The scope is `reviewed_files`, the job's
        # final file set: a reviewer correction can touch a file the initial
        # session never did, and that file must select tests like any other.
        self._report_activity("reasoning")
        tests_run = False
        tests_passed: bool | None = None
        for argv in self._verification_commands(request, plan, reviewed_files):
            try:
                record = run_permitted_command(
                    list(argv), workspace.root,
                    timeout_seconds=DEFAULT_VERIFICATION_COMMAND_SECONDS,
                    blocked_paths=workspace.blocked_paths,
                )
            except CodingError as error:
                commands.append(
                    CodingCommandRecord(
                        tuple(argv), -1, "", error.code, False,
                        error.code != "command_not_permitted",
                    )
                )
                continue
            commands.append(record)
            if is_test_command(record.argv):
                tests_run = True
                passed = record.exit_status == 0 and not record.timed_out
                if not passed:
                    tests_passed = False
                elif tests_passed is None:
                    tests_passed = True

        git_status, git_diff = self._git_evidence(workspace, reviewed_files)
        files = self._files_changed(
            (), git_status, preexisting_dirty,
            self._modified_preexisting(workspace, preexisting_fingerprints),
        )
        issues = list(_strings(session.diagnostics.get("unresolved_issues")))
        issues.extend(review_issues)
        status = "succeeded"
        if not session.completed:
            status = "failed"
            if session.failure_code:
                issues.append(session.failure_code)
            else:
                issues.append("session_failed")
        elif not files:
            # A session that reports success while changing nothing has not
            # done the job. V1 treated an unchanged worktree the same way.
            status = "failed"
            issues.append("no_files_changed")
        elif tests_run and tests_passed is False:
            status = "failed"

        # Only a job that actually succeeded is committed. A failed one leaves
        # its work in the worktree for AL/X to read as a diff: committing it
        # would turn evidence Core still has to judge into a branch and a SHA
        # that read as a finished repair.
        #
        # `tests_run` is required, not merely `tests_passed is not False`.
        # D-029 authorises a commit "after the job has passed its required
        # verification", and a job that ran no verification has not passed it —
        # it skipped it. That is reachable whenever no candidate command
        # survives the allowlist, and the difference matters precisely because
        # an unverified commit reads downstream exactly like a verified one.
        # The job still succeeds and its work stays in the worktree; what it
        # does not get is a commit asserting it was checked.
        commit: CodingCommit | None = None
        wanted_commit = status == "succeeded" and request.commit_message.strip()
        # `files` is clipped to MAX_REPORTED_FILES for reporting. Committing
        # from a clipped set would stage the first fifty and present the
        # result as a complete repair, so the untruncated count decides
        # whether a commit is possible at all. Found in review on 2026-09-12:
        # the bound inside `commit_job_changes` compared an already-truncated
        # tuple against the same number and so could never fire.
        complete_files = self._files_changed(
            (), git_status, preexisting_dirty,
            self._modified_preexisting(workspace, preexisting_fingerprints),
            limit=None,
        )
        if wanted_commit and len(complete_files) > MAX_STAGED_FILES:
            issues.append("too_many_files_to_commit")
            wanted_commit = False
        if wanted_commit and not (tests_run and tests_passed):
            issues.append("unverified_not_committed")
        elif wanted_commit:
            try:
                commit = commit_job_changes(
                    workspace.root,
                    request.repair_branch.strip(),
                    request.commit_message.strip(),
                    complete_files,
                    baseline_dirty,
                    workspace.blocked_paths,
                )
            except CodingError as error:
                # A refused commit is a failed job, not a succeeded one with a
                # footnote. `unrelated_changes_staged` is the case this exists
                # for: rather than commit somebody else's work alongside the
                # repair, nothing is committed and Core is told why.
                status = "failed"
                issues.append(error.code)
                git_status, git_diff = self._git_evidence(workspace, reviewed_files)
            else:
                # Re-read after the commit: the files are now in history, so
                # the diff and status Core sees must describe what is left.
                git_status, git_diff = self._git_evidence(workspace, reviewed_files)

        summary = session.report.strip() or "the coding session returned no report"
        return self._outcome(
            status=status,
            summary=summary[:8_000],
            files=files,
            preexisting_dirty=preexisting_dirty,
            commands=commands,
            tests_run=tests_run,
            tests_passed=tests_passed,
            git_status=git_status,
            git_diff=git_diff,
            issues=tuple(issues),
            review=False,
            plan_summary=plan_summary,
            baseline=baseline,
            commit=commit,
            diagnostics={
                "phase": "execution",
                "session_turns": session.turns,
                "session_completed": session.completed,
                **{
                    key: value
                    for key, value in session.diagnostics.items()
                    if key != "unresolved_issues"
                },
            },
        )

    def _local_review_loop(
        self, request: CodingRequest, workspace: CodingWorkspace,
        plan: Mapping[str, Any], initial_files: tuple[str, ...],
        preexisting_dirty: tuple[str, ...],
        preexisting_fingerprints: Mapping[str, str | None],
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        """Review a candidate once, then re-review one bounded correction."""
        reviewed_files = initial_files
        for cycle in range(MAX_LOCAL_REVIEW_CYCLES):
            # The reviewer judges this job's diff, not the worktree's. Same
            # scoping as the outcome evidence, so both see the same thing.
            git_status, git_diff = self._git_evidence(workspace, reviewed_files)
            changed_files = self._files_changed(
                (), git_status, preexisting_dirty,
                self._modified_preexisting(workspace, preexisting_fingerprints),
            )
            reviewed_files = tuple(dict.fromkeys((*reviewed_files, *changed_files)))
            inspection_targets = tuple(
                name
                for name in _strings(plan.get("inspection_targets"))
                if name not in preexisting_dirty or name in changed_files
            )
            files = tuple(dict.fromkeys((
                *reviewed_files, *inspection_targets,
            )))
            self._report_activity("reviewing")
            try:
                findings = self._review(request, workspace, plan, files, git_diff)
            except CodingError:
                return (
                    "the local reviewer could not produce a usable result",
                    ("review_failed",), reviewed_files,
                )
            material = [item for item in findings if item["severity"] in _MATERIAL_REVIEW_SEVERITIES]
            if not material:
                return None, (), reviewed_files
            if cycle + 1 == MAX_LOCAL_REVIEW_CYCLES:
                return (
                    "the local reviewer found unresolved material issues",
                    ("local_review_material_findings",), reviewed_files,
                )
            before = (git_status, git_diff)
            briefing = build_briefing(request, plan) + "\n\n# Local reviewer findings\n" + "\n".join(
                f"- [{item['severity']}] {item['title']}: {item['evidence']} Correction: {item['correction']}"
                for item in material
            )
            self._report_activity("coding")
            try:
                correction = self._session.run_session(request, briefing)
            except CodingError:
                return (
                    "the coding session could not correct local review findings",
                    ("session_failed",), reviewed_files,
                )
            if not correction.completed:
                return (
                    "the coding session could not correct local review findings",
                    (correction.failure_code or "session_failed",), reviewed_files,
                )
            # Scoped exactly as `before` was. Comparing a narrowed diff with a
            # whole-worktree one would never match, so a correction that
            # changed nothing would read as progress.
            post_correction_status, _ = self._git_evidence(workspace)
            corrected_files = self._files_changed(
                (), post_correction_status, preexisting_dirty,
                self._modified_preexisting(workspace, preexisting_fingerprints),
            )
            next_files = tuple(dict.fromkeys((*reviewed_files, *corrected_files)))
            after = self._git_evidence(workspace, next_files)
            if after == before:
                return (
                    "the coding session did not change the reviewed candidate",
                    ("local_review_material_findings",), reviewed_files,
                )
            reviewed_files = next_files
        raise AssertionError("local review loop must return within its bound")

    def _review(
        self, request: CodingRequest, workspace: CodingWorkspace,
        plan: Mapping[str, Any], files: tuple[str, ...], git_diff: str,
    ) -> tuple[dict[str, str], ...]:
        """Ask the configured coding model for bounded advisory findings only."""
        remaining = MAX_LOCAL_REVIEW_CONTEXT_CHARACTERS
        context: dict[str, str] = {}
        for name in files:
            if remaining <= 0:
                break
            try:
                workspace.validate_inspection_target(name)
                text = workspace.resolve(name).read_text(encoding="utf-8")
            except (CodingError, OSError, UnicodeDecodeError):
                continue
            context[name] = text[:remaining]
            remaining -= len(context[name])
        values = self._complete(LOCAL_REVIEW_INSTRUCTION, {
            "task": request.task, "root_cause_context": request.context,
            "acceptance_criteria": list(request.acceptance_criteria),
            "plan": dict(plan), "git_diff": git_diff,
            "changed_files": list(files), "changed_file_context": context,
            "test_guidance": request.test_guidance,
        }, "alx_coding_local_review", LOCAL_REVIEW_SCHEMA, model=self._reviewer)
        raw = values.get("findings")
        if not isinstance(raw, (list, tuple)):
            raise CodingError("review_failed", reason_code="review_schema_invalid")
        findings: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise CodingError("review_failed", reason_code="review_schema_invalid")
            finding = {key: str(item.get(key, "")).strip() for key in ("severity", "title", "evidence", "correction")}
            finding["severity"] = finding["severity"].lower()
            if not all(finding.values()) or finding["severity"] not in {"low", "medium", "high"}:
                raise CodingError("review_failed", reason_code="review_schema_invalid")
            findings.append(finding)
        return tuple(findings)

    def _verification_commands(
        self,
        request: CodingRequest,
        plan: Mapping[str, Any],
        changed_files: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], ...]:
        """The bounded checks AL/X runs after the session finishes.

        Explicit test guidance and the accepted plan come first. The session's
        changed test modules and deterministic source-to-test neighbours follow.
        Each is accepted only if `command_permitted` already allows it, so this
        cannot widen the allowlist; only an absence of targeted evidence falls
        back to the worktree's broader suite.
        """
        chosen: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()

        def choose(argv: tuple[str, ...]) -> bool:
            if argv in seen:
                return False
            if not command_permitted(
                list(argv), self._root(request), tuple(request.blocked_paths)
            ):
                return False
            seen.add(argv)
            chosen.append(argv)
            return len(chosen) >= MAX_VERIFICATION_COMMANDS

        for text in (request.test_guidance, *_strings(plan.get("verification"))):
            for argv in self._candidate_arguments(text):
                if choose(argv):
                    return tuple(chosen)
        derived = self._changed_test_modules(request, changed_files)
        if derived and choose(("python", "-m", "pytest", "-q", *derived)):
            return tuple(chosen)
        if not chosen:
            fallback = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")
            choose(fallback)
        return tuple(chosen)

    def _changed_test_modules(
        self, request: CodingRequest, changed_files: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Test files changed by the job or mechanically adjacent to its source.

        No name is invented from task language. A source module has only the
        conventional test candidates derived from its path, and a candidate is
        returned only when it already exists inside the assigned worktree.
        """
        root = self._root(request)
        tests: list[str] = []
        for relative in changed_files:
            path = Path(relative)
            candidates: list[Path] = []
            if path.suffix == ".py" and path.name.startswith("test_"):
                candidates.append(path)
            if path.suffix == ".py" and path.parts[:2] == ("src", "alx"):
                module_parts = path.with_suffix("").parts[2:]
                candidates.extend((
                    Path("tests") / f"test_{'_'.join(module_parts)}.py",
                    Path("tests") / f"test_{path.stem}.py",
                ))
            for candidate in candidates:
                name = candidate.as_posix()
                if (
                    name not in tests
                    and (root / candidate).is_file()
                    and command_permitted(
                        ["python", "-m", "pytest", "-q", name],
                        root,
                        tuple(request.blocked_paths),
                    )
                ):
                    tests.append(name)
        return tuple(tests)

    @staticmethod
    def _root(request: CodingRequest):
        return Path(request.worktree).expanduser().resolve()

    @staticmethod
    def _candidate_arguments(text: str) -> tuple[tuple[str, ...], ...]:
        """Read command-shaped lines out of guidance text.

        This never executes what it finds. A candidate only becomes a command
        after the allowlist accepts it, so a wrong guess is discarded rather
        than run.
        """
        if not isinstance(text, str) or not text.strip():
            return ()
        found: list[tuple[str, ...]] = []
        for line in text.splitlines():
            stripped = line.strip().strip("`").strip()
            if not stripped:
                continue
            for prefix in ("$ ", "- ", "* "):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):].strip()
            parts = stripped.split()
            if not parts:
                continue
            if parts[0].lower() in ("pytest", "python", "python3", "git"):
                found.append(tuple(parts))
        return tuple(found)

    def _planning_phase(
        self, request: CodingRequest, workspace: CodingWorkspace
    ) -> tuple[Mapping[str, Any] | None, dict[str, object]]:
        feedback: list[str] = []
        last: dict[str, object] = {}
        for attempt in range(1, MAX_PLANNING_ATTEMPTS + 1):
            try:
                return self._plan(request, workspace, feedback), {}
            except CodingError as error:
                last = {
                    "phase": "planning",
                    "failure_code": error.code,
                    "reason_code": error.details.get("reason_code", error.code),
                    "planning_attempts": attempt,
                    "structured_output_received": error.code != "provider_failed",
                    "parsing_succeeded": error.code != "provider_failed",
                    "validation_succeeded": False,
                    **error.details,
                }
                # A selected transport failure fails closed. Only a parsed but
                # invalid plan receives bounded corrective feedback.
                if error.code == "provider_failed":
                    return None, last
                feedback.append("plan_validation_error:" + str(last["reason_code"]))
        return None, last

    def _plan(
        self, request: CodingRequest, workspace: CodingWorkspace,
        feedback: list[str],
    ) -> Mapping[str, Any]:
        material = {
            "phase": "planning",
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
            "execution_model": {
                "tools": "native file reading, searching and editing",
                "terminal": False,
                "tests_run_by": "alx_after_session",
            },
            "planning_feedback": feedback[-2:],
        }
        values = self._complete(
            PLAN_INSTRUCTION, material, "alx_coding_plan", PLAN_SCHEMA,
            model=self._model,
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

    def _complete(
        self, instruction: str, material: Mapping[str, Any], affinity: str,
        schema: Mapping[str, Any], *, model: ReasoningModel,
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
            completion = model.complete(model_request)
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

    @staticmethod
    def _read_baseline(workspace: CodingWorkspace) -> GitWorkspaceState | None:
        """The worktree's branch, HEAD and inherited dirt before the job runs.

        Absent rather than fatal when git cannot answer: a job that was not
        asked for a branch or a commit still works in a directory that is not
        a repository, and refusing it here would withdraw a capability that
        already exists. A job that *was* asked for one fails at the branch
        step instead, where the refusal is the right answer.
        """
        try:
            return read_workspace_state(workspace.root)
        except CodingError:
            return None

    def _git_evidence(
        self, workspace: CodingWorkspace, paths: tuple[str, ...] = ()
    ) -> tuple[str, str]:
        """Status for the whole tree, diff for the files this job touched.

        The worktree is not the job's to own. On 2026-09-11 a 109k diff of
        somebody else's uncommitted work clipped at the 32k bound before
        reaching any file under repair, and four sessions were handed the same
        truncated prefix. Narrowing the diff spends the budget on this job;
        status still covers everything, because what else is dirty is a fact
        the job needs to know.
        """
        try:
            evidence = inspect_git(workspace.root, paths)
        except CodingError:
            return "", ""
        diff = evidence.diff
        if evidence.diff_truncated:
            # Never present clipped evidence as complete. The reader decides
            # what a partial diff is worth; it may not be left to infer it.
            diff = (
                f"[diff truncated: showing {len(diff)} of "
                f"{evidence.diff_characters} characters]\n{diff}"
            )
        return evidence.status, diff

    def _files_changed(
        self,
        written: tuple[str, ...],
        git_status: str,
        preexisting_dirty: tuple[str, ...],
        modified_preexisting: tuple[str, ...] = (),
        limit: int | None = MAX_REPORTED_FILES,
    ) -> tuple[str, ...]:
        """The job's own changed files. `limit=None` returns the whole set.

        Reporting is clipped; authorisation is not. A clipped set reaching the
        commit would stage its prefix and call the result a finished repair.
        """
        preexisting = set(preexisting_dirty)
        modified = set(modified_preexisting)
        names = list(written)
        for item in files_from_git_status(git_status):
            if item in preexisting and item not in modified:
                continue
            if item not in names:
                names.append(item)
        return tuple(names if limit is None else names[:limit])

    @staticmethod
    def _file_fingerprints(
        workspace: CodingWorkspace, paths: tuple[str, ...]
    ) -> dict[str, str | None]:
        """Streaming fingerprints distinguish job edits from inherited dirt."""
        fingerprints: dict[str, str | None] = {}
        for name in paths:
            try:
                target = workspace.resolve(name)
                digest = hashlib.sha256()
                with target.open("rb") as source:
                    for chunk in iter(lambda: source.read(64 * 1024), b""):
                        digest.update(chunk)
            except (CodingError, OSError):
                fingerprints[name] = None
            else:
                fingerprints[name] = digest.hexdigest()
        return fingerprints

    @classmethod
    def _modified_preexisting(
        cls, workspace: CodingWorkspace, before: Mapping[str, str | None]
    ) -> tuple[str, ...]:
        """Only inherited paths whose bytes changed become this job's evidence."""
        after = cls._file_fingerprints(workspace, tuple(before))
        return tuple(name for name, value in before.items() if after.get(name) != value)

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
        baseline: GitWorkspaceState | None = None,
        commit: CodingCommit | None = None,
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
            baseline,
            commit,
        )
