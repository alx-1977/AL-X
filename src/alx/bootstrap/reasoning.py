"""Compose the Core reasoner from approved repository identity documents."""

from __future__ import annotations

from pathlib import Path

from alx.config import ConfigurationError
from alx.contracts import (
    AgentDecision, AutonomousReasoningDisabled, CognitionOrigin, GoalMutationKind,
    ReasoningModel, SideEffect,
)
from alx.contracts.models import core_input_ceiling
from alx.core import ModelReasoner


def autonomous_input_ceiling(provider: str, model: str) -> int:
    """The input bound for an autonomous turn on this Core.

    Derived from the Core's recorded context window less its recorded maximum
    output, so it moves with the model rather than being a second number that
    can drift from it. Called with the conversational Core's identity, which
    settings require the autonomous one to equal. A model with no recorded
    window cannot be bounded honestly, so autonomy refuses to start on it.
    """
    ceiling = core_input_ceiling(provider, model)
    if ceiling is None:
        raise ConfigurationError(
            f"no model limits are recorded for {provider}/{model}; "
            "autonomous cognition cannot bound its input"
        )
    return ceiling


def build_model_reasoner(
    model: ReasoningModel,
    repository_root: Path,
    max_output_tokens: int | None = None,
    max_input_tokens: int | None = None,
    spend_authority=None,
    on_request_dispatched=None,
) -> ModelReasoner:
    """Build a reasoner over the approved Laws and identity.

    Every reasoner reads the same two approved documents, so a second Core
    built here is the same AL/X over a different model, never a different mind.
    The two bounds and the spend authority are one thing, not three options:
    conversation passes none of them, and a path spending against a dollar
    ceiling passes all of them. ModelReasoner refuses a partial combination,
    so a bounded reasoner that could dispatch without withdrawing anything
    cannot be built at all.
    """
    laws = (repository_root / "LAWS_OF_ALX.md").read_text(encoding="utf-8")
    identity = (repository_root / "IDENTITY_AND_MEMORY.md").read_text(encoding="utf-8")
    return ModelReasoner(
        model, laws, identity, max_output_tokens, max_input_tokens,
        spend_authority,
        on_request_dispatched,
    )


def changes_anything(decision: AgentDecision, context) -> bool:
    """Whether acting on this decision would change anything.

    Read from the decision's structure and the capability's declared effect
    class only, never from what anything says: an effectful call, a plan
    change, an approval, or closing a goal. Reading, attention state, memory
    queries, goal progress notes, words and silence change nothing.
    """
    if (decision.plan_update is not None or decision.approval_proposal is not None):
        return True
    proposal = decision.goal_proposal
    if proposal is not None and proposal.kind in (
        GoalMutationKind.REQUEST_COMPLETION, GoalMutationKind.CANCEL,
    ):
        return True
    if decision.call is None:
        return False
    definition = next((item for item in context.capabilities
                       if item.capability_id == decision.call.capability_id), None)
    # An unknown capability is treated as effectful rather than guessed safe.
    return definition is None or definition.side_effect is SideEffect.EFFECTFUL


class AutonomousReasonerUnavailable(AutonomousReasoningDisabled):
    """An autonomous turn arrived with no autonomous Core configured.

    Refused before any provider call. Answering it with the conversational
    model would spend money on a Core nobody selected and would make the
    recorded experiment a measurement of something nobody chose.
    """

    def __init__(self, origin: str) -> None:
        self.origin = origin
        super().__init__(
            f"no autonomous reasoner is configured for a {origin} turn"
        )


class OriginSelectedReasoner:
    """Two Cores, chosen by where the turn came from. Nothing else.

    Authorised by EX-001 as a narrowly scoped, time-boxed exception to Laws 0
    and 1, and recorded under D-024a as an experiment rather than architecture.
    It was built to learn whether Luna/max answered autonomous turns better
    than the conversational Core. Since the 2026-09-27 amendment both are the
    same subscription Core; the autonomous instance differs only in carrying
    the input bound and the per-occasion ledger that recovery relies on.

    The selection below is the whole mechanism: one expression over
    `CognitionOrigin.is_autonomous`. There is deliberately no table, registry,
    strategy object or per-turn choice, because each of those is the shape a
    model router takes, and EX-001 prohibits every one of them. A router keyed
    on anything semantic would be a second authority deciding which AL/X shows
    up before she has reasoned at all.

    Both reasoners are built from identical Laws, identity, catalogue,
    contracts, stores, broker and gate. Only provider, model, effort and the
    provider-side bounds differ. `CoreAgent`, the broker and the gate are never
    told which one answered, and nothing downstream can find out.

    The 2026-11-03 amendment adds an optional first pass on autonomous turns:
    a lighter model reasons first, and its decision stands only if
    `changes_anything` finds it changes nothing. Which Core answers first is
    still chosen by origin alone; the first pass is checked on what it
    decided, by structure, after it has reasoned.

    The exception expires when the experiment concludes. Until then this has no
    standing as architecture.
    """

    def __init__(
        self,
        conversational: ModelReasoner,
        autonomous: ModelReasoner | None = None,
        autonomous_first_pass: ModelReasoner | None = None,
    ) -> None:
        if autonomous_first_pass is not None and autonomous is None:
            raise ValueError("a first pass needs the autonomous Core behind it")
        self._conversational = conversational
        # EX-001 as amended 2026-11-03: a lighter model reasons first on an
        # autonomous turn. Its decision stands only if it changes nothing.
        self._first_pass = autonomous_first_pass
        # None when the experiment is unconfigured. The boundary still exists:
        # an autonomous turn is refused rather than quietly answered by the
        # conversational model, because a silent fallback would run the
        # experiment on the wrong Core and record results as if it had not.
        self._autonomous = autonomous

    def decide(self, context):
        # The one expression. It reads `origin` and nothing else: not the
        # goal, the notebook, the memories, the capabilities, the conversation,
        # nor any content of the turn.
        if context.origin.is_autonomous:
            if self._autonomous is None:
                raise AutonomousReasonerUnavailable(context.origin.value)
            # Her own work coming back (a finished job, a plan needing her, an
            # idle goal) is nearly always acted on, and a first pass would
            # only start, hand over and pay the context twice. It goes to the
            # autonomous Core directly, by origin alone (EX-001, 2026-11-03).
            if (self._first_pass is not None
                    and context.origin is not CognitionOrigin.WORK_COMPLETED):
                # A session the autonomous Core paused is continued by it (D-065).
                if self._autonomous.can_continue(context):
                    return self._autonomous.decide(context)
                decision = self._first_pass.decide(context)
                if not changes_anything(decision, context):
                    return decision
                # Reasoned again from the start; the first pass's session ends.
                self._first_pass.abandon()
            return self._autonomous.decide(context)
        return self._conversational.decide(context)
