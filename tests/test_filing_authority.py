"""Filing authority comes from a capture that actually completed.

D-020 authorises filing a processed supplier invoice. Granting the capability
outright let any identified message be moved to the processed folder, whether
or not its invoice had been captured, so the authority is now an exact standing
scope derived from the capability result rather than from anything asserted.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.mail import (  # noqa: E402
    build_mail_runtime,
    captured_invoice_filing_scopes,
)
from alx.config import MailSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    GoalState,
    Objective,
    SuccessCriterion,
)
from alx.safety import AuthorityContext, SafetyGate, SafetyState  # noqa: E402
from alx.tools import CAPTURE_SUPPLIER_INVOICE, FILE_PROCESSED_MAIL_MESSAGE  # noqa: E402


REFERENCE = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "12"}
OTHER = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "99"}


def attempt(*, completed: bool, arguments=None, capability=CAPTURE_SUPPLIER_INVOICE):
    call = CapabilityCall("call-1", capability, {**(arguments or REFERENCE)})
    return CapabilityAttempt(
        call=call,
        disposition=CapabilityAttemptDisposition.EXECUTED,
        implementation_invoked=True,
        result=CapabilityResult(
            "call-1",
            capability,
            CapabilityResultState.SUCCEEDED,
            {"completed": completed, "returned_for": "" if completed else "duplicate_bill"},
        ),
    )


def goal(*attempts) -> GoalState:
    return GoalState(
        goal_id="goal-1",
        objective=Objective("turn:1", "process the supplier invoices"),
        success_criteria=(SuccessCriterion("criterion-1", "bill in Xero"),),
        attempts=tuple(attempts),
    )


class ScopeDerivationTests(unittest.TestCase):
    def test_a_completed_capture_authorises_filing_that_message(self) -> None:
        scopes = captured_invoice_filing_scopes(goal(attempt(completed=True)))
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].capability_id, FILE_PROCESSED_MAIL_MESSAGE)
        self.assertEqual(dict(scopes[0].arguments), REFERENCE)

    def test_a_returned_capture_authorises_nothing(self) -> None:
        """A capture that stopped for ambiguity did not process the invoice."""
        self.assertEqual(
            captured_invoice_filing_scopes(goal(attempt(completed=False))), ()
        )

    def test_a_capture_of_one_message_does_not_authorise_another(self) -> None:
        scopes = captured_invoice_filing_scopes(goal(attempt(completed=True)))
        self.assertNotIn(OTHER, [dict(item.arguments) for item in scopes])

    def test_reading_a_message_authorises_nothing(self) -> None:
        """Merely looking at mail must not permit filing it."""
        self.assertEqual(
            captured_invoice_filing_scopes(
                goal(attempt(completed=True, capability="read_mail_message"))
            ),
            (),
        )

    def test_no_goal_authorises_nothing(self) -> None:
        self.assertEqual(captured_invoice_filing_scopes(None), ())


class GateEnforcementTests(unittest.TestCase):
    """The scope must actually decide what the safety gate permits."""

    def gate(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                MailSettings(
                    "friedl@example.test",
                    "secret",
                    "imap.example.test",
                    993,
                    15,
                    "FireFli/Processed",
                ),
                Path(directory),
                lambda: "call-1",
            )
        return SafetyGate(runtime.policies), runtime.permissions

    def outcome(self, state, arguments):
        gate, permissions = self.gate()
        return gate.evaluate(
            CapabilityCall("call-2", FILE_PROCESSED_MAIL_MESSAGE, arguments),
            AuthorityContext(
                "friedl",
                frozenset(permissions),
                datetime(2026, 8, 31, tzinfo=UTC),
                standing_scopes=captured_invoice_filing_scopes(state),
            ),
        )

    def test_filing_the_captured_message_is_allowed(self) -> None:
        self.assertEqual(
            self.outcome(goal(attempt(completed=True)), REFERENCE).state,
            SafetyState.ALLOWED,
        )

    def test_filing_any_other_message_is_not_allowed(self) -> None:
        """The defect: any identified message could be moved."""
        self.assertNotEqual(
            self.outcome(goal(attempt(completed=True)), OTHER).state,
            SafetyState.ALLOWED,
        )

    def test_filing_without_a_completed_capture_is_not_allowed(self) -> None:
        self.assertNotEqual(
            self.outcome(goal(attempt(completed=False)), REFERENCE).state,
            SafetyState.ALLOWED,
        )

    def test_filing_with_no_goal_at_all_is_not_allowed(self) -> None:
        self.assertNotEqual(
            self.outcome(None, REFERENCE).state, SafetyState.ALLOWED
        )


if __name__ == "__main__":
    unittest.main()
