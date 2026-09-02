"""One transport-neutral entry into the authoritative AL/X Core."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from alx.contracts import (
    BackgroundEvent, ConversationOrigin, ConversationSnapshot, ConversationTurn,
    DurableConversationStore,
    ContentOrigin, RetentionPolicy,
)
from alx.conversation.store import ConversationNotFound
from alx.core import CoreAgent, CoreOutcome, CoreState


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

    def _with_contextual_events(
        self, conversation: ConversationSnapshot, *additional: BackgroundEvent
    ) -> ConversationSnapshot:
        events = {
            item.event_id: item
            for item in (*self._contextual_events(), *additional)
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
            response_turn = ConversationTurn(
                turn.conversation_id,
                self._identifier_factory(),
                ConversationOrigin.ALX_RESPONSE,
                outcome.response,
                self._clock(),
                provenance=outcome.response_provenance,
            )
            self._conversation_store.append(
                response_turn,
                retention_until,
                self._conversation_store.load(turn.conversation_id).revision,
            )
        return outcome

    def receive_background_event(
        self,
        conversation_id: str,
        event: BackgroundEvent,
        step_budget: int,
        retention_until: datetime,
    ) -> CoreOutcome:
        """Persist safe event metadata, then give the sole Core its transient facts."""
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
        if event.provenance is None:
            event = replace(
                event,
                provenance=RetentionPolicy().non_mail(
                    ContentOrigin.EXTERNAL, event.occurred_at
                ),
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
        )
        if outcome.state is CoreState.RESPONDED and outcome.response is not None:
            response_turn = ConversationTurn(
                conversation_id,
                self._identifier_factory(),
                ConversationOrigin.ALX_RESPONSE,
                outcome.response,
                self._clock(),
                provenance=outcome.response_provenance,
            )
            self._conversation_store.append(
                response_turn,
                retention_until,
                self._conversation_store.load(conversation_id).revision,
            )
        return outcome
