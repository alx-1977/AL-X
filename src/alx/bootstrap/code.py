"""Compose the one `code` capability from the code jobs this runtime has (D-074).

Each job keeps its own composition and authority: a coding job `coding.execute`
(D-028), git `repository.operate`, the check read `checks.read`, review
requests and reads their own permissions (D-026), the merge `repository.merge`,
restarting `runtime.restart` (D-055) and experiments `sandbox.execute` (D-027).
None follows from another. An action whose composition is absent is not
offered at all, which is the difference between it being off and merely
failing. Only a paid review past Friedl's allowance needs his approval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import (
    CODE,
    CapabilityDefinition,
    CapabilityResult,
    CodeAction,
    StructuredData,
)
from alx.safety import AuthorityPolicy
from alx.tools.code import APPROVAL_ACTIONS, CONTRACTS, build_code_executors, code_definition


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CodeRuntime:
    """The one `code` capability over the actions this runtime composed."""

    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]
    offered: tuple[CodeAction, ...]


def build_code_runtime(
    parts: Iterable[Any],
    call_id_source: Callable[[], str],
) -> CodeRuntime | None:
    """Compose `code` from each part's actions, or nothing when none is composed.

    A part is any composition carrying `actions` and `action_permissions`
    keyed by CodeAction; an absent part is None.
    """
    actions: dict[CodeAction, Callable[[StructuredData], CapabilityResult]] = {}
    permission_of: dict[CodeAction, frozenset[str]] = {}
    for part in parts:
        if part is None:
            continue
        for action, run in part.actions.items():
            if action in actions:
                raise ValueError(f"code action composed twice: {action.value}")
            actions[action] = run
            permission_of[action] = frozenset(part.action_permissions[action])
    if not actions:
        LOGGER.info("No code job is composed: no code capability")
        return None
    offered = tuple(item for item in CONTRACTS if item in actions)
    permissions = frozenset().union(*(permission_of[item] for item in offered))
    LOGGER.info("Code composed: %s", ", ".join(item.value for item in offered))
    return CodeRuntime(
        definitions=(code_definition(offered),),
        policies={
            CODE: AuthorityPolicy(
                permissions,
                # A review past his allowance is Friedl's to allow and binds
                # the exact call (2026-10-14); every other job is hers.
                approval_required=any(item in APPROVAL_ACTIONS for item in offered),
                approval_exempt_when=tuple(
                    (("action", item.value),) for item in offered
                    if item not in APPROVAL_ACTIONS
                ),
            ),
        },
        executors=build_code_executors(actions, call_id_source),
        permissions=permissions,
        offered=offered,
    )


@dataclass(frozen=True, slots=True)
class CodePart:
    """A code job composed in place, such as restarting onto merged main."""

    actions: Mapping[CodeAction, Callable[[StructuredData], CapabilityResult]]
    action_permissions: Mapping[CodeAction, frozenset[str]]


__all__ = ["CodePart", "CodeRuntime", "build_code_runtime"]
