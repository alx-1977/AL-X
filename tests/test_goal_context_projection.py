"""What reaches the reasoning prompt is a projection, not the durable record.

A long-running goal accumulated 65 attempts with full result values and resent
all of them on every reasoning call, so each call cost more than the last. The
prompt now carries recent detail plus a summary of the rest. Nothing durable is
truncated: restart and audit still see every attempt.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    GoalState,
    Objective,
    ProgressRecord,
    SuccessCriterion,
)
from alx.core.model_reasoner import (  # noqa: E402
    VERBATIM_ATTEMPTS,
    VERBATIM_HISTORY,
    _state_payload,
)
from alx.goals import SQLiteGoalStore  # noqa: E402


def attempt(index: int, *, failed: bool = False) -> CapabilityAttempt:
    call = CapabilityCall(f"call-{index}", "list_xero_accounts", {})
    result = CapabilityResult(
        f"call-{index}",
        "list_xero_accounts",
        CapabilityResultState.FAILED if failed else CapabilityResultState.SUCCEEDED,
        {} if failed else {"accounts": [{"code": str(n)} for n in range(40)]},
        failure={"code": "rate_limited"} if failed else None,
    )
    return CapabilityAttempt(
        call=call,
        disposition=CapabilityAttemptDisposition.EXECUTED,
        implementation_invoked=True,
        result=result,
    )


def goal(attempts: int = 65, progress: int = 40) -> GoalState:
    return GoalState(
        goal_id="goal-1",
        objective=Objective("turn:1", "capture the supplier invoices"),
        success_criteria=(SuccessCriterion("criterion-1", "bill exists in Xero"),),
        attempts=tuple(
            attempt(index, failed=index % 9 == 0) for index in range(attempts)
        ),
        progress=tuple(
            ProgressRecord(f"progress-{index}", f"step {index}", ())
            for index in range(progress)
        ),
    )


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = goal()
        self.payload = _state_payload(self.state)

    def test_only_recent_attempts_are_sent_verbatim(self) -> None:
        self.assertEqual(len(self.payload["attempts"]), VERBATIM_ATTEMPTS)
        self.assertEqual(
            [item["call_id"] for item in self.payload["attempts"]],
            [f"call-{index}" for index in range(65 - VERBATIM_ATTEMPTS, 65)],
            "the most recent attempts must be the ones kept",
        )

    def test_recent_attempts_keep_their_full_result_values(self) -> None:
        """Recent detail is what the next decision is actually made from."""
        latest = self.payload["attempts"][-1]
        self.assertEqual(len(latest["result_values"]["accounts"]), 40)

    def test_older_attempts_become_a_compact_summary(self) -> None:
        summary = self.payload["older_attempts"]
        self.assertEqual(summary["count"], 65 - VERBATIM_ATTEMPTS)
        self.assertIn("list_xero_accounts:succeeded", summary["outcomes"])
        self.assertIn("rate_limited", summary["failure_codes"])

    def test_older_failures_are_never_silently_dropped(self) -> None:
        """A failure that shaped the goal must still be visible."""
        self.assertTrue(self.payload["older_attempts"]["failure_codes"])

    def test_a_short_goal_sends_everything_and_summarises_nothing(self) -> None:
        payload = _state_payload(goal(attempts=3, progress=2))
        self.assertEqual(len(payload["attempts"]), 3)
        self.assertIsNone(payload["older_attempts"])
        self.assertEqual(payload["older_progress_count"], 0)

    def test_progress_is_projected_with_a_visible_remainder(self) -> None:
        self.assertEqual(len(self.payload["progress"]), VERBATIM_HISTORY)
        self.assertEqual(self.payload["older_progress_count"], 40 - VERBATIM_HISTORY)

    def test_decision_critical_state_is_never_projected_away(self) -> None:
        """Objective, status, criteria, approvals and evidence stay whole."""
        for key in (
            "goal_id",
            "objective",
            "success_criteria",
            "status",
            "stop_reason",
            "blockers",
            "outstanding_work",
            "approvals",
            "evidence",
            "decisions",
            "corrections",
            "referents",
            "context",
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.payload)

    def test_the_projection_is_materially_smaller(self) -> None:
        full = json.dumps(
            [
                {
                    "call_id": item.call.call_id,
                    "capability_id": item.call.capability_id,
                    "disposition": item.disposition.value,
                    "result_state": item.result.state.value,
                    "result_values": json.loads(json.dumps(
                        item.result.values, default=dict
                    )),
                }
                for item in self.state.attempts
            ],
            separators=(",", ":"),
        )
        projected = json.dumps(
            {
                "attempts": self.payload["attempts"],
                "older_attempts": self.payload["older_attempts"],
            },
            separators=(",", ":"),
        )
        self.assertLess(len(projected), len(full) // 2)


class DurableHistoryTests(unittest.TestCase):
    """Projection must not touch what is stored, recovered, or auditable."""

    def test_every_attempt_survives_a_restart(self) -> None:
        """Audit and restart must still see all 65, not the projected 8."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite3"
            store = SQLiteGoalStore(path)
            state = goal()
            store.create(
                state, "conversation-1", datetime.now(UTC) + timedelta(days=30)
            )
            # The prompt saw a projection while this was the active goal.
            self.assertEqual(len(_state_payload(state)["attempts"]), VERBATIM_ATTEMPTS)
            store.close()

            reopened = SQLiteGoalStore(path)
            recovered = reopened.load("goal-1").state
            self.assertEqual(len(recovered.attempts), 65)
            self.assertEqual(len(recovered.progress), 40)
            self.assertEqual(
                [item.call.call_id for item in recovered.attempts],
                [f"call-{index}" for index in range(65)],
            )
            reopened.close()

    def test_projection_reads_the_state_without_changing_it(self) -> None:
        state = goal()
        payload_one = _state_payload(state)
        payload_two = _state_payload(state)
        self.assertEqual(payload_one, payload_two)
        self.assertEqual(len(state.attempts), 65)

    def test_the_durable_record_still_holds_the_summarised_attempts(self) -> None:
        """The summary is a view; the originals remain addressable."""
        state = goal()
        payload = _state_payload(state)
        summarised = payload["older_attempts"]["count"]
        self.assertEqual(summarised, 65 - VERBATIM_ATTEMPTS)
        # Every summarised attempt is still present on the durable state.
        self.assertEqual(len(state.attempts), 65)
        self.assertTrue(
            all(item.call is not None for item in state.attempts[:summarised])
        )


if __name__ == "__main__":
    unittest.main()
