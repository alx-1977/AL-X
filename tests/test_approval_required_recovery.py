"""Granting the approval a refusal asked for must let the action proceed.

On 2026-09-11 the Core asked Friedl whether it could discard an orphaned Xero
draft bill left by a failed import. He answered "yes please". The turn ended
with state=error, reason=repeated_rejected_call, and said nothing back to him.

The sequence was correct at every step. delete_xero_draft_bill carried no
approval, so the gate refused it approval_required; the Core asked; Friedl
answered; the Core proposed a fresh approval grounded in that turn and retried
the same capability with the same arguments. _repeats_rejected_call then saw a
prior rejection of that exact call whose reason was not in
_CORRECTABLE_REJECTION_REASONS and stopped the turn.

approval_required is the ordinary first-time refusal of a consequential
action: it means nothing authorised this yet. Asking and retrying with what was
granted is the correction that refusal exists to elicit, so it belongs with the
other approval faults the guard already treats as repairable. The identical
defect cost a session on 2026-09-10 through approval_invalid, and the fix then
added only that one reason.

Nothing else changes. The retry still has to carry an approval, it must be a
different identifier from the one already refused, and Approval.permits must
accept it against this exact call at this moment -- lifecycle, identifier,
expiry and scope. A refusal an approval cannot repair, such as a missing policy
or permission, still stands whatever else happened.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, CapabilityAttempt,
    CapabilityAttemptDisposition, CapabilityCall, GoalState, Objective,
    SuccessCriterion,
)
from alx.core.loop import CoreAgent  # noqa: E402

NOW = datetime(2026, 9, 11, 18, 46, tzinfo=UTC)

# The live call: one draft bill, named exactly.
ARGUMENTS = {"invoice_id": "5181fdd0-2b1c-4f6a-9e3d-7c8a1b2d4e5f"}


def call(call_id: str, approval_id: str | None = None) -> CapabilityCall:
    return CapabilityCall(call_id, "delete_xero_draft_bill", ARGUMENTS, approval_id)


def refused(reason: str, approval_id: str | None = None) -> CapabilityAttempt:
    return CapabilityAttempt(
        call("call-1", approval_id),
        CapabilityAttemptDisposition.REJECTED,
        False,
        reason_code=reason,
    )


def approval(
    approval_id: str = "approval-1",
    *,
    lifecycle: ApprovalLifecycle = ApprovalLifecycle.GRANTED,
    expires_at: datetime | None = None,
    arguments: dict | None = None,
) -> Approval:
    return Approval(
        approval_id,
        ApprovalScope("delete_xero_draft_bill",
                      ARGUMENTS if arguments is None else arguments),
        lifecycle,
        expires_at,
    )


def goal(attempts=(), approvals=()) -> GoalState:
    return GoalState(
        "goal-1",
        Objective("turn:turn-1", "Discard the orphaned draft bill"),
        (SuccessCriterion("criterion-1", "draft discarded"),),
        attempts=attempts,
        approvals=approvals,
    )


def repeats(state: GoalState, retry: CapabilityCall, at: datetime = NOW) -> bool:
    return CoreAgent._repeats_rejected_call(state, retry, at)


class TheLiveSequenceRecovers(unittest.TestCase):
    """approval_required, then a real approval, then the same call."""

    def test_a_granted_approval_lets_the_refused_call_retry(self) -> None:
        state = goal(
            attempts=(refused("approval_required"),),
            approvals=(approval(),),
        )
        self.assertFalse(repeats(state, call("call-2", "approval-1")))

    def test_approval_required_is_treated_as_correctable(self) -> None:
        self.assertIn(
            "approval_required", CoreAgent._CORRECTABLE_REJECTION_REASONS
        )


class TheRetryStillNeedsARealApproval(unittest.TestCase):
    """Every barrier after the carve-out is unchanged."""

    def test_a_retry_carrying_no_approval_is_still_blocked(self) -> None:
        state = goal(attempts=(refused("approval_required"),))
        self.assertTrue(repeats(state, call("call-2", None)))

    def test_a_retry_with_no_matching_approval_recorded_is_blocked(self) -> None:
        """An identifier the goal never granted authorises nothing."""
        state = goal(attempts=(refused("approval_required"),))
        self.assertTrue(repeats(state, call("call-2", "approval-invented")))

    def test_reusing_the_refused_approval_identifier_is_blocked(self) -> None:
        state = goal(
            attempts=(refused("approval_invalid", "approval-1"),),
            approvals=(approval("approval-1"),),
        )
        self.assertTrue(repeats(state, call("call-2", "approval-1")))

    def test_a_consumed_approval_is_blocked(self) -> None:
        """CLAIMED means it has already authorised its one action.

        A claimed approval is only a valid goal state alongside the pending
        attempt that claimed it, so the state is built that way rather than
        constructing something the contract forbids.
        """
        claimed = CapabilityAttempt(
            call("call-2", "approval-1"),
            CapabilityAttemptDisposition.PENDING,
            None,
            reason_code="dispatch_pending",
        )
        state = goal(
            attempts=(refused("approval_required"), claimed),
            approvals=(approval(lifecycle=ApprovalLifecycle.CLAIMED),),
        )
        self.assertTrue(repeats(state, call("call-3", "approval-1")))

    def test_an_expired_approval_is_blocked(self) -> None:
        state = goal(
            attempts=(refused("approval_required"),),
            approvals=(approval(expires_at=NOW - timedelta(seconds=1)),),
        )
        self.assertTrue(repeats(state, call("call-2", "approval-1")))

    def test_an_approval_scoped_to_another_action_is_blocked(self) -> None:
        """Approving one bill's deletion does not approve another's."""
        state = goal(
            attempts=(refused("approval_required"),),
            approvals=(approval(arguments={"invoice_id": "some-other-bill"}),),
        )
        self.assertTrue(repeats(state, call("call-2", "approval-1")))

    def test_an_unexpired_approval_still_permits_the_retry(self) -> None:
        """The expiry check is a real bound, not a blanket refusal."""
        state = goal(
            attempts=(refused("approval_required"),),
            approvals=(approval(expires_at=NOW + timedelta(minutes=5)),),
        )
        self.assertFalse(repeats(state, call("call-2", "approval-1")))


class RefusalsAnApprovalCannotRepairStillStand(unittest.TestCase):
    """The gate's non-approval denials are not corrected by approving."""

    def test_a_missing_permission_still_blocks_the_retry(self) -> None:
        state = goal(
            attempts=(refused("permission_missing"),),
            approvals=(approval(),),
        )
        self.assertTrue(repeats(state, call("call-2", "approval-1")))

    def test_a_missing_or_disabled_policy_still_blocks_the_retry(self) -> None:
        for reason in ("policy_missing", "policy_denied"):
            with self.subTest(reason=reason):
                state = goal(
                    attempts=(refused(reason),), approvals=(approval(),),
                )
                self.assertTrue(repeats(state, call("call-2", "approval-1")))

    def test_one_unrepairable_refusal_blocks_even_beside_a_repairable_one(self) -> None:
        state = goal(
            attempts=(refused("approval_required"), refused("permission_missing")),
            approvals=(approval(),),
        )
        self.assertTrue(repeats(state, call("call-2", "approval-1")))


