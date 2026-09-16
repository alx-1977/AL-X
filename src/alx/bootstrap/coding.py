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
from alx.contracts.coding import CodingRequest, CodingTelemetry
from alx.providers.coding_agent import CodingAgent
from alx.providers.coding_worktree import CodingWorktreeAllocator
from alx.safety import AuthorityPolicy
from alx.tools.coding import (
    DEFINITION as CODING_DEFINITION,
    RELEASE_CODING_WORKSPACE,
    RELEASE_DEFINITION,
    RUN_CODING_TASK,
    build_coding_executors,
    build_release_executors,
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
    reviewer: ReasoningModel | None = None,
    activity_sink: Callable[[str], None] | None = None,
    telemetry_sink: Callable[[CodingTelemetry], None] | None = None,
    allocator: CodingWorktreeAllocator | None = None,
) -> CodingRuntime | None:
    """Compose coding-job authority, or leave it unregistered.

    Three halves are required now: a model to plan with, a session to carry the
    plan out, and under D-030 an allocator to give it somewhere isolated to do
    that. Without the session the capability is registered but every job fails
    at execution, which is a worse answer than the capability being honestly
    absent. Without the allocator there is nowhere a job may safely run, and
    the same reasoning applies more strongly: the alternative to an isolated
    worktree is the live checkout.
    """
    if not enabled:
        LOGGER.info("Coding agent is not enabled: no coding capability")
        return None
    if agent is None and model is None:
        LOGGER.info("Coding agent has no model: no coding capability")
        return None
    if agent is None and reviewer is None:
        LOGGER.info("Coding agent has no reviewer model: no coding capability")
        return None
    if agent is None and session is None:
        LOGGER.info("Coding agent has no session: no coding capability")
        return None
    if allocator is None:
        LOGGER.info("Coding agent has no worktree root: no coding capability")
        return None
    selected = agent or CodingAgent(
        model, session, reviewer, activity_sink, telemetry_sink,
        allocator=allocator,
    )

    def run_job(request: CodingRequest) -> Any:
        return selected.run(request)

    def release(job_id: str) -> Mapping[str, Any]:
        return allocator.release_authorised(job_id)

    executors = dict(build_coding_executors(run_job, call_id_source))
    executors.update(build_release_executors(release, call_id_source))

    LOGGER.info(
        "Coding agent enabled: %s, %s", RUN_CODING_TASK, RELEASE_CODING_WORKSPACE
    )
    return CodingRuntime(
        agent=selected,
        definitions=(CODING_DEFINITION, RELEASE_DEFINITION),
        policies={
            RUN_CODING_TASK: AuthorityPolicy(
                frozenset({CODING_EXECUTE_PERMISSION}),
                approval_required=False,
            ),
            # Releasing a workspace is the same authority as creating one: it
            # removes only what a coding job created, and only after that job
            # succeeded. It is a separate capability so that Core must choose
            # it deliberately, not a separate permission.
            RELEASE_CODING_WORKSPACE: AuthorityPolicy(
                frozenset({CODING_EXECUTE_PERMISSION}),
                approval_required=False,
            ),
        },
        executors=executors,
        permissions=frozenset({CODING_EXECUTE_PERMISSION}),
    )
