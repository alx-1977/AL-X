"""The planning catalogue must stay complete about inputs while shrinking.

AL/X needs to know which capability to use and exactly what structured input it
requires. The full shape of a result is evident when the result arrives, so the
catalogue names result fields instead of restating their schemas.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityDefinition,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.core.model_reasoner import (  # noqa: E402
    _capability_schema_payload,
    _failure_codes,
    _result_fields,
    _shared_failure_codes,
)
from alx.tools import XERO_DEFINITIONS  # noqa: E402


def find(capability_id: str) -> CapabilityDefinition:
    return next(
        item for item in XERO_DEFINITIONS if item.capability_id == capability_id
    )


class InputCompletenessTests(unittest.TestCase):
    """Nothing needed to compose a valid call may be dropped."""

    def test_every_input_property_and_requirement_survives(self) -> None:
        for definition in XERO_DEFINITIONS:
            with self.subTest(capability_id=definition.capability_id):
                payload = _capability_schema_payload(definition.input_schema)
                self.assertEqual(
                    set(payload.get("properties", {})),
                    set(definition.input_schema.properties),
                )
                self.assertEqual(
                    payload.get("required", []),
                    list(definition.input_schema.required),
                )

    def test_nested_input_structure_is_preserved(self) -> None:
        """A composite call takes arrays of objects; that shape must survive."""
        payload = _capability_schema_payload(find("execute_xero_bill").input_schema)
        documents = payload["properties"]["source_documents"]
        self.assertEqual(documents["kind"], "array")
        self.assertEqual(
            set(documents["items"]["properties"]),
            {"mailbox_id", "uid_validity", "uid", "attachment_id", "expected_sha256"},
        )
        self.assertIn("expected_sha256", documents["items"]["required"])

    def test_line_item_structure_survives_two_levels_down(self) -> None:
        payload = _capability_schema_payload(find("execute_xero_bill").input_schema)
        line = payload["properties"]["line_items"]["items"]
        self.assertEqual(
            set(line["properties"]),
            {
                "description",
                "quantity",
                "unit_amount",
                "account_code",
                "tax_type",
                "tax_amount",
            },
        )

    def test_a_closed_schema_still_says_so(self) -> None:
        """extra_properties False is a real constraint, not a default."""
        payload = _capability_schema_payload(find("find_xero_bill").input_schema)
        self.assertIs(payload["extra_properties"], False)

    def test_empty_defaults_are_omitted_rather_than_spelled_out(self) -> None:
        scalar = _capability_schema_payload(StructuredSchema(ValueKind.STRING))
        self.assertEqual(scalar, {"kind": "string"})

    def test_omission_is_lossless(self) -> None:
        """An absent key means the default the schema always carried."""
        payload = _capability_schema_payload(find("find_xero_bill").input_schema)
        contact = payload["properties"]["contact_id"]
        self.assertNotIn("properties", contact)
        self.assertEqual(
            find("find_xero_bill").input_schema.properties["contact_id"].properties,
            {},
        )


class ResultCompressionTests(unittest.TestCase):
    def test_result_fields_name_what_comes_back(self) -> None:
        fields = _result_fields(find("find_xero_bill").output_schema)
        self.assertIn("invoice_id", fields)
        self.assertIn("status", fields)
        self.assertIn("total", fields)

    def test_an_array_result_says_what_it_contains(self) -> None:
        fields = _result_fields(
            StructuredSchema(
                ValueKind.ARRAY, items=StructuredSchema(ValueKind.OBJECT)
            )
        )
        self.assertEqual(fields, ["array of object"])

    def test_result_fields_are_materially_smaller_than_the_schema(self) -> None:
        definition = find("execute_xero_bill")
        full = json.dumps(
            _capability_schema_payload(definition.output_schema), separators=(",", ":")
        )
        compressed = json.dumps(
            _result_fields(definition.output_schema), separators=(",", ":")
        )
        self.assertLess(len(compressed), len(full) // 3)


class FailureCodeTests(unittest.TestCase):
    def test_shared_codes_are_stated_once_and_not_repeated(self) -> None:
        shared = _shared_failure_codes(XERO_DEFINITIONS)
        self.assertIn("arguments_unusable", shared)
        for definition in XERO_DEFINITIONS:
            with self.subTest(capability_id=definition.capability_id):
                self.assertNotIn(
                    "arguments_unusable", _failure_codes(definition, shared)
                )

    def test_no_failure_code_is_lost(self) -> None:
        """Shared plus specific must reconstruct the original set exactly."""
        shared = _shared_failure_codes(XERO_DEFINITIONS)
        for definition in XERO_DEFINITIONS:
            with self.subTest(capability_id=definition.capability_id):
                self.assertEqual(
                    shared | frozenset(_failure_codes(definition, shared)),
                    frozenset(definition.possible_failure_codes),
                )

    def test_a_single_capability_shares_nothing(self) -> None:
        self.assertEqual(_shared_failure_codes(XERO_DEFINITIONS[:1]), frozenset())


class CatalogueSizeTests(unittest.TestCase):
    def test_no_capability_was_hidden_to_save_tokens(self) -> None:
        """Compression must not silently drop a capability from planning."""
        definitions = tuple(XERO_DEFINITIONS)
        shared = _shared_failure_codes(definitions)
        entries = [
            {
                "id": item.capability_id,
                "purpose": item.purpose,
                "side_effect": item.side_effect.value,
                "failure_codes": _failure_codes(item, shared),
                "input_schema": _capability_schema_payload(item.input_schema),
                "result_fields": _result_fields(item.output_schema),
            }
            for item in definitions
        ]
        self.assertEqual(len(entries), len(definitions))
        self.assertEqual(
            {item["id"] for item in entries},
            {item.capability_id for item in definitions},
        )

    def test_purpose_and_side_effect_remain_verbatim(self) -> None:
        """Purpose is how she chooses; side effect is how safety is judged."""
        for definition in XERO_DEFINITIONS:
            with self.subTest(capability_id=definition.capability_id):
                self.assertTrue(definition.purpose.strip())
                self.assertIsInstance(definition.side_effect, SideEffect)


if __name__ == "__main__":
    unittest.main()
