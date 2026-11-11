"""Tests address the one `code` capability by action (D-074)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.code import CodePart, build_code_runtime  # noqa: E402
from alx.contracts import CODE, ApprovalScope, CapabilityCall, CodeAction  # noqa: E402
from alx.tools.code import CONTRACTS, build_code_executors, code_definition  # noqa: E402

RUN_JOB = CodeAction.RUN_JOB.value
STOP_JOB = CodeAction.STOP_JOB.value
GIT = CodeAction.GIT.value
READ_CHECKS = CodeAction.READ_CHECKS.value
REQUEST_REVIEW = CodeAction.REQUEST_REVIEW.value
REQUEST_REVIEW_BEYOND_ALLOWANCE = CodeAction.REQUEST_REVIEW_BEYOND_ALLOWANCE.value
READ_REVIEW = CodeAction.READ_REVIEW.value
MERGE = CodeAction.MERGE.value
RESTART = CodeAction.RESTART.value
EXPERIMENT = CodeAction.EXPERIMENT.value


def definition(*actions: CodeAction | str):
    """The one capability offering these actions (all when none are named)."""
    return code_definition(tuple(CodeAction(item) for item in actions) or tuple(CodeAction))


def contract(action: CodeAction | str):
    """One action's own input and result contract."""
    return CONTRACTS[CodeAction(action)]


def code_arguments(action: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"action": action, **(arguments or {})}


def code_call(call_id: str, action: str, arguments: Mapping[str, Any] | None = None,
              approval_id: str | None = None) -> CapabilityCall:
    return CapabilityCall(call_id, CODE, code_arguments(action, arguments), approval_id)


def code_scope(action: str, arguments: Mapping[str, Any]) -> ApprovalScope:
    return ApprovalScope(CODE, code_arguments(action, arguments))


def is_code(call: Any, action: str) -> bool:
    return (call is not None and call.capability_id == CODE
            and call.arguments.get("action") == action)


def code_runtime(*parts: Any, call_id_source: Callable[[], str] = lambda: "call-1"):
    """The one `code` capability over these compositions (None ones are skipped).

    A part is a composition carrying `actions` and `action_permissions`, such
    as build_coding_runtime's, or a mapping of CodeAction to executor, which
    is given an empty permission set.
    """
    composed = [
        CodePart(part, {action: frozenset() for action in part})
        if isinstance(part, Mapping) else part
        for part in parts
    ]
    return build_code_runtime(composed, call_id_source)


def executors(actions: Mapping[CodeAction, Callable], call_id_source=lambda: "call-1"):
    """The one `code` executor map over these action executors."""
    return build_code_executors(actions, call_id_source)


def addressed(actions: Mapping[CodeAction, Callable], action: CodeAction | str,
              call_id_source=lambda: "call-1") -> Callable[[Mapping[str, Any]], Any]:
    """Call one action through the one `code` executor, as the broker would."""
    run = build_code_executors(actions, call_id_source)[CODE]
    name = CodeAction(action).value
    return lambda arguments: run({"action": name, **arguments})
