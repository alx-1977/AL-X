"""Compose the coding-agent capability, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot propose a
coding job at all. That is the difference between the authority being withheld
and the job merely failing: an unregistered capability is honestly absent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CodingSession,
    ReasoningModel,
    StructuredData,
)
from alx.contracts.coding import CodingRequest
from alx.providers.coding_agent import CodingAgent
from alx.safety import AuthorityPolicy
from alx.tools.coding import (
    DEFINITION as CODING_DEFINITION,
    RUN_CODING_TASK,
    build_coding_executors,
)


LOGGER = logging.getLogger(__name__)

# Editing an assigned worktree is its own authority under D-028. Holding it
# follows from no other permission: sandbox.execute grants isolated experiments
# with no repository access, repository.merge grants merge of a reviewed head,
# and review.request spends on an external reviewer. None of those follows
# from this, and this follows from none of them.
CODING_EXECUTE_PERMISSION = "coding.execute"


@dataclass(frozen=True, slots=True)
class CodingRuntime:
    """The one coding-job capability, or nothing at all."""

    agent: CodingAgent
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_coding_runtime(
    enabled: bool,
    model: ReasoningModel | None,
    call_id_source: Callable[[], str],
    agent: CodingAgent | None = None,
    session: CodingSession | None = None,
) -> CodingRuntime | None:
    """Compose coding-job authority, or leave it unregistered.

    Both halves are required: a model to plan with, and a session to carry the
    plan out in the worktree. Without the session the capability is registered
    but every job fails at execution, which is a worse answer than the
    capability being honestly absent.
    """
    if not enabled:
        LOGGER.info("Coding agent is not enabled: no coding capability")
        return None
    if agent is None and model is None:
        LOGGER.info("Coding agent has no model: no coding capability")
        return None
    if agent is None and session is None:
        LOGGER.info("Coding agent has no session: no coding capability")
        return None
    selected = agent or CodingAgent(model, session)

    def run_job(request: CodingRequest) -> Any:
        return selected.run(request)

    LOGGER.info("Coding agent enabled: %s", RUN_CODING_TASK)
    return CodingRuntime(
        agent=selected,
        definitions=(CODING_DEFINITION,),
        policies={
            RUN_CODING_TASK: AuthorityPolicy(
                frozenset({CODING_EXECUTE_PERMISSION}),
                approval_required=False,
            ),
        },
        executors=build_coding_executors(run_job, call_id_source),
        permissions=frozenset({CODING_EXECUTE_PERMISSION}),
    )
