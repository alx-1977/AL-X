"""The catalogue is the authoritative namespace of what Core may call.

On 2026-09-10 Core told Friedl it could not find or access the delete
capability. `move_mail_message_to_trash` was in the catalogue message sent on
that same reasoning step, and the identical id existed in the contract, the
registry, the broker, the executors and the authority policy. No call was
emitted and no authority filtering happened, so nothing had removed it: the
claim of absence was false.

The cause was an omission in how the catalogue is presented. It arrived as a
bare JSON object whose meaning had to be inferred. Nothing stated that its ids
are exact, that presence means available, or that absence is the only ground
for reporting a capability missing. The only nearby rule guarded the opposite
direction, forbidding a call to something unregistered.

These tests bind the contract and the state representation. They deliberately
do not assert that a nondeterministic model will never utter a particular
sentence; they prove the catalogue reaches Core under the real id, that the
namespace Core is shown is the namespace dispatch resolves against, that the
protocol states both directions of the rule, that an absent capability stays
absent, and that vanished mail remains a fact about a message rather than
about a capability.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.mail import build_mail_runtime  # noqa: E402
from alx.capabilities import CapabilityRegistry  # noqa: E402
from alx.config import MailSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    ConversationOrigin, ConversationTurn, GoalState, GoalSummary, ModelCompletion,
    Objective, ReasoningContext, SuccessCriterion,
)
from alx.core import ModelReasoner  # noqa: E402
from alx.core.model_reasoner import (  # noqa: E402
    PROTOCOL_INSTRUCTIONS, decision_schema,
)
from alx.tools import MOVE_MAIL_MESSAGE_TO_TRASH  # noqa: E402

from datetime import UTC, datetime  # noqa: E402

NOW = datetime(2026, 9, 10, tzinfo=UTC)
TRASH = "move_mail_message_to_trash"


def mail_settings() -> MailSettings:
    return MailSettings(
        address="friedl@example.test",
        secret="unused",
        imap_host="imap.example.test",
        imap_port=993,
        poll_seconds=15,
        processed_mailbox="",
    )


class FakeModel:
    """Records the exact messages the reasoner sends."""

    def __init__(self, output) -> None:
        self.output = output
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelCompletion("fake", "fake-model", self.output)


def response_output(text: str = "A normal response.") -> dict:
    return {
        "goal_id": None,
        "goal_update": None,
        "memory_proposals": [],
        "action": {
            "type": "respond",
            "response": text,
            "response_requires_goal_commit": False,
            "claimed_completed_actions": [],
            "unfinished_actions": [],
        },
    }


def goal() -> GoalState:
    return GoalState(
        "goal-1", Objective("turn:turn-1", "clear the two messages"),
        (SuccessCriterion("criterion-1", "inbox settled"),),
    )


def context(definitions) -> ReasoningContext:
    state = goal()
    return ReasoningContext(
        state,
        (ConversationTurn(
            "conversation-1", "turn-1", ConversationOrigin.SPEECH_TRANSCRIPT,
            "Delete those two", NOW, "friedl",
        ),),
        tuple(definitions),
        unfinished_goals=(GoalSummary.of(state),),
    )


def catalogue_of(request) -> dict:
    """The one catalogue message this request carries.

    Found by content rather than by index, so the assertion survives the
    messages being reordered and fails rather than silently reading the wrong
    one if the catalogue ever stops being sent.
    """
    found = [
        message.content for message in request.messages
        if message.content.lstrip().startswith("{")
        and "capabilities" in json.loads(message.content)
    ]
    if len(found) != 1:
        raise AssertionError(f"expected one catalogue message, found {len(found)}")
    return json.loads(found[0])


class LiveDefinitionReachesCore(unittest.TestCase):
    """1. The real definition arrives under the exact live id."""

    def test_the_live_trash_definition_reaches_core_under_its_exact_id(self) -> None:
        """Built by the real bootstrap, registered, listed, and sent.

        Nothing here constructs a stand-in definition. The path is the
        production one: build_mail_runtime produces the definition, the real
        registry holds it, list_definitions is what composition passes to the
        Core, and the reasoner serialises it into the message the model reads.
        """
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                mail_settings(), Path(directory), lambda: "call-1"
            )
            registry = CapabilityRegistry()
            for definition in runtime.definitions:
                registry.register(definition)
            model = FakeModel(response_output())
            ModelReasoner(model, "laws", "identity").decide(
                context(registry.list_definitions())
            )

        catalogue = catalogue_of(model.requests[0])
        entries = {item["id"]: item for item in catalogue["capabilities"]}
        # The exact identifier, not a near miss, and not a description.
        self.assertIn(TRASH, entries)
        self.assertEqual(TRASH, MOVE_MAIL_MESSAGE_TO_TRASH)
        entry = entries[TRASH]
        # Enough for Core to call it without guessing anything.
        self.assertEqual(entry["side_effect"], "effectful")
        self.assertEqual(
            sorted(entry["input_schema"]["required"]),
            ["mailbox_id", "uid", "uid_validity"],
        )

    def test_the_catalogue_states_that_its_entries_are_available_and_exact(self) -> None:
        """The message carries its own meaning rather than needing inference."""
        model = FakeModel(response_output())
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                mail_settings(), Path(directory), lambda: "call-1"
            )
            ModelReasoner(model, "laws", "identity").decide(
                context(runtime.definitions)
            )
        semantics = catalogue_of(model.requests[0])["catalogue_semantics"]
        self.assertIn("authoritative namespace", semantics)
        self.assertIn("Each id is exact", semantics)
        self.assertIn("available now", semantics)
        self.assertIn("not listed here does not exist", semantics)


class CatalogueAndDispatchAgree(unittest.TestCase):
    """2. What Core is offered is what dispatch will resolve."""

    def test_the_offered_namespace_is_the_dispatched_namespace(self) -> None:
        """A divergence either way is the defect.

        An id shown but not dispatchable invites a call that cannot run; an id
        dispatchable but not shown is a capability Core can only reach by
        inventing it. Both are failures of the same invariant.
        """
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                mail_settings(), Path(directory), lambda: "call-1"
            )
            registry = CapabilityRegistry()
            for definition in runtime.definitions:
                registry.register(definition)
            model = FakeModel(response_output())
            ModelReasoner(model, "laws", "identity").decide(
                context(registry.list_definitions())
            )
            offered = {
                item["id"] for item in catalogue_of(model.requests[0])["capabilities"]
            }
            registered = {
                item.capability_id for item in registry.list_definitions()
            }
            self.assertEqual(offered, registered)
            # Every offered id has an implementation and a recorded authority,
            # so presence in the catalogue is not merely a description.
            self.assertEqual(offered, set(runtime.executors))
            self.assertTrue(offered <= set(runtime.policies) | {
                item.capability_id for item in runtime.definitions
                if item.capability_id not in runtime.policies
            })
            self.assertIn(TRASH, runtime.policies)

    def test_core_resolves_a_called_id_against_the_same_offered_sequence(self) -> None:
        """The reasoner looks the call up in context.capabilities itself."""
        import inspect
        from alx.core import model_reasoner
        from alx.core.loop import CoreAgent

        self.assertIn(
            "item.capability_id == action[\"capability_id\"]",
            inspect.getsource(model_reasoner.ModelReasoner._decide),
        )
        self.assertIn(
            "item.capability_id == capability_id",
            inspect.getsource(CoreAgent._definition),
        )


class ProtocolStatesTheRule(unittest.TestCase):
    """3. The protocol says presented capabilities are available and exact."""

    def test_the_protocol_states_both_directions(self) -> None:
        for stated in (
            "authoritative namespace of what you\ncan call on this step",
            "ids are exact strings",
            "Every entry in it is a capability currently available to you",
            "may not\nsay that it does not exist",
            "cannot find it",
            "no access to it",
            "does not exist for you",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_protocol_requires_the_actual_reason_for_declining(self) -> None:
        """Declining is fine; inventing unavailability to explain it is not."""
        for stated in (
            "Choosing not to call an offered\ncapability is ordinary",
            "give the\nactual reason",
            "say that about the thing, not about the capability",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_decision_schema_states_the_same_rule(self) -> None:
        """The field Core fills carries it too, not only the prose."""
        variants = decision_schema()["properties"]["action"]["anyOf"]
        described = [
            item["properties"]["capability_id"]["description"]
            for item in variants
            if item["properties"].get("type", {}).get("const") == "call_capability"
        ]
        self.assertEqual(len(described), 1)
        self.assertIn("exactly as written", described[0])
        self.assertIn("available to call now", described[0])
        self.assertIn("must never be invented", described[0])


class AnAbsentCapabilityStaysAbsent(unittest.TestCase):
    """4. Absence is real, and must not be invented around."""

    def test_a_capability_withheld_from_the_context_is_not_offered(self) -> None:
        """The catalogue reflects what this step was actually given."""
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                mail_settings(), Path(directory), lambda: "call-1"
            )
            without_trash = tuple(
                item for item in runtime.definitions
                if item.capability_id != TRASH
            )
            model = FakeModel(response_output())
            ModelReasoner(model, "laws", "identity").decide(context(without_trash))
        offered = {
            item["id"] for item in catalogue_of(model.requests[0])["capabilities"]
        }
        self.assertNotIn(TRASH, offered)
        # Reading mail was never withheld, so the absence is specific rather
        # than the whole catalogue having gone missing.
        self.assertIn("read_mail_message", offered)

    def test_an_invented_capability_cannot_be_dispatched(self) -> None:
        """The broker resolves against the registry, so invention fails."""
        from alx.capabilities import CapabilityBroker
        from alx.contracts import (
            CapabilityAttemptDisposition, CapabilityCall,
        )
        from alx.safety import AuthorityContext, SafetyGate

        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                mail_settings(), Path(directory), lambda: "call-1"
            )
            registry = CapabilityRegistry()
            for definition in runtime.definitions:
                if definition.capability_id != TRASH:
                    registry.register(definition)
            broker = CapabilityBroker(
                registry, SafetyGate(dict(runtime.policies)), dict(runtime.executors)
            )
            attempt = broker.dispatch(
                CapabilityCall("call-1", TRASH, {
                    "mailbox_id": "INBOX", "uid_validity": "777", "uid": "59093",
                }),
                AuthorityContext("friedl", frozenset(runtime.permissions), NOW),
            )
        self.assertEqual(
            attempt.disposition, CapabilityAttemptDisposition.BROKER_FAILURE
        )
        self.assertEqual(attempt.reason_code, "capability_unknown")
        self.assertFalse(attempt.implementation_invoked)


class VanishedMailIsNotCapabilityAbsence(unittest.TestCase):
    """5. A gone message and a missing capability are different facts."""

    def test_a_vanished_observation_leaves_the_capability_offered(self) -> None:
        """The live shape: UIDs gone from INBOX, trash still callable.

        This is the distinction the failed turn collapsed. The messages were
        genuinely gone, which is a fact about them; the capability that would
        have moved them was present throughout.
        """
        from alx.contracts import BackgroundEvent

        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                mail_settings(), Path(directory), lambda: "call-1"
            )
            state = goal()
            vanished = tuple(
                BackgroundEvent(
                    f"mail:777:{uid}:vanished", "mail.message_vanished", NOW,
                    {"mailbox_id": "INBOX", "uid_validity": "777", "uid": uid},
                )
                for uid in ("59093", "59094")
            )
            model = FakeModel(response_output())
            ModelReasoner(model, "laws", "identity").decide(ReasoningContext(
                state,
                (ConversationTurn(
                    "conversation-1", "turn-1",
                    ConversationOrigin.SPEECH_TRANSCRIPT,
                    "Delete those two", NOW, "friedl",
                ),),
                runtime.definitions,
                events=vanished,
                unfinished_goals=(GoalSummary.of(state),),
            ))

        request = model.requests[0]
        # The capability is offered on exactly the step where the messages are
        # reported gone.
        offered = {item["id"] for item in catalogue_of(request)["capabilities"]}
        self.assertIn(TRASH, offered)
        supplied = json.loads(request.messages[-1].content)
        kinds = {item["kind"] for item in supplied["background_events"]}
        self.assertEqual(kinds, {"mail.message_vanished"})
        # The messages are gone; the capability that moves them is not.
        self.assertEqual(
            {item["durable_data"]["uid"] for item in supplied["background_events"]},
            {"59093", "59094"},
        )

    def test_a_vanished_message_keeps_its_pending_observation(self) -> None:
        """Deliberate semantics, unchanged: release is AL/X's, through the capability.

        The live rows for 59093 and 59094 were state=pending with
        reported_vanished=2, and that combination stays valid until she
        acknowledges the observation. Nothing here expires it, and detecting
        the disappearance does not release it. This test exists so a later
        change to the catalogue cannot quietly buy its truthfulness by
        discarding this state instead.
        """
        from alx.providers import SQLiteMailObservationState

        def observed(uid: int) -> tuple[int, dict[str, str]]:
            return (uid, {
                "mailbox_id": "INBOX", "uid_validity": "777", "uid": str(uid),
                "observed_at": "2026-09-10T06:00:00+00:00",
                "subject": f"Message {uid}", "sender": "someone@example.test",
            })

        with tempfile.TemporaryDirectory() as directory:
            observations = SQLiteMailObservationState(
                Path(directory) / "mail-observations.sqlite3"
            )
            self.addCleanup(observations.close)
            observations.new_identifiers("INBOX", "777", ())
            uids = (59093, 59094)
            observations.discover(
                "INBOX", "777", tuple(observed(uid) for uid in uids), uids
            )
            # Shown to her as waiting, then gone from INBOX.
            observations.contextual_events()
            self.assertEqual(observations.reconcile("INBOX", "777", ()), 2)
            events = observations.pending_vanished()
            self.assertEqual(
                [item.data["uid"] for item in events], ["59093", "59094"]
            )
            for event in events:
                self.assertTrue(
                    observations.record_vanished_delivery(event.event_id)
                )
            # Carried once, and not offered again.
            self.assertEqual(observations.pending_vanished(), ())
            rows = {
                int(uid): (state, vanished)
                for uid, state, vanished in observations._connection.execute(
                    "SELECT uid, state, reported_vanished FROM mail_observations"
                )
            }
            for uid in uids:
                # state=pending + reported_vanished=2, exactly as the live
                # rows stood. Still held, because only she releases it.
                self.assertEqual(rows[uid], ("pending", 2))


if __name__ == "__main__":
    unittest.main()
