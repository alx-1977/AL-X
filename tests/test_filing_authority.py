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
from alx.tools import MAIL, XERO  # noqa: E402
from mail_actions import FILE, READ, mail_arguments  # noqa: E402
from xero_actions import CAPTURE_INVOICE, FIND_BILL, xero_arguments  # noqa: E402


REFERENCE = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "12"}
OTHER = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "99"}


def attempt(*, completed: bool, arguments=None, capability=XERO):
    call = CapabilityCall("call-1", capability,
                          {**(arguments or xero_arguments(CAPTURE_INVOICE, REFERENCE))})
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
        self.assertEqual(scopes[0].capability_id, MAIL)
        self.assertEqual(dict(scopes[0].arguments), mail_arguments(FILE, REFERENCE))

    def test_a_returned_capture_authorises_nothing(self) -> None:
        """A capture that stopped for ambiguity did not process the invoice."""
        self.assertEqual(
            captured_invoice_filing_scopes(goal(attempt(completed=False))), ()
        )

    def test_a_capture_of_one_message_does_not_authorise_another(self) -> None:
        scopes = captured_invoice_filing_scopes(goal(attempt(completed=True)))
        self.assertNotIn(mail_arguments(FILE, OTHER), [dict(item.arguments) for item in scopes])

    def test_reading_a_message_authorises_nothing(self) -> None:
        """Merely looking at mail must not permit filing it."""
        self.assertEqual(
            captured_invoice_filing_scopes(
                goal(attempt(completed=True, capability=MAIL,
                             arguments=mail_arguments(READ, REFERENCE)))
            ),
            (),
        )

    def test_another_xero_action_authorises_nothing(self) -> None:
        """Only a completed capture_invoice of `xero` names a message to file (D-074)."""
        self.assertEqual(
            captured_invoice_filing_scopes(
                goal(attempt(completed=True, arguments=xero_arguments(FIND_BILL, REFERENCE)))
            ),
            (),
        )

    def test_a_capture_saved_before_d074_still_authorises_filing(self) -> None:
        scopes = captured_invoice_filing_scopes(
            goal(attempt(completed=True, capability="capture_supplier_invoice", arguments=REFERENCE)))
        self.assertEqual([dict(item.arguments) for item in scopes], [mail_arguments(FILE, REFERENCE)])

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
            CapabilityCall("call-2", MAIL, mail_arguments(FILE, arguments)),
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

    def test_filing_is_hers_without_a_capture_since_d_051(self) -> None:
        """D-051: filing can be undone, so it no longer waits on a capture or on Friedl."""
        for state, arguments in ((goal(attempt(completed=True)), OTHER),
                                 (goal(attempt(completed=False)), REFERENCE),
                                 (None, REFERENCE)):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.outcome(state, arguments).state, SafetyState.ALLOWED)


if __name__ == "__main__":
    unittest.main()
