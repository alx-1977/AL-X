"""One transport-neutral entry into the authoritative AL/X Core."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from alx.contracts import (
    BackgroundEvent, ConversationOrigin, ConversationSnapshot, ConversationTurn,
    DurableConversationStore,
    ContentOrigin, RetentionPolicy,
)
from alx.conversation.store import ConversationNotFound
from alx.core import CoreAgent, CoreOutcome, CoreState

LOGGER = logging.getLogger(__name__)

# Above this many seconds, assembling context is worth saying out loud.
# Context is prepared while the person waits, so it should be work nobody
# notices; when it stops being that, the log should say so rather than leave a
# silent gap between the turn starting and the Core being called. Set where an
# attentive person begins to feel a pause.
#
# This is about how long the work took, never about what the mail says. No
# rule here decides whether a message matters or whether AL/X should speak:
# that is hers, and the speech path is checked to contain no such rule.
_SLOW_CONTEXT_ASSEMBLY_SECONDS = 0.25


class ConversationGateway:
    """Transport-neutral ingress. It attaches no goal: the Core is shown every
    unfinished goal of the conversation and decides which, if any, applies."""

    def __init__(self, core: CoreAgent, conversation_store: DurableConversationStore,
                 identifier_factory: Callable[[], str] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 contextual_events: Callable[[], tuple[BackgroundEvent, ...]] | None = None) -> None:
        self._core = core
        self._conversation_store = conversation_store
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._contextual_events = contextual_events or (lambda: ())

    def _store_response(self, conversation_id: str, outcome: CoreOutcome,
                        retention_until: datetime) -> None:
        """Store her reply. A plan's finish or cancel reply keeps its fixed id."""
        turn_id = outcome.response_turn_id or self._identifier_factory()
        self._append_reply(conversation_id, turn_id, outcome.response,
                           outcome.response_provenance, retention_until)
        if outcome.response_turn_id is not None and outcome.snapshot is not None:
            self._acknowledge(outcome.snapshot.state.goal_id, turn_id)

    def _append_reply(self, conversation_id: str, turn_id: str, text: str,
                      provenance, retention_until: datetime) -> None:
        """Append one reply turn, unless that exact turn is already stored."""
        current = self._conversation_store.load(conversation_id)
        if any(item.turn_id == turn_id for item in current.turns):
            return
        self._conversation_store.append(
            ConversationTurn(conversation_id, turn_id, ConversationOrigin.ALX_RESPONSE,
                             text, self._clock(), provenance=provenance),
            retention_until, current.revision,
        )

    def _acknowledge(self, goal_id: str, turn_id: str) -> None:
        try:
            self._core.plan_announcement_stored(goal_id, turn_id)
        except Exception as error:  # noqa: BLE001 - stored; the next tick clears it
            LOGGER.warning("Plan reply acknowledgement failed: %s", type(error).__name__)

    def reconcile_plan_announcements(self) -> int:
        """Store any finish or cancel reply a failure kept from the conversation.

        Her own words, already authored and held on the plan with their turn
        id; nothing is composed here. Stored at most once, by that id, then
        cleared from the plan. Runs on each tick, under the Core lock.
        """
        stored = 0
        for snapshot in self._core.pending_plan_announcements():
            announcement = snapshot.state.execution_plan.announcement
            try:
                self._append_reply(snapshot.conversation_id, announcement.turn_id,
                                   announcement.text, snapshot.provenance,
                                   snapshot.retention_until)
            except Exception as error:  # noqa: BLE001 - kept for the next tick
                LOGGER.warning("Plan reply for goal %s not stored: %s",
                               snapshot.state.goal_id, type(error).__name__)
                continue
            self._acknowledge(snapshot.state.goal_id, announcement.turn_id)
            stored += 1
        return stored

    def _with_contextual_events(
        self, conversation: ConversationSnapshot, *additional: BackgroundEvent
    ) -> ConversationSnapshot:
        started_at = monotonic()
        contextual = self._contextual_events()
        elapsed = monotonic() - started_at
        if elapsed >= _SLOW_CONTEXT_ASSEMBLY_SECONDS:
            LOGGER.warning(
                "Contextual event assembly took %.2fs for %d events",
                elapsed,
                len(contextual),
            )
        events = {
            item.event_id: item
            for item in (*contextual, *additional)
        }
        return ConversationSnapshot(
            conversation.conversation_id,
            conversation.turns,
            conversation.revision,
            conversation.retention_until,
            tuple(events.values()),
        )

    def receive_conversation_turn(self, turn: ConversationTurn, step_budget: int,
                                  retention_until: datetime) -> CoreOutcome:
        """Persist a turn, then pass the same durable thread to the sole Core."""
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
        if turn.provenance is None:
            origin = (
                ContentOrigin.ALX
                if turn.origin is ConversationOrigin.ALX_RESPONSE
                else ContentOrigin.PERSON
            )
            turn = replace(
                turn,
                provenance=RetentionPolicy().non_mail(origin, turn.occurred_at),
            )
        try:
            conversation = self._conversation_store.load(turn.conversation_id)
        except ConversationNotFound:
            conversation = self._conversation_store.create(
                turn.conversation_id, retention_until)
        conversation = self._conversation_store.append(
            turn, retention_until, conversation.revision)
        conversation = self._with_contextual_events(conversation)
        outcome = self._core.process(conversation, retention_until, step_budget)
        if outcome.state is CoreState.RESPONDED and outcome.response is not None:
            self._store_response(turn.conversation_id, outcome, retention_until)
        return outcome

    def receive_cognition_opportunity(
        self,
        conversation_id: str,
        opportunity,
        step_budget: int,
        retention_until: datetime,
    ) -> CoreOutcome:
        """Give the sole Core one occasion nobody asked for.

        The same durable conversation, the same Core, the same stores. The one
        difference is the origin, which Phase 3 uses to select the reasoner and
        which the Core sees as provenance. Her own note travels verbatim as
        context; nothing here reads it.
        """
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
        event = BackgroundEvent(
            opportunity.opportunity_id,
            "cognition.opportunity",
            opportunity.arose_at,
            {"origin": opportunity.origin.value},
            transient_data=(
                {} if opportunity.note is None else {"note": opportunity.note}
            ),
            provenance=opportunity.provenance,
        )
        try:
            conversation = self._conversation_store.load(conversation_id)
        except ConversationNotFound:
            conversation = self._conversation_store.create(
                conversation_id, retention_until
            )
        transient_conversation = self._with_contextual_events(conversation, event)
        outcome = self._core.process(
            transient_conversation,
            retention_until,
            step_budget,
            trigger_event_id=event.event_id,
            origin=opportunity.origin,
            # A plan attention names the goal it belongs to. A structured
            # reference the plan source wrote, never anything said.
            resume_plan_goal_id=next(
                (reference[len("execution_plan:"):]
                 for reference in opportunity.references
                 if reference.startswith("execution_plan:")), None,
            ),
        )
        if outcome.state is CoreState.RESPONDED and outcome.response is not None:
            self._store_response(conversation_id, outcome, retention_until)
        return outcome
