"""An approval's expiry is a fact the Core is shown, not one it must infer.

On 2026-09-11 Friedl approved discarding an orphaned Xero draft bill. The
approval was stamped with the configured ten-minute life. A coding session ran
for twenty, and by the time the Core came to act the grant had lapsed.

The projection showed only lifecycle "granted". Nothing in it said the approval
had expired, so from the Core's side it already held a valid authorisation for
exactly that call and proposing another would have been redundant. It reused
the lapsed identifier, _repeats_rejected_call refused the reuse, and the turn
ended with no response. That happened three times: three typed messages from
Friedl, no reply to any of them.

Both refusals were correct. The runtime knew the approval was dead and said so
to nobody. This projects expires_at alongside the lifecycle so the Core can
tell a live grant from a lapsed one and ask again rather than reuse.

Enforcement is untouched. Approval.permits still decides what may run, the TTL
is unchanged, and an expired approval still fails closed at dispatch. The only
change is that the fact is now told rather than kept.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, CapabilityCall,
    GoalState, GoalSummary, Objective, ReasoningContext, SuccessCriterion,
)
from alx.core.model_reasoner import _context_payload  # noqa: E402

NOW = datetime(2026, 9, 11, 21, 30, tzinfo=UTC)
ARGUMENTS = {"invoice_id": "5181fdd0-21d1-44a8-8db2-755630f5d883"}


def approval(
    approval_id: str = "approval-1", expires_at: datetime | None = None
) -> Approval:
    return Approval(
        approval_id,
        ApprovalScope("delete_xero_draft_bill", ARGUMENTS),
        ApprovalLifecycle.GRANTED,
        expires_at,
    )


def projected(*approvals: Approval) -> list[dict]:
    state = GoalState(
        "goal-1",
        Objective("turn:turn-1", "Discard the orphaned draft bill"),
        (SuccessCriterion("criterion-1", "draft discarded"),),
        approvals=approvals,
    )
    payload = json.loads(_context_payload(ReasoningContext(
        active_goal=state, turns=(), capabilities=(),
        unfinished_goals=(GoalSummary.of(state),), conversation_id="c1",
    )))
    return payload["active_goal"]["approvals"]


class TheExpiryIsProjected(unittest.TestCase):
    def test_a_projected_approval_carries_expires_at(self) -> None:
        entry = projected(approval(expires_at=NOW + timedelta(minutes=10)))[0]
        self.assertIn("expires_at", entry)

    def test_an_approval_without_an_expiry_projects_null(self) -> None:
        """A TTL-less approval is a real shape and must not read as expired."""
        self.assertIsNone(projected(approval())[0]["expires_at"])

    def test_the_existing_fields_are_unchanged(self) -> None:
        entry = projected(approval(expires_at=NOW))[0]
        self.assertEqual(entry["id"], "approval-1")
        self.assertEqual(entry["capability_id"], "delete_xero_draft_bill")
        self.assertEqual(entry["arguments"], ARGUMENTS)
        self.assertEqual(entry["lifecycle"], "granted")


class LiveAndLapsedAreDistinguishable(unittest.TestCase):
    """The whole point: the Core can tell one from the other."""

    def test_an_unexpired_approval_shows_granted_with_a_future_expiry(self) -> None:
        later = NOW + timedelta(minutes=10)
        entry = projected(approval(expires_at=later))[0]
        self.assertEqual(entry["lifecycle"], "granted")
        self.assertEqual(entry["expires_at"], later.isoformat())
        self.assertGreater(datetime.fromisoformat(entry["expires_at"]), NOW)

    def test_an_expired_approval_shows_the_same_lifecycle_and_a_past_expiry(self) -> None:
        """Lifecycle is not rewritten: expiry is a separate, truthful fact.

        The live approval was still GRANTED in durable state after lapsing,
        because nothing revokes it. Reporting it as anything else would be the
        projection inventing a lifecycle the store does not hold.
        """
        earlier = NOW - timedelta(minutes=10)
        entry = projected(approval(expires_at=earlier))[0]
        self.assertEqual(entry["lifecycle"], "granted")
        self.assertEqual(entry["expires_at"], earlier.isoformat())
        self.assertLess(datetime.fromisoformat(entry["expires_at"]), NOW)

    def test_the_two_differ_only_by_their_expiry(self) -> None:
        """So the expiry is the sole signal, and it is present."""
        live = projected(approval(expires_at=NOW + timedelta(minutes=10)))[0]
        dead = projected(approval(expires_at=NOW - timedelta(minutes=10)))[0]
        self.assertNotEqual(live["expires_at"], dead["expires_at"])
        for field in ("id", "capability_id", "arguments", "lifecycle"):
            self.assertEqual(live[field], dead[field])

    def test_the_live_deadlock_case_is_now_visible(self) -> None:
        """The exact approval that stalled three turns."""
        lapsed = datetime(2026, 9, 11, 18, 56, 20, tzinfo=UTC)
        entry = projected(approval("appr-dhl-discard-draft-2", lapsed))[0]
        self.assertEqual(entry["expires_at"], lapsed.isoformat())
        self.assertLess(datetime.fromisoformat(entry["expires_at"]), NOW)


class TheProtocolExplainsTheField(unittest.TestCase):
    def test_the_protocol_states_that_a_lapsed_approval_authorises_nothing(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "carries expires_at",
            "Past that moment it\nauthorises nothing",
            "needs a fresh approval",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)


class EnforcementIsUnchanged(unittest.TestCase):
    """Telling the Core changes nothing about what the runtime permits."""

    def _call(self, approval_id: str = "approval-1") -> CapabilityCall:
        return CapabilityCall(
            "call-1", "delete_xero_draft_bill", ARGUMENTS, approval_id,
        )

    def test_an_expired_approval_still_fails_closed(self) -> None:
        self.assertFalse(
            approval(expires_at=NOW - timedelta(seconds=1)).permits(
                self._call(), NOW
            )
        )

    def test_an_unexpired_approval_still_permits(self) -> None:
        self.assertTrue(
            approval(expires_at=NOW + timedelta(seconds=1)).permits(
                self._call(), NOW
            )
        )

    def test_expiry_is_checked_at_the_boundary_not_in_the_projection(self) -> None:
        """permits() remains the authority; the projection only reports."""
        import inspect
        from alx.contracts.records import Approval as Contract

        source = inspect.getsource(Contract.permits)
        self.assertIn("self.expires_at is None or at <= self.expires_at", source)

    def test_the_ttl_policy_is_unchanged(self) -> None:
        import inspect
        from alx.core.loop import CoreAgent

        source = inspect.getsource(CoreAgent)
        self.assertIn("now + timedelta(seconds=self._approval_ttl_seconds)", source)


if __name__ == "__main__":
    unittest.main()
