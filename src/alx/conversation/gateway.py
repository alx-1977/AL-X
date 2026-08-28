"""One transport-neutral entry into the authoritative AL/X Core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from alx.contracts import (
    ConversationOrigin, ConversationTurn, DurableConversationStore,
)
from alx.conversation.store import ConversationNotFound
from alx.core import CoreAgent, CoreOutcome, CoreState


class ActiveGoalLocator(Protocol):
    """Locate attached durable goal state without interpreting any words."""

    def __call__(self, conversation_id: str) -> str | None: ...


class ConversationGateway:
    def __init__(self, core: CoreAgent, conversation_store: DurableConversationStore,
                 locate_active_goal: ActiveGoalLocator,
                 identifier_factory: Callable[[], str] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._core = core
        self._conversation_store = conversation_store
        self._locate_active_goal = locate_active_goal
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))
        self._clock = clock or (lambda: datetime.now(UTC))

    def receive_conversation_turn(self, turn: ConversationTurn, step_budget: int,
                                  retention_until: datetime) -> CoreOutcome:
        """Persist a turn, then pass the same durable thread to the sole Core."""
        if step_budget <= 0:
            raise ValueError("step_budget must be positive")
        try:
            conversation = self._conversation_store.load(turn.conversation_id)
        except ConversationNotFound:
            conversation = self._conversation_store.create(
                turn.conversation_id, retention_until)
        conversation = self._conversation_store.append(
            turn, retention_until, conversation.revision)
        outcome = self._core.process(
            conversation,
            self._locate_active_goal(turn.conversation_id),
            retention_until,
            step_budget,
        )
        if outcome.state is CoreState.RESPONDED and outcome.response is not None:
            response_turn = ConversationTurn(
                turn.conversation_id,
                self._identifier_factory(),
                ConversationOrigin.ALX_RESPONSE,
                outcome.response,
                self._clock(),
            )
            self._conversation_store.append(
                response_turn,
                retention_until,
                self._conversation_store.load(turn.conversation_id).revision,
            )
        return outcome
