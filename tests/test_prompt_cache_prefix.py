"""A cache only reuses an unchanged prefix, so ordering is load-bearing.

The capability catalogue is identical between calls but used to sit in the same
message as the goal and conversation. Anything volatile in front of it stopped
it from ever being reused, so it now has its own stable message ahead of the
task material.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    ConversationOrigin,
    ConversationTurn,
    ModelRole,
    ReasoningContext,
)
from alx.core.model_reasoner import CACHE_KEY, ModelReasoner  # noqa: E402
from alx.tools import XERO_DEFINITIONS  # noqa: E402


class FakeModel:
    def __init__(self) -> None:
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        from alx.contracts import ModelCompletion

        return ModelCompletion(
            "fake",
            "fake-model",
            {
                "action": {
                    "type": "respond",
                    "response": "A normal response.",
                    "response_requires_goal_commit": False,
                },
                "goal_update": None,
                "memory_proposals": [],
            },
            {},
        )


def turn(content: str, index: int) -> ConversationTurn:
    return ConversationTurn(
        conversation_id="conversation-1",
        turn_id=f"turn-{index}",
        origin=ConversationOrigin.SPEECH_TRANSCRIPT,
        content=content,
        occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
        person_id="friedl",
    )


def context(conversation_id: str, content: str) -> ReasoningContext:
    return ReasoningContext(
        active_goal=None,
        turns=(turn(content, 1),),
        capabilities=tuple(XERO_DEFINITIONS),
        memories=(),
        events=(),
        transient_attempts=(),
        conversation_id=conversation_id,
    )


class PrefixStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = FakeModel()
        self.reasoner = ModelReasoner(self.model, "Approved Laws", "Approved identity")

    def test_the_prefix_is_byte_identical_across_different_turns(self) -> None:
        """Different conversation content must not disturb the prefix."""
        self.reasoner.decide(context("conversation-1", "hello"))
        self.reasoner.decide(context("conversation-1", "something else entirely"))
        first, second = self.model.requests
        self.assertEqual(
            [item.content for item in first.messages[:-1]],
            [item.content for item in second.messages[:-1]],
        )

    def test_the_prefix_is_byte_identical_across_conversations(self) -> None:
        """A new conversation reuses the same cached prefix."""
        self.reasoner.decide(context("conversation-1", "hello"))
        self.reasoner.decide(context("conversation-2", "hello"))
        first, second = self.model.requests
        self.assertEqual(
            [item.content for item in first.messages[:-1]],
            [item.content for item in second.messages[:-1]],
        )

    def test_the_catalogue_sits_in_the_prefix_not_beside_the_goal(self) -> None:
        self.reasoner.decide(context("conversation-1", "hello"))
        messages = self.model.requests[0].messages
        self.assertEqual(messages[2].role, ModelRole.SYSTEM)
        self.assertIn("capture_supplier_invoice", messages[2].content)
        # The volatile task material is last and holds no catalogue.
        self.assertEqual(messages[-1].role, ModelRole.USER)
        self.assertNotIn('"capabilities"', messages[-1].content)

    def test_only_the_final_message_changes_between_calls(self) -> None:
        self.reasoner.decide(context("conversation-1", "hello"))
        self.reasoner.decide(context("conversation-1", "a different thing"))
        first, second = self.model.requests
        self.assertNotEqual(first.messages[-1].content, second.messages[-1].content)

    def test_no_timestamp_or_generated_value_enters_the_prefix(self) -> None:
        """A clock or identifier in the prefix would defeat the cache."""
        self.reasoner.decide(context("conversation-1", "hello"))
        prefix = "".join(
            item.content for item in self.model.requests[0].messages[:-1]
        )
        self.assertNotIn("2026-", prefix)
        self.assertNotIn("conversation-1", prefix)
        self.assertNotIn("turn-1", prefix)


class CacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = FakeModel()
        self.reasoner = ModelReasoner(self.model, "Approved Laws", "Approved identity")

    def test_every_conversation_shares_one_cache_key(self) -> None:
        """Keying per conversation split one reusable cache into many."""
        self.reasoner.decide(context("conversation-1", "hello"))
        self.reasoner.decide(context("conversation-2", "hello"))
        keys = {item.cache_key for item in self.model.requests}
        self.assertEqual(keys, {CACHE_KEY})

    def test_telemetry_still_identifies_the_conversation(self) -> None:
        """The cache key must not take over the telemetry and budget key."""
        self.reasoner.decide(context("conversation-1", "hello"))
        self.reasoner.decide(context("conversation-2", "hello"))
        self.assertEqual(
            [item.affinity_key for item in self.model.requests],
            ["conversation-1", "conversation-2"],
        )


class ProviderCacheKeyTests(unittest.TestCase):
    def test_the_provider_sends_the_shared_key_not_the_conversation(self) -> None:
        from unittest.mock import Mock, patch

        from alx.contracts import ModelMessage, ModelRequest
        from alx.providers.openai import OpenAIReasoningModel

        request = ModelRequest(
            (ModelMessage(ModelRole.SYSTEM, "stable"),),
            "alx_core_decision",
            {"type": "object"},
            "conversation-1",
            CACHE_KEY,
        )
        response = Mock(status_code=200)
        response.json.return_value = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "{}"}]}
            ],
            "usage": {},
        }
        response.raise_for_status = Mock()
        model = OpenAIReasoningModel(
            "gpt-5.6-sol", "key", "https://api.openai.com", 10, streaming=False
        )
        with patch.object(model._client, "post", return_value=response) as post:
            model.complete(request)
        self.assertEqual(
            post.call_args.kwargs["json"]["prompt_cache_key"], CACHE_KEY
        )


if __name__ == "__main__":
    unittest.main()
