"""D-084a: checked scripts, on real KiCad.

Needs KiCad; skipped where it is not installed (CI).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from alx.bootstrap.kicad import build_kicad_runtime  # noqa: E402
from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.kicad import PresentationEditScope, presentation_edit_scope  # noqa: E402
from alx.providers.kicad_change import ChangeError, unmet, validate_postconditions  # noqa: E402
from kicad_actions import kicad_call  # noqa: E402
from test_kicad_schematic_helpers import (APP, CLI, DEVICE, P0005_FIXTURE, PYTHON,  # noqa: E402
                                          library_symbol, schematic)

APP_SYMBOLS = APP / "SharedSupport/symbols"

FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


class ChangeShapeTests(unittest.TestCase):
    def test_postconditions_are_validated_without_dispatch(self):
        for invalid in (None, {}, [{"op": "script"}], [{"op": "wire", "from": "U1", "to": "U2.1"}],
                        [{"op": "label", "pin": "R1.1"}], [{"op": "add", "ref": "C1", "part": []}],
                        [{"op": "remove", "ref": "R1", "side": "library"}]):
            with self.subTest(invalid=invalid), self.assertRaises(ChangeError):
                validate_postconditions(invalid)

    def test_a_disconnect_may_split_only_the_named_pin(self):
        parts = dict.fromkeys(("R1", "R2", "R3", "R4"), "Device:R")
        old = {"R1.1": "/A/EN", "R2.1": "/A/EN", "R3.1": "/B/EN", "R4.1": "/B/EN"}
        detached = dict(old, **{"R1.1": "unconnected-R1"})
        declaration = [{"op": "disconnect", "pin": "R1.1"}]
        self.assertEqual(unmet(declaration, (parts, old), (parts, detached)), [])
        detached["R4.1"] = "unconnected-R4"
        self.assertTrue(any("R3.1 lost R4.1" in m for m in unmet(declaration, (parts, old), (parts, detached))))

    def test_added_and_swapped_parts_are_verified_and_other_nets_stay_separate(self):
        parts = {"U1": "Lib:Old", "C1": "Lib:C", "C2": "Lib:C"}
        old = {"U1.1": "GND", "C1.1": "GND", "C2.1": "VCC"}
        expected = [{"op": "add", "ref": "C3", "part": "Lib:C"},
                    {"op": "label", "pin": "C3.2", "net": "GND"},
                    {"op": "swap", "ref": "U1", "part": "Lib:New"}]
        new_parts = dict(parts, U1="Lib:New", C3="Lib:C")
        new = dict(old, **{"U1.1": "VCC", "C3.2": "GND"})
        self.assertEqual(unmet(expected, (parts, old), (new_parts, new)), [])
        self.assertTrue(unmet(expected, (parts, old), (parts, old)))
        new["C2.1"] = "GND"
        self.assertTrue(any("C2.1" in m for m in unmet(expected, (parts, old), (new_parts, new))))


class EditDrawingTests(unittest.TestCase):
    def test_crop_uses_both_ends_of_a_move_with_margin(self):
        from io import BytesIO
        from PIL import Image, ImageDraw
        from alx.providers.kicad_drawing import crop
        before = Image.new("RGB", (2400, 1800), "white")
        after = before.copy()
        ImageDraw.Draw(before).rectangle((100, 100, 110, 110), fill="black")
        ImageDraw.Draw(after).rectangle((200, 100, 210, 110), fill="black")
        with Image.open(BytesIO(crop(before, after))) as image:
            self.assertEqual(image.size, (171, 71))
            self.assertEqual(image.getpixel((135,35)), (0,0,0))
            self.assertEqual(image.getpixel((35,35)), (255,255,255))

    def test_missing_renderer_is_a_diagnostic(self):
        from types import SimpleNamespace
        from alx.providers.kicad_drawing import drawings
        result = drawings(SimpleNamespace(engine_paths={}), None, None, ["a.kicad_sch"], False)
        self.assertEqual(result, [{"file": "a.kicad_sch", "unavailable": "pdftoppm unavailable"}])


class SetCheckTests(unittest.TestCase):
    """18 Nov review: the first set_check passed P0009's 206 thinned designator strokes."""

    def field(self, uid, ref, size, thickness, at=(0.0, 0.0)):
        return {"id": uid, "kind": "field", "ref": ref, "name": "Reference", "size": list(size),
                "thickness": thickness, "visible": True, "angle": 0.0, "at": list(at), "offset": list(at)}

    def board(self, *objects):
        return {"version": 2, "objects": list(objects), "nets": []}

    def test_the_thinned_strokes_now_fail(self):
        from alx.providers.kicad_change import set_check
        before = self.board(self.field("a", "R1", (0.5, 0.5), 0.254), self.field("b", "R2", (1.27, 1.27), 0.254))
        damaged = self.board(self.field("a", "R1", (0.5, 0.5), 0.075), self.field("b", "R2", (0.5, 0.5), 0.075))
        repaired = self.board(self.field("a", "R1", (0.5, 0.5), 0.254), self.field("b", "R2", (0.5, 0.5), 0.254))
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "ids": ["a", "b"]}]
        receipts, problems = set_check(change, before, damaged)
        self.assertTrue(any("2 objects changed that no set asked for" in p and "thickness" in p for p in problems), problems)
        receipts, problems = set_check(change, before, repaired)
        self.assertEqual(problems, [])
        self.assertEqual((receipts[0]["already"], receipts[0]["changed"], list(receipts[0]["ids_changed"])), (1, 1, ["b"]))

    def test_a_move_that_did_nothing_fails_and_others_must_not_move(self):
        from alx.providers.kicad_change import set_check
        text = {"id": "t", "kind": "text", "text": "Keepout", "at": [10.0, 10.0], "size": [1, 1], "thickness": 0.2,
                "visible": True, "angle": 0.0, "layer": "F.SilkS", "box": True}
        other = dict(text, id="u", text="Cellular")
        change = [{"op": "set", "kind": "text", "to": {"move_by": [2, 1]}, "ids": ["t"]}]
        _, problems = set_check(change, self.board(text, other), self.board(text, other))
        self.assertTrue(any("not as asked" in p for p in problems), problems)
        moved = dict(text, at=[12.0, 11.0])
        receipts, problems = set_check(change, self.board(text, other), self.board(moved, other))
        self.assertEqual((problems, receipts[0]["changed"]), ([], 1))
        _, problems = set_check(change, self.board(text, other), self.board(moved, dict(other, at=[0.0, 0.0])))
        self.assertTrue(any("u (text: at)" in p for p in problems), problems)

    def test_another_operation_in_the_batch_does_not_hide_an_outline_move(self):
        """19 Nov recheck: a title-block edit switched the comparison off; an Edge.Cuts move passed."""
        from alx.providers.kicad_change import set_check
        a = self.field("a", "R1", (1, 1), 0.2)
        edge = {"id": "e", "kind": "graphic", "shape": "line", "layer": "Edge.Cuts", "start": [0, 0], "end": [4, 0]}
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "ids": ["a"]}, {"op": "title_block"}]
        _, problems = set_check(change, self.board(a, edge),
                                self.board(dict(a, size=[0.5, 0.5]), dict(edge, end=[14, 0])))
        self.assertTrue(any("e (graphic: end)" in p for p in problems), problems)
    def test_objects_may_not_appear_or_go(self):
        from alx.providers.kicad_change import set_check
        a = self.field("a", "R1", (0.5, 0.5), 0.2)
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "ids": ["a"]}]
        _, problems = set_check(change, self.board(a), self.board(a, self.field("z", "R9", (1, 1), 0.2)))
        self.assertTrue(any("appeared (1)" in p for p in problems), problems)


