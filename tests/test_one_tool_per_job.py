"""One capability per job, with its actions treated by exact argument (D-074).

2026-11-08: AL/X reviewed the cellular antenna when asked about the WiFi one.
Nothing in her tools let her look at a board before judging it; a separate
tool per KiCad job also spread one subject across seven entries. The gate can
now exempt several actions of one capability from approval, and a capability
can name the actions that only read.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import CapabilityCall, CapabilityDefinition, SideEffect, StructuredSchema, ValueKind  # noqa: E402
from alx.contracts.capabilities import arguments_match  # noqa: E402
from alx.providers.kicad_section import INSPECT_SCRIPT, SectionError, read_overview  # noqa: E402
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402
from alx.tools.worker import WORKER_CAPABILITIES  # noqa: E402

OBJECT = StructuredSchema(ValueKind.OBJECT)


class ArgumentAlternativesTests(unittest.TestCase):
    def test_any_one_alternative_whose_pairs_all_match(self) -> None:
        alternatives = ((("action", "inspect"),), (("action", "api"), ("method", "GET")))
        self.assertTrue(arguments_match(alternatives, {"action": "inspect", "x": 1}))
        self.assertTrue(arguments_match(alternatives, {"action": "api", "method": "GET"}))
        self.assertFalse(arguments_match(alternatives, {"action": "api", "method": "POST"}))
        self.assertFalse(arguments_match(alternatives, {"action": "write_back"}))
        self.assertFalse(arguments_match((), {"action": "inspect"}))
        self.assertFalse(arguments_match(((),), {"action": "inspect"}))  # an empty alternative exempts nothing

    def test_the_gate_asks_approval_only_for_the_actions_that_need_it(self) -> None:
        policy = AuthorityPolicy(frozenset({"design.kicad"}), approval_required=True,
                                 approval_exempt_when=((("action", "inspect"),), (("action", "edit"),)))
        gate = SafetyGate({"kicad": policy})
        context = AuthorityContext("friedl", frozenset({"design.kicad"}), datetime.now(UTC))
        free = gate.evaluate(CapabilityCall("c1", "kicad", {"action": "inspect"}), context)
        gated = gate.evaluate(CapabilityCall("c2", "kicad", {"action": "write_back"}), context)
        self.assertEqual(free.state.value, "allowed")
        self.assertEqual(gated.state.value, "approval_required")

    def test_reads_of_a_changing_capability_count_as_reads(self) -> None:
        definition = CapabilityDefinition("kicad", "Anything KiCad.", OBJECT, OBJECT, SideEffect.EFFECTFUL,
                                          read_only_when=((("action", "inspect"),),))
        self.assertIs(definition.effect_for({"action": "inspect"}), SideEffect.NONE)
        self.assertIs(definition.effect_for({"action": "write_back"}), SideEffect.EFFECTFUL)
        reading = CapabilityDefinition("r", "Reads.", OBJECT, OBJECT, SideEffect.NONE,
                                       read_only_when=((("action", "x"),),))
        self.assertIs(reading.effect_for({}), SideEffect.NONE)


class InspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.board = root / "p" / "board.kicad_pcb"
        self.board.parent.mkdir()
        self.board.write_text("(kicad_pcb)")
        self.scratch = root / "scratch"
        self.args = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_python(self, python, script, board):
        board = Path(board)
        self.args.append(json.loads((board.parent / "section-args.json").read_text()))
        (board.parent / "section.json").write_text(json.dumps({"parts": [], "nets": [], "zones": []}))
        return SimpleNamespace(returncode=0, stderr="")

    def test_the_board_is_read_from_a_copy_with_its_filters(self) -> None:
        read_overview(self.board, {"refs": ["AE1", "AE2"], "contains": "antenna", "limit": 50},
                      scratch_root=self.scratch, kicad_python=Path("python"),
                      execute_python=self.run_python)
        self.assertEqual(self.args, [{"refs": ["AE1", "AE2"], "nets": [], "contains": "antenna", "limit": 50}])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_bad_filters_are_refused_before_anything_runs(self) -> None:
        for bad in ({"refs": "AE1"}, {"limit": 0}, {"limit": True}, {"contains": 3}):
            with self.subTest(bad=bad), self.assertRaises(SectionError):
                read_overview(self.board, bad, scratch_root=self.scratch, kicad_python=Path("python"),
                              execute_python=self.run_python)
        self.assertEqual(self.args, [])

    def test_every_field_and_both_kinds_of_zone_are_read(self) -> None:
        # Part numbers and descriptions live in footprint fields; a keep-out is
        # a rule area. Layers are read by number.
        self.assertIn("GetFields()", INSPECT_SCRIPT)
        self.assertIn("GetIsRuleArea()", INSPECT_SCRIPT)
        self.assertIn("pcbnew.F_Cu", INSPECT_SCRIPT)


class WorkerTests(unittest.TestCase):
    def test_the_worker_has_the_one_kicad_capability(self) -> None:
        # It works without approvals, so export and write_back stay out of its reach.
        self.assertIn("kicad", WORKER_CAPABILITIES)
        self.assertNotIn("write_back_kicad_board", WORKER_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
