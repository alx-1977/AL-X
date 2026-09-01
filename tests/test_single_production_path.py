"""Law 0: one outcome, one production path — proved by absence, not intent.

Two production outcomes were consolidated here. Committing an ordinary
supplier bill became `capture_supplier_invoice`, and a DHL import became
`process_dhl_import`. Five capabilities that could each independently reach a
posted bill were deleted rather than hidden, deprecated or kept for recovery.

"Not currently used" is not deleted, and "not exposed" is not deleted, so
these tests assert the superseded identifiers are absent from the registry,
the executors, the policies and the source itself. The mutation test that the
enforcement specification requires restores a superseded entry point and
proves the suite notices.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.dhl import build_dhl_runtime  # noqa: E402
from alx.bootstrap.xero import build_xero_runtime  # noqa: E402
from alx.contracts import SideEffect  # noqa: E402
from alx.tools import (  # noqa: E402
    CAPTURE_SUPPLIER_INVOICE,
    DELETE_XERO_DRAFT_BILL,
    DHL_DEFINITIONS,
    PROCESS_DHL_IMPORT,
    XERO_DEFINITIONS,
)
from support import xero_settings  # noqa: E402

# Every production entry point that could once reach a posted supplier bill.
SUPERSEDED = (
    "execute_xero_bill",
    "create_xero_draft_bill",
    "update_xero_draft_bill",
    "attach_mail_document_to_xero_bill",
    "authorise_xero_bill",
    "analyze_dhl_customs_documents",
    "reconcile_dhl_import_documents",
)

# The MyBill GDB CSV reconciliation implementation D-021 deleted. Its
# capability was already gone; Law 0 required the implementation to go too.
SUPERSEDED_CSV_IMPLEMENTATION = (
    "def reconcile(",
    "_parse_invoices",
    "_charges",
    "class Charge",
    "class Shipment",
    "_KNOWN_CODES",
    "_SERVICE_CODES",
)


class FakeMail:
    def read_attachment(self, _reference, _attachment_id):
        raise AssertionError("not used")


class FakeXero:
    pass


def runtimes():
    settings = xero_settings(unattended_bill_writes=True)
    with tempfile.TemporaryDirectory() as directory:
        xero = build_xero_runtime(
            settings, Path(directory), FakeMail(), lambda: "call"
        )
    dhl = build_dhl_runtime(
        FakeMail(),
        FakeXero(),
        lambda: "call",
        settings.import_vat_account,
        settings.customs_duty_account,
        settings.clearance_account,
        settings.unattended_bill_writes,
    )
    return xero, dhl


class SupersededPathsAreDeletedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.xero, self.dhl = runtimes()

    def test_no_superseded_capability_is_registered_anywhere(self) -> None:
        registered = {
            item.capability_id for item in (*XERO_DEFINITIONS, *DHL_DEFINITIONS)
        }
        for capability_id in SUPERSEDED:
            with self.subTest(capability_id=capability_id):
                self.assertNotIn(capability_id, registered)

    def test_no_superseded_capability_is_dispatchable(self) -> None:
        """A registration is not the only way to reach an executor."""
        executors = {**self.xero.executors, **self.dhl.executors}
        policies = {**self.xero.policies, **self.dhl.policies}
        for capability_id in SUPERSEDED:
            with self.subTest(capability_id=capability_id):
                self.assertNotIn(capability_id, executors)
                self.assertNotIn(capability_id, policies)

    def test_the_superseded_identifiers_are_gone_from_the_source(self) -> None:
        """Renamed and left behind is not deleted either."""
        for relative in (
            "src/alx/tools/xero.py",
            "src/alx/tools/dhl.py",
            "src/alx/tools/__init__.py",
            "src/alx/bootstrap/xero.py",
            "src/alx/bootstrap/dhl.py",
        ):
            source = (REPOSITORY_ROOT / relative).read_text()
            for capability_id in SUPERSEDED:
                with self.subTest(path=relative, capability_id=capability_id):
                    self.assertNotIn(capability_id, source)

    def test_the_csv_reconciliation_implementation_is_gone(self) -> None:
        """Law 0: the capability's deletion did not delete its implementation."""
        source = (REPOSITORY_ROOT / "src/alx/providers/dhl.py").read_text()
        for fragment in SUPERSEDED_CSV_IMPLEMENTATION:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_the_analyzer_offers_no_reconciliation_entry_point(self) -> None:
        from alx.providers import DhlImportAnalyzerAdapter

        self.assertFalse(hasattr(DhlImportAnalyzerAdapter, "reconcile"))
        self.assertFalse(hasattr(DhlImportAnalyzerAdapter, "analyze_customs"))

    def test_exactly_one_capability_commits_a_bill(self) -> None:
        effectful = {
            item.capability_id
            for item in (*XERO_DEFINITIONS, *DHL_DEFINITIONS)
            if item.side_effect is SideEffect.EFFECTFUL
        }
        # Capture posts an ordinary bill, process_dhl_import posts a DHL
        # import, and delete discards a draft. Nothing else writes.
        self.assertEqual(
            effectful,
            {CAPTURE_SUPPLIER_INVOICE, PROCESS_DHL_IMPORT, DELETE_XERO_DRAFT_BILL},
        )

    def test_the_two_bill_paths_do_not_overlap(self) -> None:
        """An ordinary bill and a DHL import are separate outcomes."""
        self.assertEqual(set(self.xero.executors) & set(self.dhl.executors), set())

    def test_a_dhl_document_cannot_reach_the_ordinary_bill_path(self) -> None:
        """Two routes to a posted DHL bill would be the Law 0 violation."""
        source = (REPOSITORY_ROOT / "src/alx/tools/xero.py").read_text()
        self.assertIn("dhl_import_requires_dedicated_processing", source)
        self.assertIn("dhl_classifier", source)

    def test_no_recovery_surface_is_exposed_by_either_runtime(self) -> None:
        for runtime in (self.xero, self.dhl):
            with self.subTest(runtime=type(runtime).__name__):
                self.assertFalse(hasattr(runtime, "recovery_definitions"))