@unittest.skipUnless(PYTHON.is_file() and CLI.is_file() and DEVICE.is_file(), "KiCad is not installed")
class RealChangeTests(unittest.TestCase):
    def runtime(self, folder):
        root = Path(folder).resolve()
        project = root / "designs" / "board"
        project.mkdir(parents=True)
        (project / "board.kicad_pcb").write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
        (project / "board.kicad_pro").write_text(json.dumps({"meta": {"filename": "board.kicad_pro", "version": 1}}))
        (project / "board.kicad_sch").write_text(schematic())
        runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                      root / "storage")
        return runtime, project

    def call(self, runtime, action, **arguments):
        return kicad_call(runtime.executors, action, arguments)

    def test_label_join_is_new_only_and_requires_a_visible_declaration(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            schematic_path = project / "board.kicad_sch"
            drawing = schematic_path.read_text().rstrip()[:-1]
            schematic_path.write_text(drawing + """
(text "One block" (at 85 80 0) (effects (font (size 1.524 1.524) (bold yes))))
(polyline (pts (xy 85 81) (xy 100 81)) (stroke (width 0.254) (type solid)))
)
""")
            work = self.call(runtime, "check", project_dir=str(project)).values["work_copy_id"]
            root, _ = runtime.provider._load(work)
            copy = root / "project/board.kicad_sch"
            before = copy.read_bytes()
            script = ('s=sheet("board.kicad_sch"); s.label_pin("R1","1","SIGNAL"); '
                      's.label_pin("R3","1","SIGNAL"); s.save()')
            intent = [{"op": "label", "pin": pin, "net": "/SIGNAL"} for pin in ("R1.1", "R3.1")]
            denied = self.call(runtime, "edit", work_copy_id=work, script=script, intent=intent)
            self.assertEqual(denied.failure["code"], "change_unmet", denied.failure)
            evidence = denied.failure["recovery"]["gate"]["label_joins"][0]
            self.assertEqual(evidence["net"], "/SIGNAL")
            self.assertEqual(tuple(evidence["pins"]), ("R1.1", "R3.1"))
            self.assertEqual(len(evidence["positions"]), 2)
            self.assertEqual(copy.read_bytes(), before)
            declaration = {"label_net": "SIGNAL", "reason": "Keep the connector break visible"}
            kept = self.call(runtime, "edit", work_copy_id=work, script=script, intent=intent + [declaration])
            self.assertEqual(kept.state, CapabilityResultState.SUCCEEDED, kept.failure)
            self.assertEqual(dict(kept.values["change_report"]["label_exceptions"][0]), declaration)
            another_edit = 's=sheet("board.kicad_sch"); s.tidy_fields("R4"); s.save()'
            result = self.call(runtime, "edit", work_copy_id=work, script=another_edit, intent=[])
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)

    def test_label_join_without_blocks_is_advisory(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            work = self.call(runtime, "check", project_dir=str(project)).values["work_copy_id"]
            script = ('s=sheet("board.kicad_sch"); s.label_pin("R1","1","SIGNAL"); '
                      's.label_pin("R3","1","SIGNAL"); s.save()')
            intent = [{"op": "label", "pin": pin, "net": "/SIGNAL"} for pin in ("R1.1", "R3.1")]
            kept = self.call(runtime, "edit", work_copy_id=work, script=script, intent=intent)
            self.assertEqual(kept.state, CapabilityResultState.SUCCEEDED, kept.failure)
            gate = kept.values["change_report"]["gate"]
            self.assertTrue(gate["label_joins"][0]["warning"])
            self.assertIn("block boundaries unavailable", gate["style_warnings"][0])

    def test_orientation_is_new_only_and_reports_the_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            (project / "board.kicad_sch").write_text(P0005_FIXTURE.read_text().replace(
                '(project "t"', '(project "board"'))
            work = self.call(runtime, "check", project_dir=str(project)).values["work_copy_id"]
            # U2 already has a vertical flip in the fixture. Add a new route-free
            # defect to a real power symbol, with its electrical contact preserved.
            script = """
s = sheet("board.kicad_sch")
ref = s.place_power("FireFli_Power:GND", (30.48, 45.72))
part = s.symbol(ref)
s.add_no_connect(next(iter(part["pins"].values())))
_relocate_symbol(s, ref, part["at"], 180, None)
s.save()
"""
            args = dict(work_copy_id=work, script=script)
            rejected = self.call(runtime, "edit", **args, intent=[])
            self.assertEqual(rejected.failure["code"], "change_unmet", rejected.failure)
            issues = rejected.failure["recovery"]["gate"]["orientation"]
            self.assertEqual([issue["rule"] for issue in issues], ["ground graphic must be below its pin"])
            declaration = {"orientation": issues[0]["ref"], "reason": "Exercise the checked style exception"}
            kept = self.call(runtime, "edit", **args, intent=[declaration])
            self.assertEqual(kept.state, CapabilityResultState.SUCCEEDED, kept.failure)
            self.assertEqual(dict(kept.values["change_report"]["orientation_exceptions"][0]), declaration)

    def test_unreferenced_sheet_can_be_deleted_and_failed_edits_remove_created_files(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            orphan = "RS485_Interface.kicad_sch"
            (project / orphan).write_text("(kicad_sch)")
            work = self.call(runtime, "check", project_dir=str(project)).values["work_copy_id"]
            root, _ = runtime.provider._load(work)
            remove = f'from pathlib import Path\nPath({orphan!r}).unlink()'
            failure = self.call(runtime, "edit", work_copy_id=work, intent=[],
                                script=remove + '\nPath("created.kicad_sch").write_text("(kicad_sch)")\nraise ValueError("test")')
            self.assertEqual(failure.failure["recovery"]["rollback_status"], "restored")
            self.assertIn("ValueError: test", failure.failure["recovery"]["stderr"])
            self.assertFalse((root / "project/created.kicad_sch").exists())
            self.assertTrue((root / "project" / orphan).is_file())
            result = self.call(runtime, "edit", work_copy_id=work, intent=[], script=remove)
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            self.assertFalse((root / "project" / orphan).exists())
            self.assertTrue((project / orphan).is_file())

    def test_placement_and_loose_table_warnings_keep_the_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            work = self.call(runtime, "check", project_dir=str(project)).values["work_copy_id"]
            result = self.call(runtime, "edit", work_copy_id=work, intent=[], script="""
s = sheet("board.kicad_sch")
block_grid(s, "port", (101.6, 93.98), ["in", "out"], ["rail", 1, "ground"], col_pitch=50.8)
for y in (120.65, 125.73, 130.81):
    s._append('(text "Supply: 5 V" (at 101.6 %g 0) (effects (font (size 1.016 1.016))) (uuid "%s"))'
              % (y, _uuid.uuid4()))
s.save()
""")
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            warnings = result.values["change_report"]["gate"]["style_warnings"]
            self.assertTrue(any("off the block slots" in w for w in warnings), warnings)
            self.assertTrue(any("use info_table" in w for w in warnings), warnings)

    def test_a_view_through_the_tool_gives_every_kind_with_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            work = self.call(runtime, "check_kicad_board", project_dir=str(project)).values["work_copy_id"]
            parts = self.call(runtime, "select", work_copy_id=work, kind="part").values["objects"]
            seen = self.call(runtime, "select", work_copy_id=work, kind="view", refs=[parts[0]["ref"]]).values
            self.assertEqual(seen["kind"], "view")
            self.assertEqual(sum(seen["counts"].values()), seen["count"])
            self.assertIn("part", seen["counts"])
            refused = self.call(runtime, "select", work_copy_id=work, kind="view", refs=[parts[0]["ref"]], name="x")
            self.assertEqual(refused.failure["code"], "arguments_unusable")

    def test_select_then_set_verifies_the_ids_at_the_selected_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            work = self.call(runtime, "check_kicad_board", project_dir=str(project)).values["work_copy_id"]
            found = self.call(runtime, "select", work_copy_id=work, kind="field", name="Reference").values
            ids = [o["id"] for o in found["objects"]]
            sizing = {"op": "set", "side": "board", "ids": ids, "kind": "field", "to": {"size": 0.5}}
            args = dict(work_copy_id=work, intent=[sizing],
                        script=f"b = load()\nset_properties(b, {ids!r}, {{'size': 0.5}})\nsave(b)")
            unbound = self.call(runtime, "edit_kicad_work_copy", **args)
            self.assertIn("revision", unbound.failure["recovery"]["detail"])
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_revision=found["work_copy_revision"], **args)
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            self.assertEqual(result.values["change_report"]["set"][0]["matched"], len(ids))
            stale = self.call(runtime, "edit_kicad_work_copy", work_copy_revision=found["work_copy_revision"], **args)
            self.assertEqual(stale.failure["code"], "work_copy_changed")

    def test_gate_and_rollback_on_the_real_exported_netlist(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            work = self.call(runtime, "check_kicad_board", project_dir=str(project)).values["work_copy_id"]
            copy = next(Path(folder).rglob(work + "/project/board.kicad_sch"))
            initial = copy.read_bytes()
            def edit(script, **args):
                return self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, script=script, **args)
            wire = {"op": "wire", "from": "R1.2", "to": "R2.1"}
            script = 's = sheet("board.kicad_sch")\ns.wire_pin_to("R1","2",s.symbol("R2")["pins"]["1"])\ns.save()'
            for script_, declaration, detail in [("pass", [wire], "R1.2 is not joined to R2.1"),
                                                  (script, [], "R1.2 gained R2.1")]:
                refused = edit(script_, intent=declaration)
                self.assertEqual(refused.failure["code"], "change_unmet", refused.failure)
                self.assertIn(detail, refused.failure["recovery"]["unmet"])
                self.assertEqual(copy.read_bytes(), initial)
            missing = edit(script)
            self.assertIn("intent is required", str(missing.failure))
            good = edit(script, intent=[wire])
            self.assertEqual(good.state, CapabilityResultState.SUCCEEDED, good.failure)
            self.assertEqual(good.values["change_report"]["unmet"], ())
            drawings = good.values["change_report"]["drawings"]
            digest = drawings[0]["image_sha256"]
            from PIL import Image
            with Image.open(runtime.provider.image_store / (digest + ".png")) as img:
                self.assertLessEqual(max(img.size), 1600)
            self.assertNotEqual(copy.read_bytes(), initial)
            self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, revert_to="start")
            self.assertEqual(copy.read_bytes(), initial)
            unknown = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, change=[])
            self.assertIn("not taken by edit: change", str(unknown.failure))

    def test_presentation_edits_keep_the_real_netlist_and_work_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            copy = self.call(runtime, "check", project_dir=str(project)).values
            work = copy["work_copy_id"]
            schematic = next(Path(folder).rglob(work + "/project/board.kicad_sch"))
            original = schematic.read_bytes()
            token = presentation_edit_scope.set(PresentationEditScope(work, copy["work_copy_revision"]))
            try:
                def edit(script, **args):
                    return self.call(runtime, "edit", work_copy_id=work, script=script, **args)

                wire = {"op": "wire", "from": "R1.2", "to": "R2.1"}
                for args in ({}, {"intent": [wire]}):
                    refused = edit("raise RuntimeError('script must not run')", **args)
                    self.assertEqual(refused.failure["code"], "arguments_unusable", refused.failure)
                    self.assertIn("require intent: []", str(refused.failure))
                joined = edit('s = sheet("board.kicad_sch")\n'
                              's.wire_pin_to("R1","2",s.symbol("R2")["pins"]["1"])\ns.save()', intent=[])
                self.assertEqual(joined.failure["code"], "change_unmet", joined.failure)
                self.assertIn("R1.2 gained R2.1", joined.failure["recovery"]["unmet"])
                self.assertEqual(joined.failure["recovery"]["rollback_status"], "restored")
                changed_value = edit(
                    'import sys\nfrom pathlib import Path\n'
                    'p = Path(sys.argv[1]).with_suffix(".kicad_sch")\n'
                    'p.write_text(p.read_text().replace(\'(property "Value" "10k"\', \'(property "Value" "100"\'))',
                    intent=[])
                self.assertIn("presentation changed R1 value", str(changed_value.failure))
                self.assertEqual(schematic.read_bytes(), original)
                tidied = edit('s = sheet("board.kicad_sch")\ns.tidy_fields("R1")\ns.save()', intent=[])
                self.assertEqual(tidied.state, CapabilityResultState.SUCCEEDED, tidied.failure)
                self.assertNotEqual(schematic.read_bytes(), original)
                self.assertNotEqual(tidied.values["work_copy_revision"], copy["work_copy_revision"])
                stale = edit("pass", intent=[])
                self.assertEqual(stale.failure["code"], "work_copy_changed", stale.failure)
            finally:
                presentation_edit_scope.reset(token)

    def test_split_keeps_existing_erc_error_and_symbol_instance_identities(self):
        from alx.providers.kicad import sexpr
        from alx.providers.kicad_index import netlist_nets

        for keep in ("moved", "remaining"):
            with self.subTest(keep=keep), tempfile.TemporaryDirectory() as folder:
                runtime, project = self.runtime(folder)
                top = project / "board.kicad_sch"
                text = top.read_text().replace('(project "t"', '(project "board"')
                definitions = []
                for kind in ("open_collector", "power_out"):
                    definition = library_symbol("R").replace('(symbol "R"', f'(symbol "Test:{kind}"', 1)
                    definitions.append(definition.replace('(pin passive ', f'(pin {kind} ').replace('(symbol "R_', f'(symbol "{kind}_'))
                    text = text.replace('(lib_id "Device:R")', f'(lib_id "Test:{kind}")', 1)
                top.write_text(text.replace('(lib_symbols', '(lib_symbols ' + ' '.join(definitions), 1))
                root_id = sexpr(top.read_text()).named('uuid')[0].children[1]
                program = """
s = sheet("board.kicad_sch")
s.wire_pin_to("R1", "2", s.symbol("R2")["pins"]["1"])
for ref, pin in (("R1", "1"), ("R2", "2"), ("R3", "1"), ("R3", "2"), ("R4", "1"), ("R4", "2")):
    s.add_no_connect(s.symbol(ref)["pins"][pin])
s.save()
new_sheet("Original", "original.kicad_sch")
move_region("board.kicad_sch", "original.kicad_sch", [90, 90, 190, 120])
"""
                # Prepare the fixture before making the checked work copy.
                from alx.providers.kicad_helpers import HELPERS, script_runner
                from alx.providers.kicad_schematic_helpers import SCHEMATIC_HELPERS
                import subprocess
                helper = Path(folder) / "fixture.py"
                helper.write_text(HELPERS + SCHEMATIC_HELPERS + script_runner(program))
                subprocess.run([str(PYTHON), str(helper), str(top.with_suffix('.kicad_pcb'))],
                               cwd=project, check=True, capture_output=True, timeout=120)
                initial = self.call(runtime, "check", project_dir=str(project))
                self.assertEqual(initial.state, CapabilityResultState.SUCCEEDED, initial.failure)
                report = initial.values['erc']['groups']
                self.assertTrue(any(group['check_type'] == 'pin_to_pin' and group['severity'] == 'error'
                                    for group in report), report)
                work = initial.values["work_copy_id"]
                provider = runtime.provider
                root, meta = provider._load(work)
                copy = root / "project"

                def checked_hierarchy():
                    model = provider._netlist(copy, meta['board'], root / 'artifacts')
                    ids = {}
                    for file in provider._covered_sheets(copy, meta['board'], model):
                        tree = sexpr((copy / file).read_text())
                        for symbol in tree.named('symbol'):
                            path = symbol.named('instances')[0].named('project')[0].named('path')[0]
                            ref = path.named('reference')[0].children[1]
                            ids[ref] = (path.children[1], symbol.named('uuid')[0].children[1])
                    for part in model['components']:
                        self.assertEqual(ids[part['ref']][0], f"/{root_id}" + part['path'].rsplit('/', 1)[0])
                    return ids, set(netlist_nets(model))

                before, nets = checked_hierarchy()
                def source_sheet_block():
                    page = (copy / top.name).read_text()
                    node = sexpr(page).named('sheet')[0]
                    return page[node.start:node.end]

                old_sheet = source_sheet_block()
                script = f'print(split_sheet("original.kicad_sch", [95,89,134,118], "New", "new.kicad_sch", keep={keep!r}))'
                edited = self.call(runtime, "edit", work_copy_id=work, intent=[], script=script)
                self.assertEqual(edited.state, CapabilityResultState.SUCCEEDED, edited.failure)
                change = edited.values['change_report']
                self.assertEqual(change['gate']['erc'], ())
                self.assertEqual(change['erc']['new']['total'], 0)
                after, after_nets = checked_hierarchy()
                self.assertEqual(nets, after_nets)
                self.assertEqual(set(before), set(after))
                self.assertEqual(old_sheet, source_sheet_block())
                for ref, (path, uid) in after.items():
                    self.assertEqual(uid, before[ref][1])
                    self.assertEqual(path == before[ref][0], (ref in {"R1", "R2"}) == (keep == "moved"))

    def test_new_style_violation_is_named_and_old_ones_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            work = self.call(runtime, "check_kicad_board", project_dir=str(project)).values["work_copy_id"]
            copy = next(Path(folder).rglob(work + "/project/board.kicad_sch"))
            tidy = 's = sheet("board.kicad_sch")\ns.tidy_fields("R1")\ns.save()'
            good = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, intent=[], script=tidy)
            self.assertEqual(good.state, CapabilityResultState.SUCCEEDED, good.failure)
            before = copy.read_bytes()
            tee = 's = sheet("board.kicad_sch")\ns.add_wire((10,10),(20,10))\ns.add_wire((15,10),(15,15))\ns.save()'
            bad = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, intent=[], script=tee)
            self.assertIn("T-join without junction", str(bad.failure))
            self.assertIn("(15.0, 10.0)", str(bad.failure))
            self.assertEqual(copy.read_bytes(), before)

    def with_ground(self, project, extra=""):
        """The sheet with a GND symbol (unannotated, as P0009 imports them) and extra items."""
        import uuid as _uuid
        power = (APP_SYMBOLS / "power.kicad_sym").read_text()
        start = power.index('(symbol "GND"')
        depth = 0
        for end in range(start, len(power)):
            depth += {"(": 1, ")": -1}.get(power[end], 0)
            if depth == 0:
                break
        gnd = power[start:end + 1].replace('(symbol "GND"', '(symbol "power:GND"', 1)
        text = (project / "board.kicad_sch").read_text()
        text = text.replace("\t(lib_symbols\n", "\t(lib_symbols\n\t\t" + gnd + "\n", 1)
        root = text.split('(uuid "', 1)[1].split('"', 1)[0]
        placed = ('\t(symbol\n\t\t(lib_id "power:GND")\n\t\t(at 200 150 0)\n\t\t(unit 1)\n'
                  '\t\t(uuid "%s")\n\t\t(property "Reference" "#PWR?"\n\t\t\t(at 200 155 0)\n\t\t)\n'
                  '\t\t(property "Value" "GND"\n\t\t\t(at 200 153 0)\n\t\t)\n'
                  '\t\t(pin "1"\n\t\t\t(uuid "%s")\n\t\t)\n\t\t(instances\n\t\t\t(project "t"\n'
                  '\t\t\t\t(path "/%s"\n\t\t\t\t\t(reference "#PWR?")\n\t\t\t\t\t(unit 1)\n'
                  '\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n' % (_uuid.uuid4(), _uuid.uuid4(), root))
        text = text.replace("\t(sheet_instances", placed + extra + "\t(sheet_instances", 1)
        (project / "board.kicad_sch").write_text(text)

    @staticmethod
    def wire(*points):
        import uuid as _uuid
        return "".join('\t(wire\n\t\t(pts\n\t\t\t(xy %g %g) (xy %g %g)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n'
                       '\t\t\t(type default)\n\t\t)\n\t\t(uuid "%s")\n\t)\n' % (a + b + (_uuid.uuid4(),))
                       for a, b in zip(points, points[1:]))

    def test_a_port_that_would_land_on_an_old_wire_is_refused_and_says_where(self):
        """15 Nov: two GND ports landed on the corner of an old RF1 wire and grounded RF1."""
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            # An old wire from R1.2 runs under R3, where R3.2's GND port would go.
            self.with_ground(project, self.wire((101.6, 105.41), (101.6, 107.95), (160, 107.95)))
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            copy = next(Path(folder).rglob(checked["work_copy_id"] + "/project/board.kicad_sch"))
            before = copy.read_text()
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], intent=[],
                               script='s=sheet("board.kicad_sch"); s.ground_pin("R3","2",power_template(s,"GND"))')
            self.assertEqual(result.failure["code"], "script_failed")
            self.assertIn("R3.2: the new wire would land on an existing wire at (152.4, 107.95)",
                          result.failure["recovery"]["stderr"])
            self.assertEqual(copy.read_text(), before)

    def test_leftovers_are_cleared_and_a_kept_edit_can_be_gone_back_over(self):
        import uuid as _uuid
        stray = ('\t(no_connect\n\t\t(at 50.8 50.8)\n\t\t(uuid "%s")\n\t)\n' % _uuid.uuid4()
                 + self.wire((40.64, 40.64), (45.72, 40.64)))
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            self.with_ground(project, stray)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            work = checked["work_copy_id"]
            copy = next(Path(folder).rglob(work + "/project/board.kicad_sch"))
            start = copy.read_text()
            first = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work,
                              intent=[{"op":"label", "pin":p, "net":"/OLD"} for p in ("R1.1", "R3.1")]
                              + [{"label_net": "OLD", "reason": "Exercise cleanup and revert of a kept label net"}],
                              script='s=sheet("board.kicad_sch"); s.label_pin("R1","1","OLD"); '
                                     's.label_pin("R3","1","OLD"); s.save()')
            self.assertEqual(first.state, CapabilityResultState.SUCCEEDED, first.failure)
            labelled = copy.read_text()
            cleared = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work,
                                intent=[{"op":"disconnect", "pin":p} for p in ("R1.1", "R3.1")],
                                script='s=sheet("board.kicad_sch"); s.remove_labels(["OLD"]); '
                                       's.remove_items([195,145,205,160],("power",)); s.remove_dangling(); s.save()')
            # Removing the stubs would reintroduce two formerly-resolved style/ERC violations.
            self.assertEqual(cleared.failure["code"], "change_unmet", cleared.failure)
            self.assertEqual(copy.read_text(), labelled)
            again = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, revert_to="start")
            self.assertEqual(again.state, CapabilityResultState.SUCCEEDED, again.failure)
            self.assertEqual(copy.read_text(), start)
            nothing = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, revert_to="start")
            self.assertEqual(nothing.failure["code"], "arguments_unusable")

    def test_a_project_is_found_by_its_number(self):
        """16 Nov: the agent spent 14 calls finding "P0009"."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            project = root / "designs" / "P0123-test-board" / "hardware" / "kicad"
            project.mkdir(parents=True)
            (project / "board.kicad_pcb").write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            (project / "board.kicad_pro").write_text(json.dumps({"meta": {"filename": "board.kicad_pro", "version": 1}}))
            (project / "board.kicad_sch").write_text(schematic())
            (project / "backups" / "old").mkdir(parents=True)
            (project / "backups" / "old" / "board.kicad_pcb").write_text("(kicad_pcb)")
            runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                          root / "storage")
            for name in ("P0123", str(root / "designs" / "P0123-test-board")):
                with self.subTest(name=name):
                    checked = self.call(runtime, "check_kicad_board", project_dir=name)
                    self.assertEqual(checked.state, CapabilityResultState.SUCCEEDED, checked.failure)
            missing = self.call(runtime, "check_kicad_board", project_dir="P0999")
            self.assertIn("no project P0999", missing.failure["recovery"]["detail"])

    def test_a_missing_power_port_names_what_the_sheet_has(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], intent=[],
                               script='s=sheet("board.kicad_sch"); s.ground_pin("R1","1",power_template(s,"GND"))')
            self.assertEqual(result.failure["code"], "script_failed")
            self.assertIn("no GND port", result.failure["recovery"]["stderr"])



class BriefInspectTests(unittest.TestCase):
    def test_brief_lists_only_the_parts_names_from_the_board_index(self):
        """18 Nov: the full reading of P0009 was too much, so references were guessed in sixes.
        The short list comes from the board index; KiCad is not started for it."""
        import tempfile
        from unittest import mock
        from alx.providers.kicad import KiCadProvider
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "b.kicad_pcb"
            board.write_text('(kicad_pcb (footprint "Lib:C_0402" (layer "F.Cu") (uuid "u1") (at 1 2)'
                             ' (property "Reference" "C1") (property "Value" "100n")))')
            provider = KiCadProvider.__new__(KiCadProvider)
            provider.execute_python, provider.work_root = object(), Path(folder) / "work"
            with mock.patch.object(KiCadProvider, "_board_for", return_value=board), \
                    mock.patch("alx.providers.kicad_section.read_overview") as overview:
                result = provider.inspect({"work_copy_id": "w", "brief": True})
            overview.assert_not_called()
            self.assertEqual(result["parts"], [{"ref": "C1", "value": "100n", "footprint": "Lib:C_0402"}])
            self.assertEqual(result["nets"], [])
            with mock.patch.object(KiCadProvider, "_board_for", return_value=board):
                self.assertEqual(provider.inspect({"work_copy_id": "w", "brief": True, "nets": ["NONE"]})["parts"], [])


class KeptReadingTests(unittest.TestCase):
    def test_a_kept_index_of_an_older_shape_is_read_again(self):
        """18 Nov review: a step-2 index of the same bytes, without text angle, was still used."""
        import hashlib
        import tempfile
        from alx.providers.kicad import KiCadProvider
        from alx.providers.kicad_index import INDEX_VERSION
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "b.kicad_pcb"
            board.write_text('(kicad_pcb (gr_text "Keepout" (at 1 2 90) (layer "F.SilkS") (uuid "t1")))')
            scratch = Path(folder) / "artifacts"
            scratch.mkdir()
            key = hashlib.sha256(board.read_text().encode()).hexdigest()[:32]
            (scratch / ("index-%s.json" % key)).write_text('{"objects": [], "nets": []}')
            provider = KiCadProvider.__new__(KiCadProvider)
            index = provider._board_index(board, scratch)
            self.assertEqual(index["version"], INDEX_VERSION)
            self.assertEqual(index["objects"][0]["angle"], 90.0)
            # Something that is not an index at all is read again too.
            (scratch / ("index-%s.json" % key)).write_text('[1, 2]')
            self.assertEqual(provider._board_index(board, scratch)["version"], INDEX_VERSION)
            for n in range(12):
                (scratch / ("index-old%d.json" % n)).write_text("{}")
            provider._keep_latest(scratch, "index-")
            self.assertEqual(len(list(scratch.glob("index-*.json"))), 8)


class OneWayToChangeACopyTests(unittest.TestCase):
    def test_the_edit_contract_and_confined_runner_have_only_one_edit_surface(self):
        import ast
        from alx.providers import kicad_change
        from alx.providers.kicad_board_helpers import BOARD_HELPERS
        from alx.providers.kicad_helpers import HELPERS
        from alx.providers.kicad_schematic_helpers import SCHEMATIC_HELPERS
        from alx.tools.kicad import CONTRACTS, KiCadAction
        fields = set(CONTRACTS[KiCadAction.EDIT].input_schema.properties)
        program = HELPERS + SCHEMATIC_HELPERS + BOARD_HELPERS

        def check(arguments, helpers):
            self.assertIn("script", arguments)
            self.assertNotIn("change", arguments)
            self.assertFalse({"validate_change", "apply_change"} & set(vars(kicad_change)))
            names = {n.name for n in ast.walk(ast.parse(helpers)) if isinstance(n, ast.FunctionDef)}
            self.assertNotIn("apply_change", names)
        check(fields, program)
        for args, helpers in ((fields | {"change"}, program), (fields, program + "\ndef apply_change(ops): pass")):
            with self.assertRaises(AssertionError):
                check(args, helpers)

    def test_only_change_snapshots_restores_and_records_history(self):
        """Law 0, 18 Nov review: edit, hierarchy conversion, taking his edits and revert each had
        their own copy-and-restore. Now one context manager does it; this fails if another returns."""
        import ast
        source = (Path(__file__).resolve().parents[1] / "src/alx/providers/kicad.py").read_text()
        provider = next(node for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.ClassDef) and node.name == "KiCadProvider")
        owners = {}
        for method in provider.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Constant) and node.value in ("snapshot-", "before-take-"):
                    owners.setdefault("snapshot", set()).add(method.name)
                if (isinstance(node, ast.Attribute) and node.attr == "_record_history"
                        and isinstance(node.ctx, ast.Load)):
                    owners.setdefault("history", set()).add(method.name)
                # Any copy of a whole project tree back into place (18 Nov review: hierarchy's retry did).
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "copytree"
                        and node.args and "snapshot" in ast.unparse(node.args[0])):
                    owners.setdefault("restore", set()).add(method.name)
        self.assertEqual(owners, {"snapshot": {"_change"}, "history": {"_change"}, "restore": {"_change"}})
        users = {method.name for method in provider.body if isinstance(method, ast.FunctionDef)
                 and "self._change(" in ast.get_source_segment(source, method)}
        self.assertEqual(users, {"edit", "make_hierarchical", "take_edits", "_revert", "add_part", "add_passive"})

if __name__ == "__main__":
    unittest.main()