class ExistingBehaviourIsUnchanged(unittest.TestCase):
    def test_approval_invalid_recovery_still_works(self) -> None:
        """The 2026-09-10 fix is untouched by this one."""
        state = goal(
            attempts=(refused("approval_invalid", "approval-stale"),),
            approvals=(approval("approval-2"),),
        )
        self.assertFalse(repeats(state, call("call-2", "approval-2")))

    def test_a_call_with_no_prior_rejection_is_never_a_repeat(self) -> None:
        self.assertFalse(repeats(goal(), call("call-1", "approval-1")))

    def test_a_different_action_is_not_a_repeat(self) -> None:
        """Identity is the capability and its arguments."""
        state = goal(attempts=(refused("approval_required"),))
        other = CapabilityCall(
            "call-2", "delete_xero_draft_bill", {"invoice_id": "another"}, None,
        )
        self.assertFalse(repeats(state, other))

    def test_the_correctable_set_holds_only_approval_faults(self) -> None:
        """Nothing a fresh approval cannot actually repair crept in."""
        self.assertEqual(
            CoreAgent._CORRECTABLE_REJECTION_REASONS,
            frozenset({
                "approval_required",
                "approval_invalid",
                "approval_call_id_mismatch",
                "approval_scope_mismatch",
                "approval_id_reused",
                "approval_source_missing",
                "approval_source_not_latest_person_turn",
            }),
        )


if __name__ == "__main__":
    unittest.main()
