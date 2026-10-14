"""D-020 (amended 2026-10-14): DHL emails are filed, flagged while drafted.

Friedl: once a DHL import is drafted, its documents email is filed and
flagged orange; the flag is cleared when the bill is authorised. Authority
for each step comes only from a DHL result naming the exact message and
colour, and the gate enforces it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alx.bootstrap.mail import build_mail_runtime, dhl_mail_scopes  # noqa: E402
from alx.config import MailSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    GoalState,
    MailAccessError,
    MailReference,
    Objective,
    SuccessCriterion,
)
from alx.safety import AuthorityContext, SafetyGate, SafetyState  # noqa: E402
from alx.tools import (  # noqa: E402
    FILE_PROCESSED_MAIL_MESSAGE,
    PROCESS_DHL_IMPORT,
    SET_MAIL_FLAG_COLOUR,
    build_mail_executors,
)

DOCS = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "60692"}
FILED_DOCS = {"mailbox_id": "FireFli/Processed", "uid_validity": "888", "uid": "41"}
INVOICE = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "60801"}
OTHER = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "99"}


def dhl(stage: str, *, completed: bool = True, source=(DOCS,), customs=()):
    values = {"completed": completed, "stage": stage, "source_mail": list(source)}
    if customs:
        values["customs_mail"] = list(customs)
    call = CapabilityCall("call-1", PROCESS_DHL_IMPORT, {"documents": []})
    return CapabilityAttempt(
        call=call,
        disposition=CapabilityAttemptDisposition.EXECUTED,
        implementation_invoked=True,
        result=CapabilityResult(
            "call-1", PROCESS_DHL_IMPORT, CapabilityResultState.SUCCEEDED, values
        ),
    )


def goal(*attempts) -> GoalState:
    return GoalState(
        goal_id="goal-1",
        objective=Objective("turn:1", "process the DHL import"),
        success_criteria=(SuccessCriterion("criterion-1", "bill in Xero"),),
        attempts=tuple(attempts),
    )


def scopes_of(state):
    return {(item.capability_id, tuple(sorted(dict(item.arguments).items())))
            for item in dhl_mail_scopes(state)}


def scope(capability, arguments):
    return (capability, tuple(sorted(arguments.items())))


class ScopeDerivationTests(unittest.TestCase):
    def test_a_drafted_import_authorises_filing_and_an_orange_flag(self) -> None:
        scopes = scopes_of(goal(dhl("customs_documents")))
        self.assertEqual(scopes, {
            scope(FILE_PROCESSED_MAIL_MESSAGE, DOCS),
            scope(SET_MAIL_FLAG_COLOUR, {**DOCS, "colour": "orange"}),
        })

    def test_an_authorised_bill_authorises_filing_its_invoice_and_clearing_the_flag(self) -> None:
        scopes = scopes_of(goal(dhl("dhl_invoice", source=(INVOICE,), customs=(FILED_DOCS,))))
        self.assertEqual(scopes, {
            scope(FILE_PROCESSED_MAIL_MESSAGE, INVOICE),
            scope(SET_MAIL_FLAG_COLOUR, {**FILED_DOCS, "colour": "none"}),
        })

    def test_a_duty_tax_paid_bill_is_filed_but_never_flagged(self) -> None:
        scopes = scopes_of(goal(dhl("dhl_duty_tax_invoice", source=(INVOICE,))))
        self.assertEqual(scopes, {scope(FILE_PROCESSED_MAIL_MESSAGE, INVOICE)})

    def test_a_returned_import_authorises_nothing(self) -> None:
        self.assertEqual(dhl_mail_scopes(goal(dhl("customs_documents", completed=False))), ())

    def test_no_goal_authorises_nothing(self) -> None:
        self.assertEqual(dhl_mail_scopes(None), ())


class GateEnforcementTests(unittest.TestCase):
    def outcome(self, state, capability, arguments):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_mail_runtime(
                MailSettings("friedl@example.test", "secret", "imap.example.test",
                             993, 15, "FireFli/Processed"),
                Path(directory),
                lambda: "call-1",
            )
        return SafetyGate(runtime.policies).evaluate(
            CapabilityCall("call-2", capability, arguments),
            AuthorityContext("friedl", frozenset(runtime.permissions),
                             datetime(2026, 10, 14, tzinfo=UTC),
                             standing_scopes=dhl_mail_scopes(state)),
        ).state

    def test_flagging_the_drafted_documents_email_orange_is_allowed(self) -> None:
        state = goal(dhl("customs_documents"))
        self.assertEqual(
            self.outcome(state, SET_MAIL_FLAG_COLOUR, {**DOCS, "colour": "orange"}),
            SafetyState.ALLOWED,
        )
        self.assertEqual(
            self.outcome(state, FILE_PROCESSED_MAIL_MESSAGE, DOCS), SafetyState.ALLOWED
        )

    def test_an_orange_scope_cannot_clear_or_recolour(self) -> None:
        state = goal(dhl("customs_documents"))
        self.assertNotEqual(
            self.outcome(state, SET_MAIL_FLAG_COLOUR, {**DOCS, "colour": "none"}),
            SafetyState.ALLOWED,
        )

    def test_another_message_cannot_be_flagged_or_filed(self) -> None:
        state = goal(dhl("customs_documents"))
        self.assertNotEqual(
            self.outcome(state, SET_MAIL_FLAG_COLOUR, {**OTHER, "colour": "orange"}),
            SafetyState.ALLOWED,
        )
        self.assertNotEqual(
            self.outcome(state, FILE_PROCESSED_MAIL_MESSAGE, OTHER), SafetyState.ALLOWED
        )

    def test_clearing_needs_the_authorised_bill(self) -> None:
        self.assertEqual(
            self.outcome(goal(dhl("dhl_invoice", source=(INVOICE,), customs=(FILED_DOCS,))),
                         SET_MAIL_FLAG_COLOUR, {**FILED_DOCS, "colour": "none"}),
            SafetyState.ALLOWED,
        )
        self.assertNotEqual(
            self.outcome(goal(dhl("customs_documents")),
                         SET_MAIL_FLAG_COLOUR, {**FILED_DOCS, "colour": "none"}),
            SafetyState.ALLOWED,
        )


class FlagAccount:
    def __init__(self, failure: str = "") -> None:
        self.flags = []
        self.failure = failure

    def set_flag_colour(self, reference, colour):
        if self.failure:
            raise MailAccessError(self.failure)
        self.flags.append((reference, colour))


class FlagCapabilityTests(unittest.TestCase):
    def run_flag(self, account, colour):
        return build_mail_executors(account, account, lambda: "call-1")[SET_MAIL_FLAG_COLOUR](
            {**DOCS, "colour": colour}
        )

    def test_orange_and_none_reach_the_account(self) -> None:
        account = FlagAccount()
        for colour in ("orange", "none"):
            result = self.run_flag(account, colour)
            self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
            self.assertEqual(result.values["colour"], colour)
        self.assertEqual([colour for _ref, colour in account.flags], ["orange", "none"])
        self.assertEqual(account.flags[0][0], MailReference("INBOX", "777", "60692"))

    def test_any_other_colour_is_refused_before_the_account(self) -> None:
        account = FlagAccount()
        for colour in ("red", "", None):
            result = self.run_flag(account, colour)
            self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(account.flags, [])

    def test_a_mail_failure_is_returned_structured(self) -> None:
        result = self.run_flag(FlagAccount("flag_update_failed"), "orange")
        self.assertEqual(result.failure["code"], "flag_update_failed")


class FlagImap:
    """Records the IMAP commands the adapter sends."""

    def __init__(self, found: bytes = b"41 42") -> None:
        self.commands = []
        self.found = found

    def login(self, *_):
        return "OK", []

    def logout(self):
        return "BYE", []

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"777"]

    def uid(self, operation, *values):
        self.commands.append(("UID", operation, *values))
        if operation == "STORE":
            return "OK", [b""]
        if operation == "SEARCH":
            return "OK", [self.found]
        raise AssertionError(operation)


class AdapterTests(unittest.TestCase):
    def adapter(self, imap):
        from alx.providers.icloud_mail import ICloudMailAdapter, SQLiteMailObservationState
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return ICloudMailAdapter(
            "imap.example.test", 993, "friedl@example.test", "secret",
            SQLiteMailObservationState(Path(directory.name) / "observations.sqlite3"),
            15, connection_factory=lambda *_args, **_kwargs: imap,
        )

    def stores(self, imap):
        return [item[2:] for item in imap.commands if item[:2] == ("UID", "STORE")]

    def test_orange_clears_every_colour_then_sets_apple_mails_orange(self) -> None:
        imap = FlagImap()
        self.adapter(imap).set_flag_colour(MailReference("INBOX", "777", "60692"), "orange")
        self.assertEqual(self.stores(imap), [
            ("60692", "-FLAGS.SILENT",
             r"(\Flagged $MailFlagBit0 $MailFlagBit1 $MailFlagBit2)"),
            ("60692", "+FLAGS.SILENT", r"(\Flagged $MailFlagBit0)"),
        ])

    def test_none_only_clears(self) -> None:
        imap = FlagImap()
        self.adapter(imap).set_flag_colour(MailReference("INBOX", "777", "60692"), "none")
        self.assertEqual(self.stores(imap), [
            ("60692", "-FLAGS.SILENT",
             r"(\Flagged $MailFlagBit0 $MailFlagBit1 $MailFlagBit2)"),
        ])

    def test_red_is_never_set(self) -> None:
        imap = FlagImap()
        with self.assertRaises(MailAccessError):
            self.adapter(imap).set_flag_colour(MailReference("INBOX", "777", "1"), "red")
        self.assertEqual(self.stores(imap), [])

    def test_orange_messages_are_found_by_exact_colour(self) -> None:
        imap = FlagImap()
        found = self.adapter(imap).flagged_messages("FireFli/Processed", "orange")
        self.assertEqual(found, (
            MailReference("FireFli/Processed", "777", "41"),
            MailReference("FireFli/Processed", "777", "42"),
        ))
        search = [item for item in imap.commands if item[:2] == ("UID", "SEARCH")][0]
        self.assertEqual(search[3:], (
            "FLAGGED", "KEYWORD", "$MailFlagBit0",
            "UNKEYWORD", "$MailFlagBit1", "UNKEYWORD", "$MailFlagBit2",
        ))
        self.assertIn(("SELECT", '"FireFli/Processed"', True), imap.commands)


if __name__ == "__main__":
    unittest.main()
