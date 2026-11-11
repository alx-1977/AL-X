"""The one `code` capability and its actions, as a contract (D-074).

Her coding jobs, git, pull requests, external reviews, sandbox experiments and
restarting onto merged code are actions of one capability. This module names
them, and recognises a call recorded before D-074 under its former id, so a
saved goal's coding history (its branches, its failure count) still counts.
None of the former ids can be called.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


CODE = "code"


class CodeAction(str, Enum):
    """Every code job, a closed list like the mail, Xero and research actions."""

    RUN_JOB = "run_job"
    STOP_JOB = "stop_job"
    GIT = "git"
    READ_CHECKS = "read_checks"
    REQUEST_REVIEW = "request_review"
    REQUEST_REVIEW_BEYOND_ALLOWANCE = "request_review_beyond_allowance"
    READ_REVIEW = "read_review"
    MERGE = "merge"
    RESTART = "restart"
    EXPERIMENT = "experiment"


# Saved goals hold calls under the ids they had before D-074. Only reading
# them is supported; nothing registers or dispatches these ids.
_RECORDED_BEFORE_D074 = {
    "run_coding_task": CodeAction.RUN_JOB,
    "stop_coding_job": CodeAction.STOP_JOB,
    "repository_operation": CodeAction.GIT,
    "read_pull_request_checks": CodeAction.READ_CHECKS,
    "request_external_review": CodeAction.REQUEST_REVIEW,
    "request_external_review_beyond_allowance": CodeAction.REQUEST_REVIEW_BEYOND_ALLOWANCE,
    "read_external_review": CodeAction.READ_REVIEW,
    "merge_pull_request": CodeAction.MERGE,
    "restart_self": CodeAction.RESTART,
    "run_sandbox_experiment": CodeAction.EXPERIMENT,
}


def recorded_code_action(capability_id: str, arguments: Mapping[str, Any]) -> CodeAction | None:
    """Which code job a recorded call was, in today's form or as saved before D-074."""
    if capability_id == CODE:
        try:
            return CodeAction(arguments.get("action"))
        except ValueError:
            return None
    return _RECORDED_BEFORE_D074.get(capability_id)


def is_code_action(call: Any, action: CodeAction) -> bool:
    """Whether a recorded or proposed call is this code job. Exact values only."""
    return (call is not None
            and recorded_code_action(call.capability_id, call.arguments) is action)


__all__ = ["CODE", "CodeAction", "is_code_action", "recorded_code_action"]
