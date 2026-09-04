"""AL/X must be told how to scope a memory retrieval, not just to do one.

On 2026-09-04, twice within thirty seconds, a live turn ended:

    Reasoner decision rejected: DecisionValidationError:
      retrieval requires a scope narrower than memory kind alone

She was asked whether she had used her diary, tried to check her own memory
before answering, and the query was refused. Both turns ended
`state=error, response=False`, so she was silenced.

`MemoryQuery` is right to demand a narrowing field: kinds alone would replay
the whole store, which `IDENTITY_AND_MEMORY.md` forbids. The protocol simply
never said so. It had recently gained an instruction to *consider one
retrieval* before forming a possibly duplicate memory, without saying how to
scope it.

These tests assert against the request the reasoner actually builds, not
against the constant alone: the constant could be correct while the assembled
request omitted it.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    ConversationOrigin, ConversationTurn, MemoryKind, MemoryQuery,
    ReasoningContext,
)
from alx.core.model_reasoner import ModelReasoner  # noqa: E402

NOW = datetime(2026, 9, 4, 8, 15, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)

NARROWING_FIELDS = (
    "memory_ids",
    "memory_person_id",
    "memory_formed_after",
    "memory_formed_before",
    "memory_source_references",
)


class Model:
    """A model that is never called: only the built request is inspected."""

    def complete(self, request):  # pragma: no cover - never invoked
        raise AssertionError("no provider call may happen in this test")


def reasoner() -> ModelReasoner:
    return ModelReasoner(Model(), "The approved Laws.", "The identity context.")


def context() -> ReasoningContext:
    turn = ConversationTurn(
        "c1", "t1", ConversationOrigin.TYPED,
        "have you used your diary yet?", NOW, "friedl",
    )
    return ReasoningContext(
        active_goal=None, turns=(turn,), capabilities=(), conversation_id="c1",
    )


def protocol_text() -> str:
    """Every system instruction of the request the runtime would send."""
    request = reasoner().build_request(context())
    return "\n".join(
        message.content for message in request.messages
        if message.role.value == "system"
    )


class RetrievalScopeProtocolTest(unittest.TestCase):
    def test_the_built_request_states_the_scope_requirement(self) -> None:
        text = protocol_text()
        self.assertIn("narrowed by more than memory kind", text)

    def test_the_built_request_names_every_narrowing_field(self) -> None:
        """Naming them is the point: she cannot guess the accepted spelling."""
        text = protocol_text()
        for field in NARROWING_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, text)

    def test_the_guidance_sits_with_the_retrieval_instruction(self) -> None:
        """The advice to retrieve and how to scope it must not drift apart."""
        text = protocol_text()
        advice = text.index("consider one retrieval")
        scope = text.index("narrowed by more than memory kind")
        self.assertLess(advice, scope)
        self.assertLess(
            scope - advice, 400,
            "the scope rule must stay beside the instruction it qualifies",
        )

    def test_the_contract_still_refuses_a_kind_only_retrieval(self) -> None:
        """The runtime rule is correct and must not have been relaxed."""
        with self.assertRaises(ValueError):
            MemoryQuery(query_id="q-1", kinds=(MemoryKind.FACTUAL,))

    def test_each_named_field_actually_satisfies_the_contract(self) -> None:
        """What she is told to send must be what the contract accepts.

        A protocol naming a field the validator rejects would be worse than
        saying nothing, so every field is exercised against the real contract.
        """
        accepted = {
            "memory_ids": {"memory_ids": ("m-1",)},
            "memory_person_id": {
                "person_id": "friedl", "kinds": (MemoryKind.RELATIONSHIP,)},
            "memory_formed_after": {"formed_after": NOW - timedelta(days=1)},
            "memory_formed_before": {"formed_before": NOW},
            "memory_source_references": {"source_references": ("turn:t1",)},
        }
        for field in NARROWING_FIELDS:
            with self.subTest(field=field):
                arguments = dict(accepted[field])
                kinds = arguments.pop("kinds", (MemoryKind.FACTUAL,))
                query = MemoryQuery(query_id="q-1", kinds=kinds, **arguments)
                self.assertEqual(query.query_id, "q-1")


if __name__ == "__main__":
    unittest.main()
