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

    def test_an_attention_action_of_a_changing_capability_is_attention_state(self) -> None:
        # Mail's acknowledge releases a notification and changes no mail (D-074, 2026-11-09).
        definition = CapabilityDefinition("mail", "Mail.", OBJECT, OBJECT, SideEffect.EFFECTFUL,
                                          read_only_when=((("action", "read"),),),
                                          attention_only_when=((("action", "acknowledge"),),))
        self.assertIs(definition.effect_for({"action": "read"}), SideEffect.NONE)
        self.assertIs(definition.effect_for({"action": "acknowledge"}), SideEffect.ATTENTION_STATE)
        self.assertIs(definition.effect_for({"action": "trash"}), SideEffect.EFFECTFUL)
        # Only an effectful capability is narrowed; a read stays a read.
        reading = CapabilityDefinition("r", "Reads.", OBJECT, OBJECT, SideEffect.NONE,
                                       attention_only_when=((("action", "x"),),))
        self.assertIs(reading.effect_for({"action": "x"}), SideEffect.NONE)


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

    def test_teardrops_are_counted_not_listed(self) -> None:
        # 2026-11-10: 908 teardrop zones on P0009 buried its 47 real zones.
        # Runs the real script on a board with two pours and two teardrops.
        from alx.config.settings import KiCadSettings
        from alx.providers.coding_process import kicad_containment_available, run_kicad_python
        python = KiCadSettings.__dataclass_fields__["kicad_python"].default
        if not python.exists() or not kicad_containment_available():
            self.skipTest("KiCad is not installed here")
        board = Path(self.directory.name) / "td" / "board.kicad_pcb"
        board.parent.mkdir()
        board.write_text((REPOSITORY_ROOT / "tests/fixtures/teardrops.kicad_pcb").read_text())
        for limit, listed in ((50, ["3V3 pour", "GND pour"]), (1, ["GND pour"])):
            with self.subTest(limit=limit):
                out = read_overview(board, {"limit": limit}, scratch_root=self.scratch,
                                    kicad_python=python, execute_python=run_kicad_python)
                self.assertEqual(out["board"]["teardrops"], 2)
                self.assertEqual(sorted(zone["name"] for zone in out["zones"]), listed)
                self.assertEqual(out["truncated"]["zones"], limit == 1)


class TurnBoundByActionTests(unittest.TestCase):
    """Review of c19c8c4: one free kicad call spent the turn's instruction."""

    def core(self):
        from alx.core.loop import CoreAgent
        core = CoreAgent.__new__(CoreAgent)
        core._turn_bound_capabilities = frozenset({"kicad", "send_mail_reply"})
        core._approval_exempt_when = {"kicad": ((("action", "inspect"),), (("action", "check"),))}
        return core

    def test_free_actions_are_not_bound_to_the_turn(self) -> None:
        core = self.core()
        self.assertFalse(core._call_turn_bound(CapabilityCall("c", "kicad", {"action": "inspect"})))
        self.assertFalse(core._call_turn_bound(CapabilityCall("c", "kicad", {"action": "check"})))
        self.assertTrue(core._call_turn_bound(CapabilityCall("c", "kicad", {"action": "write_back"})))
        self.assertTrue(core._call_turn_bound(CapabilityCall("c", "send_mail_reply", {})))
        self.assertFalse(core._call_turn_bound(CapabilityCall("c", "mail", {"action": "read"})))

    def test_the_runtime_passes_each_capabilitys_free_actions(self) -> None:
        source = (REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertIn("approval_exempt_when={", source)
        loop = (REPOSITORY_ROOT / "src/alx/core/loop.py").read_text()
        # Every turn-bound decision goes through the per-call test; the only
        # direct membership test left is inside it.
        self.assertEqual(loop.count(".capability_id in self._turn_bound_capabilities"), 1)
        self.assertEqual(loop.count("self._call_turn_bound("), 5)


class KiCadDescriptionAndPathTests(unittest.TestCase):
    def test_each_action_lists_its_fields_and_required_ones(self) -> None:
        from alx.tools.kicad import DEFINITION
        self.assertIn("[check] fields: project_dir; required: project_dir.", DEFINITION.purpose)
        self.assertIn("[write_back] fields: work_copy_id, work_copy_revision, project_dir; required:",
                      DEFINITION.purpose)

    def test_a_linked_board_outside_the_design_folders_is_refused(self) -> None:
        from alx.providers.kicad import KiCadError, KiCadProvider
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.kicad_pcb"
            outside.write_text("(kicad_pcb)")
            project = root / "designs" / "P1"
            project.mkdir(parents=True)
            (project / "board.kicad_pcb").symlink_to(outside)
            settings = SimpleNamespace(design_roots=(root / "designs",), work_root=root / "work",
                                       kicad_python=Path("python"))
            provider = KiCadProvider(settings, None)
            with self.assertRaises(KiCadError) as raised:
                provider._board_for({"project_dir": str(project)})
            self.assertEqual(str(raised.exception), "path_denied")


class WorkerTests(unittest.TestCase):
    def test_the_worker_has_the_one_kicad_capability(self) -> None:
        # It works without approvals, so export and write_back stay out of its reach.
        self.assertIn("kicad", WORKER_CAPABILITIES)
        self.assertNotIn("write_back_kicad_board", WORKER_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
