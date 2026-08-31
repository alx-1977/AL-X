"""A specialist answers one bounded question and stops.

Routing "read the fields off this invoice" through the Core cost roughly ten
times what the question was worth, because every Core call carries AL/X's laws,
identity, capability catalogue, goal state and conversation. A specialist gets
the document and a schema, and has no authority to do anything with the answer.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    ModelCompletion,
    ModelRole,
    SpecialistError,
    SpecialistQuestion,
)
from alx.specialists import (  # noqa: E402
    INSTRUCTION,
    ModelSpecialist,
    checked_invoice,
    extract_invoice,
    invoice_question,
    is_supplier_bill,
    prior_coding,
    resolve_supplier,
)


INVOICE_TEXT = """SAMTEC INC
Invoice 18300777
Date 2026-08-20   Due 2026-09-20
Electronic components
Subtotal USD 180.00
Tax USD 0.00
Total USD 180.00"""


def answer(**overrides) -> dict:
    values = {
        "document_type": "supplier_invoice",
        "supplier_name": "SAMTEC",
        "invoice_number": "18300777",
        "invoice_date": "2026-08-20",
        "due_date": "2026-09-20",
        "currency": "USD",
        "subtotal": "180.00",
        "tax_amount": "0.00",
        "total": "180.00",
        "description": "Electronic components",
    }
    values.update(overrides)
    return values


class FakeModel:
    def __init__(self, output=None, error: Exception | None = None) -> None:
        self.requests = []
        self._output = output if output is not None else answer()
        self._error = error

    def complete(self, request):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ModelCompletion("fake", "fake-model", self._output, {})


class IsolationTests(unittest.TestCase):
    """The specialist must not carry AL/X's world or authority."""

    def setUp(self) -> None:
        self.model = FakeModel()
        extract_invoice(ModelSpecialist(self.model), INVOICE_TEXT, "Invoice 18300777")
        self.request = self.model.requests[0]
        self.sent = "".join(item.content for item in self.request.messages)

    def test_it_sends_only_the_instruction_and_the_document(self) -> None:
        self.assertEqual(len(self.request.messages), 2)
        self.assertEqual(self.request.messages[0].role, ModelRole.SYSTEM)
        self.assertEqual(self.request.messages[0].content, INSTRUCTION)
        self.assertEqual(self.request.messages[1].role, ModelRole.USER)
        self.assertIn("SAMTEC", self.request.messages[1].content)

    def test_alx_laws_and_identity_never_reach_it(self) -> None:
        laws = (REPOSITORY_ROOT / "LAWS_OF_ALX.md").read_text()
        identity = (REPOSITORY_ROOT / "IDENTITY_AND_MEMORY.md").read_text()
        for marker in (
            "Law 1 — AL/X decides meaning",
            "Law 2 — Code executes known procedures",
            "Origin 01",
            "Never diminish the person",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, laws + identity)
                self.assertNotIn(marker, self.sent)

    def test_the_capability_catalogue_never_reaches_it(self) -> None:
        """It cannot call a capability, so it is never told any exist."""
        for capability_id in (
            "execute_xero_bill",
            "search_mail_messages",
            "send_mail_reply",
            "delete_xero_draft_bill",
        ):
            with self.subTest(capability_id=capability_id):
                self.assertNotIn(capability_id, self.sent)

    def test_no_goal_conversation_or_memory_reaches_it(self) -> None:
        for marker in (
            "active_goal",
            "conversation",
            "retrieved_memories",
            "outstanding_work",
            "approvals",
            "capabilities",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(f'"{marker}"', self.sent)

    def test_the_protocol_instructions_never_reach_it(self) -> None:
        """Those grant a decision contract; a specialist decides nothing."""
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        self.assertNotIn("single authoritative AL/X reasoning Core", self.sent)
        self.assertNotIn(PROTOCOL_INSTRUCTIONS[:200], self.sent)

    def test_its_instruction_grants_no_action_or_continuation(self) -> None:
        for forbidden in ("capability", "call ", "approve", "authorise", "next step"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, INSTRUCTION.lower())

    def test_material_is_bounded(self) -> None:
        """An unbounded prompt reintroduces the cost this exists to avoid."""
        question = SpecialistQuestion(
            "q", "instruction", "x" * 50_000, {"type": "object"}
        )
        self.assertEqual(len(question.bounded_material), question.material_limit)

    def test_the_whole_prompt_stays_small(self) -> None:
        self.assertLess(len(self.sent) // 4, 2_000)


class SecondReasoningPathTests(unittest.TestCase):
    """Structural proof that the specialist cannot become a second AL/X."""

    def test_it_returns_data_and_cannot_continue(self) -> None:
        """One question, one answer. No loop exists to continue in."""
        model = FakeModel()
        result = extract_invoice(ModelSpecialist(model), INVOICE_TEXT)
        self.assertEqual(len(model.requests), 1)
        self.assertIsInstance(result, dict)
        self.assertNotIn("action", result)
        self.assertNotIn("capability_id", result)

    def test_it_holds_no_dispatch_store_or_authority(self) -> None:
        specialist = ModelSpecialist(FakeModel())
        for attribute in (
            "dispatch",
            "_dispatch",
            "_store",
            "_memory_store",
            "_capabilities",
            "decide",
            "process",
        ):
            with self.subTest(attribute=attribute):
                self.assertFalse(hasattr(specialist, attribute))

    def test_the_module_imports_no_core_goal_or_capability_code(self) -> None:
        for name in ("runner.py", "invoice.py", "coding.py"):
            source = (REPOSITORY_ROOT / "src/alx/specialists" / name).read_text()
            with self.subTest(name=name):
                for forbidden in (
                    "from alx.core",
                    "from alx.goals",
                    "from alx.capabilities",
                    "from alx.memories",
                    "from alx.safety",
                    "from alx.tools",
                ):
                    self.assertNotIn(forbidden, source)

    def test_the_architecture_gate_confines_the_boundary(self) -> None:
        rules = (REPOSITORY_ROOT / "architecture/boundaries.toml").read_text()
        self.assertIn('specialists = ["contracts"]', rules)

    def test_a_failure_carries_no_document_content(self) -> None:
        model = FakeModel(error=ValueError(f"failed reading {INVOICE_TEXT}"))
        with self.assertRaises(SpecialistError) as captured:
            extract_invoice(ModelSpecialist(model), INVOICE_TEXT)
        self.assertNotIn("SAMTEC", str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)


class TelemetrySeparationTests(unittest.TestCase):
    """Specialist calls are counted apart from Core reasoning calls."""

    def test_it_records_under_its_own_task_not_the_conversation(self) -> None:
        model = FakeModel()
        extract_invoice(ModelSpecialist(model), INVOICE_TEXT)
        request = model.requests[0]
        self.assertEqual(request.affinity_key, "extract_supplier_invoice")
        self.assertNotIn("conversation", request.affinity_key)

    def test_it_never_counts_against_the_bill_reasoning_ceiling(self) -> None:
        """A specialist call must not consume the Core's four-call budget."""
        import tempfile

        from alx.observability import XERO_BILL_BUDGET, SQLiteUsageRecorder

        with tempfile.TemporaryDirectory() as directory:
            usage = SQLiteUsageRecorder(Path(directory) / "usage.sqlite3")
            usage.set_budget("conversation-1", XERO_BILL_BUDGET)
            call = {"code": "reasoning.completed", "model": "m"}
            for _ in range(10):
                usage.record("extract_supplier_invoice", call)
            self.assertEqual(usage.task("conversation-1")["calls"], 0)
            usage.check("conversation-1")


class ExtractionTests(unittest.TestCase):
    def test_a_clean_invoice_verifies(self) -> None:
        result = extract_invoice(ModelSpecialist(FakeModel()), INVOICE_TEXT)
        self.assertTrue(result["verified"])
        self.assertEqual(result["invoice_number"], "18300777")
        self.assertEqual(result["total"], "180.00")
        self.assertEqual(result["problems"], ())

    def test_arithmetic_that_does_not_add_up_is_reported_not_repaired(self) -> None:
        """A misread number can look plausible; guessing which one is wrong is not mechanical."""
        result = checked_invoice(answer(subtotal="180.00", tax_amount="27.00", total="180.00"))
        self.assertFalse(result["verified"])
        self.assertTrue(any("does not equal total" in item for item in result["problems"]))
        self.assertEqual(result["total"], "180.00", "the figures must not be altered")

    def test_missing_identity_is_reported(self) -> None:
        result = checked_invoice(answer(invoice_number="", supplier_name=""))
        self.assertFalse(result["verified"])
        self.assertIn("invoice number missing", result["problems"])
        self.assertIn("supplier name missing", result["problems"])

    def test_an_unreadable_total_is_never_guessed(self) -> None:
        result = checked_invoice(answer(total="see attached"))
        self.assertFalse(result["verified"])
        self.assertEqual(result["total"], "")

    def test_a_document_with_no_text_fails_before_any_model_call(self) -> None:
        model = FakeModel()
        with self.assertRaises(SpecialistError) as captured:
            extract_invoice(ModelSpecialist(model), "   ")
        self.assertEqual(captured.exception.code, "document_has_no_text")
        self.assertEqual(model.requests, [])

    def test_the_context_line_is_offered_for_the_invoice_number(self) -> None:
        question = invoice_question(INVOICE_TEXT, "Invoice 18300777.pdf")
        self.assertIn("Invoice 18300777.pdf", question.bounded_material)


class DocumentClassificationTests(unittest.TestCase):
    """A real invoice was refused for describing itself in different words.

    AL/X reported "automatic capture did not recognise the PDF as an invoice"
    for a document whose text extracted cleanly and whose totals she then read
    out correctly. The check matched two exact strings.
    """

    def test_an_invoice_is_recognised_however_it_is_worded(self) -> None:
        for value in (
            "supplier_invoice",
            "supplier invoice",
            "invoice",
            "Invoice",
            "INVOICE",
            "tax invoice",
            "tax_invoice",
            "supplier_bill",
            "bill",
            "vendor invoice",
        ):
            with self.subTest(document_type=value):
                self.assertTrue(is_supplier_bill(value))

    def test_another_accounting_document_never_becomes_a_bill(self) -> None:
        for value in (
            "credit note",
            "credit_note",
            "quote",
            "quotation",
            "purchase order",
            "statement",
            "remittance advice",
            "delivery note",
            "receipt",
            "proforma invoice",
            "pro forma invoice",
        ):
            with self.subTest(document_type=value):
                self.assertFalse(is_supplier_bill(value))

    def test_an_unknown_or_missing_type_is_refused(self) -> None:
        for value in ("", "   ", "letter", "newsletter", "photograph"):
            with self.subTest(document_type=value):
                self.assertFalse(is_supplier_bill(value))


class PriorCodingTests(unittest.TestCase):
    """Settled history is a fact; disagreement is judgment."""

    @staticmethod
    def bill(code: str, tax: str = "NONE", kind: str = "NoTax") -> dict:
        return {
            "LineAmountTypes": kind,
            "LineItems": [{"AccountCode": code, "TaxType": tax}],
        }

    def test_consistent_history_resolves_without_a_model(self) -> None:
        result = prior_coding([self.bill("310"), self.bill("310")])
        self.assertTrue(result["resolved"])
        self.assertEqual(result["account_code"], "310")
        self.assertEqual(result["tax_type"], "NONE")

    def test_no_history_returns_to_alx(self) -> None:
        result = prior_coding([])
        self.assertFalse(result["resolved"])
        self.assertIn("no earlier bill", result["reason"])

    def test_conflicting_history_returns_to_alx(self) -> None:
        result = prior_coding([self.bill("310"), self.bill("429", "INPUT3")])
        self.assertFalse(result["resolved"])
        self.assertIn("disagree", result["reason"])
        self.assertEqual(result["account_code"], "")

    def test_a_differing_tax_type_alone_is_still_a_disagreement(self) -> None:
        result = prior_coding([self.bill("310", "NONE"), self.bill("310", "INPUT3")])
        self.assertFalse(result["resolved"])


class DefaultCodingTests(unittest.TestCase):
    """A supplier whose work varies has no single answer to derive.

    One real supplier had 18 bills across ten account/tax combinations:
    consulting, capital equipment and office equipment, all legitimate. Asking
    Friedl to choose on every invoice is unacceptable, and letting a model pick
    an account presents a guess as knowledge. The document settles the tax; the
    account falls back to a configured default.
    """

    @staticmethod
    def bill(code: str, tax: str = "NONE", kind: str = "NoTax") -> dict:
        return {
            "LineAmountTypes": kind,
            "LineItems": [{"AccountCode": code, "TaxType": tax}],
        }

    def test_settled_history_still_wins_over_the_default(self) -> None:
        result = prior_coding(
            [self.bill("412"), self.bill("412")], "310", False, "INPUT3"
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["account_code"], "412")
        self.assertFalse(result["from_default"])

    def test_conflicting_history_uses_the_default_account(self) -> None:
        result = prior_coding(
            [self.bill("412"), self.bill("700", "CAPEXINPUT2", "Inclusive")],
            "310",
            False,
            "INPUT3",
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["account_code"], "310")
        self.assertTrue(result["from_default"])
        self.assertIn("disagree", result["reason"])

    def test_a_new_supplier_uses_the_default_account(self) -> None:
        result = prior_coding([], "310", False, "INPUT3")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["account_code"], "310")

    def test_tax_follows_the_document_not_the_account_default(self) -> None:
        """A supplier who is not VAT registered cannot be claimed for."""
        without = prior_coding([], "310", False, "INPUT3")
        self.assertEqual(without["tax_type"], "NONE")
        self.assertEqual(without["line_amount_types"], "NoTax")

        with_vat = prior_coding([], "310", True, "INPUT3")
        self.assertEqual(with_vat["tax_type"], "INPUT3")
        self.assertEqual(with_vat["line_amount_types"], "Exclusive")

    def test_without_a_configured_default_it_still_asks(self) -> None:
        """The default is a deployment choice, never assumed."""
        result = prior_coding(
            [self.bill("412"), self.bill("700", "CAPEXINPUT2")], "", False, ""
        )
        self.assertFalse(result["resolved"])
        self.assertFalse(result["from_default"])


class SupplierResolutionTests(unittest.TestCase):
    @staticmethod
    def contact(name: str, identifier: str) -> dict:
        return {"Name": name, "ContactID": identifier, "ContactStatus": "ACTIVE"}

    def test_one_exact_match_resolves(self) -> None:
        result = resolve_supplier([self.contact("SAMTEC", "c-1")], "SAMTEC")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["contact_id"], "c-1")

    def test_two_similar_contacts_return_to_alx(self) -> None:
        """The shuttle case: two near-identical suppliers is a judgment call."""
        result = resolve_supplier(
            [
                self.contact("Cape Town Shuttles and Tour", "c-1"),
                self.contact("Cape Shuttle's and Tours", "c-2"),
            ],
            "Cape Town Shuttle",
        )
        self.assertFalse(result["resolved"])
        self.assertIn("no contact is named", result["reason"])

    def test_an_exact_name_wins_over_a_partial_list(self) -> None:
        result = resolve_supplier(
            [self.contact("SAMTEC", "c-1"), self.contact("SAMTEC EUROPE", "c-2")],
            "SAMTEC",
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["contact_id"], "c-1")

    def test_no_match_returns_to_alx(self) -> None:
        result = resolve_supplier([], "SAMTEC")
        self.assertFalse(result["resolved"])

    def test_a_different_company_is_never_accepted(self) -> None:
        """Review finding: one unrelated result was declared unambiguous.

        Falling back from an exact match to whatever the search returned
        resolved "Expected Supplier" to "Completely Different Company". With
        unattended writes that posts a bill against the wrong company.
        """
        result = resolve_supplier(
            [
                {
                    "Name": "Completely Different Company",
                    "ContactID": "wrong",
                    "ContactStatus": "ACTIVE",
                }
            ],
            "Expected Supplier",
        )
        self.assertFalse(result["resolved"])
        self.assertEqual(result["contact_id"], "")
        self.assertIn("Completely Different Company", result["reason"])

    def test_a_near_miss_is_not_a_match(self) -> None:
        for stored, wanted in (
            ("SAMTEC EUROPE", "SAMTEC"),
            ("SAMTEC", "SAMTEC INC"),
            ("Cape Shuttle's and Tours", "Cape Town Shuttles and Tour"),
        ):
            with self.subTest(stored=stored, wanted=wanted):
                result = resolve_supplier(
                    [{"Name": stored, "ContactID": "c", "ContactStatus": "ACTIVE"}],
                    wanted,
                )
                self.assertFalse(result["resolved"])


if __name__ == "__main__":
    unittest.main()