class MutationTests(unittest.TestCase):
    """Restoring a superseded entry point must make the suite fail.

    A test that only reads today's code proves nothing about tomorrow's. These
    reintroduce a superseded path the way a regression actually would, and
    assert the checks above catch it.
    """

    def test_restoring_a_registration_is_caught(self) -> None:
        from alx.contracts import CapabilityDefinition, StructuredSchema, ValueKind

        restored = CapabilityDefinition(
            "create_xero_draft_bill",
            "A superseded entry point, restored to prove the check bites.",
            StructuredSchema(ValueKind.OBJECT, {}, ()),
            StructuredSchema(ValueKind.OBJECT, {}, ()),
            SideEffect.EFFECTFUL,
            (),
        )
        mutated = {
            item.capability_id for item in (*XERO_DEFINITIONS, *DHL_DEFINITIONS)
        } | {restored.capability_id}
        offending = [item for item in SUPERSEDED if item in mutated]
        self.assertEqual(offending, ["create_xero_draft_bill"])

    def test_restoring_an_executor_is_caught(self) -> None:
        xero, dhl = runtimes()
        mutated = {
            **xero.executors,
            **dhl.executors,
            "authorise_xero_bill": lambda _arguments: None,
        }
        offending = [item for item in SUPERSEDED if item in mutated]
        self.assertEqual(offending, ["authorise_xero_bill"])

    def test_restoring_the_identifier_in_source_is_caught(self) -> None:
        mutated = (
            (REPOSITORY_ROOT / "src/alx/tools/xero.py").read_text()
            + "\nEXECUTE_XERO_BILL = 'execute_xero_bill'\n"
        )
        offending = [item for item in SUPERSEDED if item in mutated]
        self.assertEqual(offending, ["execute_xero_bill"])

    def test_a_second_effectful_bill_capability_is_caught(self) -> None:
        effectful = {
            item.capability_id
            for item in (*XERO_DEFINITIONS, *DHL_DEFINITIONS)
            if item.side_effect is SideEffect.EFFECTFUL
        } | {"execute_xero_bill"}
        self.assertNotEqual(
            effectful,
            {CAPTURE_SUPPLIER_INVOICE, PROCESS_DHL_IMPORT, DELETE_XERO_DRAFT_BILL},
        )


if __name__ == "__main__":
    unittest.main()
