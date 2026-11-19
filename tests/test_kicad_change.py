"""D-084: a structured change, carried out and checked by code, on real KiCad.

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
from alx.providers.kicad_change import ChangeError, unmet, validate_change  # noqa: E402
from kicad_actions import kicad_call  # noqa: E402
from test_kicad_schematic_helpers import APP, CLI, DEVICE, PYTHON, schematic  # noqa: E402

APP_SYMBOLS = APP / "SharedSupport/symbols"

FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


class ChangeShapeTests(unittest.TestCase):
    def test_a_change_names_every_field_and_its_library_parts(self):
        plan = validate_change([
            {"op": "swap_symbol", "sheet": "a.kicad_sch", "ref": "U6", "lib_id": "FireFli_ICs:RFSW6024TR13"},
            {"op": "wire", "sheet": "a.kicad_sch", "from": "U6.9", "to": "C37.1"},
            {"op": "replace_part", "ref": "U6", "footprint": "FireFli_ICs/RFSW6024TR13", "pin_nets": {"9": "RFC2_P"}},
        ])
        self.assertEqual(plan["symbols"], ["FireFli_ICs:RFSW6024TR13"])
        self.assertEqual(plan["footprints"], ["FireFli_ICs/RFSW6024TR13"])
        validate_change([{"op": "set", "kind": "field", "where": {"name": "Reference"}, "to": {"size": 0.5}}])
        validate_change([{"op": "set", "kind": "text", "where": {"reads": "Keepout"}, "to": {"move_by": [0, -1.5]}}])
        for wrong, said in (([], "1 to 200"), ([{"op": "draw"}], "op is one of"),
                            ([{"op": "wire", "sheet": "a", "from": "U6.9"}], "needs to"),
                            ([{"op": "wire", "sheet": "a", "from": "U6-9", "to": "C1.1"}], "REF.PIN"),
                            ([{"op": "track", "width": 0.3}], "points, or from and to"),
                            ([{"op": "set", "kind": "part", "where": {}, "to": {"x": 1}}], "kind is one of"),
                            ([{"op": "set", "kind": "via", "where": {"nets": ["GND"]}, "to": {"width": 1}}], "to sets size, drill"),
                            ([{"op": "set", "kind": "field", "where": {"colour": "red"}, "to": {"size": 1}}], "where takes"),
                            ([{"op": "set", "kind": "text", "where": {"all": True}, "to": {"move_by": [1]}}], r"move_by is \[x, y\]"),
                            ([{"op": "set", "kind": "text", "where": {"name": None}, "to": {"visible": False}}], "where name is a value"),
                            ([{"op": "set", "kind": "text", "where": {"all": "false"}, "to": {"visible": False}}], "where all is true"),
                            ([{"op": "set", "kind": "field", "where": {"all": True, "name": "Value"}, "to": {"visible": False}}], "goes alone"),
                            ([{"op": "set", "kind": "via", "where": {"nets": []}, "to": {"size": 1}}], "a list of net names")):
            with self.subTest(said=said), self.assertRaisesRegex(ChangeError, said):
                validate_change(wrong)

    def test_the_result_is_checked_against_the_change_and_nothing_else_moves(self):
        before = {"R1.1": frozenset({"R1.1", "R9.1"}), "R9.1": frozenset({"R1.1", "R9.1"}),
                  "R5.1": frozenset({"R5.1", "R6.1"}), "R6.1": frozenset({"R5.1", "R6.1"})}
        after = {"R1.1": frozenset({"R1.1"}), "R9.1": frozenset({"R9.1"}),
                 "R5.1": frozenset({"R5.1"}), "R6.1": frozenset({"R6.1"})}
        problems = unmet([{"op": "wire", "sheet": "a", "from": "R1.1", "to": "R9.1"}], before, after,
                         {}, {}, ())
        self.assertIn("R1.1 is not joined to R9.1", problems)
        # R5 and R6 were never named, and came apart: that is reported too.
        self.assertTrue(any(p.startswith("R5.1 changed connection") for p in problems))

    def test_a_short_through_a_named_part_is_still_caught(self):
        """15 Nov: tidy_fields named C39, the old check then left C39 out, and RF1 grounded unseen."""
        rf1 = frozenset({"U6.9", "C39.1"})
        gnd = frozenset({"R5.2", "R6.2"})
        before = {"U6.9": rf1, "C39.1": rf1, "U6.10": frozenset({"U6.10"}), "R5.2": gnd, "R6.2": gnd}
        shorted = rf1 | gnd | {"U6.10"}
        after = {pin: shorted for pin in shorted}
        change = [{"op": "swap_symbol", "sheet": "a", "ref": "U6", "lib_id": "X:Y"},
                  {"op": "power", "sheet": "a", "pin": "U6.10", "port": "power:GND"},
                  {"op": "tidy_fields", "sheet": "a", "refs": ["U6", "C39"]}]
        names = {"R5.2": "GND", "R6.2": "GND", "U6.9": "Net-(U6-RF1)", "C39.1": "Net-(U6-RF1)"}
        problems = unmet(change, before, after, {pin: "GND" for pin in shorted}, {}, (), names)
        self.assertTrue(any(p.startswith("C39.1 changed connection: gained R5.2, R6.2") for p in problems),
                        problems)
        # The same port, landing cleanly, is what was asked.
        clean = {"U6.9": rf1, "C39.1": rf1, "U6.10": gnd | {"U6.10"}, "R5.2": gnd | {"U6.10"},
                 "R6.2": gnd | {"U6.10"}}
        self.assertEqual(unmet(change, before, clean, {"U6.10": "GND", "R5.2": "GND", "R6.2": "GND"},
                               {}, (), names), [])


    def test_a_pin_on_a_whole_net_is_named_by_that_net(self):
        """16 Nov: U6.11 was reported joined to 120 pins; the agent could not see it was GND."""
        gnd = frozenset({"U6.11"} | {"C%d.1" % n for n in range(10, 30)})
        after = {pin: gnd for pin in gnd}
        before = {"U6.11": frozenset({"U6.11"}), **{pin: gnd - {"U6.11"} for pin in gnd - {"U6.11"}}}
        names = {pin: "GND" for pin in gnd}
        problems = unmet([{"op": "no_connect", "sheet": "a", "pins": ["U6.11"]},
                          {"op": "swap_symbol", "sheet": "a", "ref": "U6", "lib_id": "X:Y"},
                          {"op": "label", "sheet": "a", "pin": "U6.11", "net": "RF"}],
                         before, after, names, {}, (), {})
        self.assertIn("U6.11 is joined to net GND (20 pins, e.g. C10.1, C11.1, C12.1), not left unconnected",
                      problems)
        self.assertTrue(any("unwire it first" in p for p in problems), problems)

    def test_an_added_part_joined_as_asked_is_not_reported(self):
        """16 Nov: the added C93 (second 220 pF on TX1) was reported as joined unasked."""
        tx1, gnd = frozenset({"L6.2", "C44.2"}), frozenset({"C54.1", "C5.1"})
        before = {"L6.2": tx1, "C44.2": tx1, "C54.1": gnd, "C5.1": gnd}
        after = {**{p: tx1 | {"C93.1"} for p in tx1 | {"C93.1"}}, **{p: gnd | {"C93.2"} for p in gnd | {"C93.2"}}}
        names = {p: "GND" for p in gnd | {"C93.2"}}
        change = [{"op": "add_symbol", "sheet": "a", "ref": "C93", "like": "C55", "at": [1, 2]},
                  {"op": "wire", "sheet": "a", "from": "C93.1", "to": "L6.2"},
                  {"op": "power", "sheet": "a", "pin": "C93.2", "port": "GND"}]
        self.assertEqual(unmet(change, before, after, names, {}, (), names), [])
        shorted = {p: tx1 | gnd | {"C93.1", "C93.2"} for p in tx1 | gnd | {"C93.1", "C93.2"}}
        self.assertTrue(unmet(change[:2], before, shorted, names, {}, (), names))

    def test_a_removal_may_split_only_what_it_targets(self):
        """18 Nov review: one removal anywhere in a batch let an unrelated split through."""
        ab, cd = frozenset({"R1.1", "R2.1"}), frozenset({"R5.1", "R6.1"})
        before = {"R1.1": ab, "R2.1": ab, "R5.1": cd, "R6.1": cd}
        names = {"R1.1": "A", "R2.1": "A", "R5.1": "C", "R6.1": "C"}
        both = {pin: frozenset({pin}) for pin in before}
        only_a = {"R1.1": frozenset({"R1.1"}), "R2.1": frozenset({"R2.1"}), "R5.1": cd, "R6.1": cd}
        unwire = [{"op": "unwire", "sheet": "a", "pins": ["R1.1"]}]
        self.assertEqual(unmet(unwire, before, only_a, names, {}, (), names), [])
        problems = unmet(unwire, before, both, names, {}, (), names)
        self.assertTrue(any(p.startswith("R5.1 changed connection: lost R6.1") for p in problems), problems)
        labels = [{"op": "remove_labels", "nets": ["A"]}]
        self.assertEqual(unmet(labels, before, only_a, names, {}, [{"removed": 1}], names), [])
        self.assertTrue(unmet(labels, before, both, names, {}, [{"removed": 1}], names))
        # Results are found by their operation's number, whatever order they ran in (review of #247).
        swapped_first = [{"op": "swap_symbol", "n": 3}, {"op": "swap_symbol", "n": 1}, {"op": "remove_labels", "n": 2, "removed": 1}]
        batch = [{"op": "swap_symbol", "sheet": "a", "ref": "U6", "lib_id": "X:Y"}, labels[0],
                 {"op": "swap_symbol", "sheet": "a", "ref": "U7", "lib_id": "X:Y"}]
        self.assertEqual(unmet(batch, before, only_a, names, {}, swapped_first, names), [])
        # Removing no label excuses nothing (19 Nov recheck).
        self.assertTrue(unmet(labels, before, only_a, names, {}, [{"removed": 0}], names))
        items = [{"op": "remove_items", "sheet": "a", "area": [0, 0, 1, 1]}]
        self.assertEqual(unmet(items, before, only_a, names, {}, [{"scope": ["R1.1", "R2.1"]}], names), [])
        self.assertTrue(unmet(items, before, both, names, {}, [{"scope": ["R1.1", "R2.1"]}], names))
        # Removing what touches nothing parts no pins.
        self.assertTrue(unmet([{"op": "remove_dangling", "sheet": "a"}], before, only_a, names, {}, (), names))
        # A net that vanishes altogether is caught too (18 Nov review: only pins still there were checked).
        vanished = {"R1.1": ab, "R2.1": ab}
        problems = unmet([{"op": "remove_dangling", "sheet": "a"}], before, vanished, names, {}, (), names)
        self.assertTrue(any("R5.1 is gone from the schematic" in p for p in problems), problems)
        # Removing "EN" labels does not excuse a split of another sheet's EN.
        en = {"R1.1": frozenset({"R1.1", "R2.1"}), "R2.1": frozenset({"R1.1", "R2.1"}),
              "R5.1": frozenset({"R5.1", "R6.1"}), "R6.1": frozenset({"R5.1", "R6.1"})}
        en_names = {"R1.1": "/A/EN", "R2.1": "/A/EN", "R5.1": "/B/EN", "R6.1": "/B/EN"}
        split_b = {"R1.1": en["R1.1"], "R2.1": en["R2.1"], "R5.1": frozenset({"R5.1"}), "R6.1": frozenset({"R6.1"})}
        self.assertTrue(unmet([{"op": "remove_labels", "nets": ["EN"]}], en, split_b, en_names, {}, [{"removed": 1}], en_names))
        self.assertEqual(unmet([{"op": "remove_labels", "nets": ["/B/EN"]}], en, split_b, en_names, {},
                               [{"removed": 1}], en_names), [])
        # remove_items is excused only for the pins wired to what it removed.
        items = [{"op": "remove_items", "sheet": "a", "area": [0, 0, 1, 1]}]
        self.assertEqual(unmet(items, before, only_a, names, {}, [{"scope": ["R1.1"]}], names), [])
        self.assertTrue(unmet(items, before, both, names, {}, [{"scope": ["R1.1"]}], names))

    def test_the_board_can_take_the_schematic_in_one_operation(self):
        plan = validate_change([{"op": "sync_from_schematic", "place": {"C93": [170.0, 95.0, 90]}}])
        self.assertTrue(plan["board"])
        self.assertFalse(plan["schematic"])

    def test_labels_can_be_removed_on_every_sheet_at_once(self):
        self.assertEqual(validate_change([{"op": "remove_labels", "nets": ["V1"]}])["schematic"], True)


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
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "_ids": ["a", "b"]}]
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
        change = [{"op": "set", "kind": "text", "to": {"move_by": [2, 1]}, "_ids": ["t"]}]
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
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "_ids": ["a"]}, {"op": "title_block"}]
        _, problems = set_check(change, self.board(a, edge),
                                self.board(dict(a, size=[0.5, 0.5]), dict(edge, end=[14, 0])))
        self.assertTrue(any("e (graphic: end)" in p for p in problems), problems)
        # A placed part's fields may move with it when place is in the batch.
        moved = dict(self.field("b", "R2", (1, 1), 0.2), at=[5.0, 5.0])
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "_ids": ["a"]}, {"op": "place", "ref": "R2"}]
        _, problems = set_check(change, self.board(a, self.field("b", "R2", (1, 1), 0.2)),
                                self.board(dict(a, size=[0.5, 0.5]), moved))
        self.assertEqual(problems, [])

    def test_objects_may_not_appear_or_go(self):
        from alx.providers.kicad_change import set_check
        a = self.field("a", "R1", (0.5, 0.5), 0.2)
        change = [{"op": "set", "kind": "field", "to": {"size": 0.5}, "_ids": ["a"]}]
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

    def call(self, runtime, name, **arguments):
        return kicad_call(runtime.executors, name, arguments)

    def test_set_changes_exactly_what_it_names_on_what_it_picks(self):
        """18 Nov: every designator 0.5 mm took 95 operations and also thinned every stroke unseen.
        Now one set, a receipt, and a check that nothing else of that kind changed."""
        from alx.providers.kicad_index import board_index, select
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            found = kicad_call(runtime.executors, "select",
                               {"work_copy_id": checked["work_copy_id"], "kind": "field", "name": "Reference"})
            self.assertEqual(found.state, CapabilityResultState.SUCCEEDED, found.failure)
            before = board_index((project / "board.kicad_pcb").read_text())
            strokes = {o["id"]: o["thickness"] for o in select(before, "field", name="Reference")}
            unbound = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=[
                {"op": "set", "kind": "field", "where": {"name": "Reference"}, "to": {"size": 0.5}}])
            self.assertIn("pass work_copy_revision", unbound.failure["recovery"]["detail"])
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"],
                               work_copy_revision=found.values["work_copy_revision"], change=[
                {"op": "set", "kind": "field", "where": {"name": "Reference"}, "to": {"size": 0.5}},
                {"op": "set", "kind": "field", "where": {"name": "Value"}, "to": {"visible": False}}])
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            refs, values = result.values["change_report"]["set"]
            self.assertEqual(refs["matched"], found.values["count"])
            self.assertEqual(refs["already"] + refs["changed"], refs["matched"])
            self.assertEqual((list(refs["not_as_asked"]), refs["others_changed"]), ([], 0))
            after = board_index(next(Path(folder).rglob(checked["work_copy_id"] + "/project/board.kicad_pcb")).read_text())
            for o in select(after, "field", name="Reference"):
                self.assertEqual((o["size"], o["thickness"]), ([0.5, 0.5], strokes[o["id"]]))
            self.assertTrue(all(not o["visible"] for o in select(after, "field", name="Value")))
            # The revision she read is the one she changes: a stale one is refused.
            stale = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"],
                              work_copy_revision=found.values["work_copy_revision"], change=[
                {"op": "set", "kind": "field", "where": {"name": "Value"}, "to": {"visible": True}}])
            self.assertEqual(stale.failure["code"], "work_copy_changed")
            # Ambiguity and nothing-matches go back to her, before anything runs.
            now = result.values["work_copy_revision"]
            many = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], work_copy_revision=now, change=[
                {"op": "set", "kind": "field", "where": {"name": "Reference"}, "to": {"at": [1, 2]}}])
            self.assertIn("at places one object", many.failure["recovery"]["detail"])
            none = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], work_copy_revision=now, change=[
                {"op": "set", "kind": "via", "where": {"nets": ["NO_SUCH_NET"]}, "to": {"size": 0.6}}])
            self.assertIn("no via matches", none.failure["recovery"]["detail"])
            unsaid = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], work_copy_revision=now, change=[
                {"op": "set", "kind": "track", "where": {}, "to": {"width": 0.2}}])
            self.assertIn("all: true", unsaid.failure["recovery"]["detail"])

    def test_a_schematic_change_is_carried_out_and_verified_in_one_run(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            change = [
                {"op": "swap_symbol", "sheet": "board.kicad_sch", "ref": "R1", "lib_id": "Device:C", "value": "100n"},
                {"op": "wire", "sheet": "board.kicad_sch", "from": "R1.2", "to": "R2.1"},
                {"op": "label", "sheet": "board.kicad_sch", "pin": "R2.2", "net": "OUT"},
                {"op": "label", "sheet": "board.kicad_sch", "pin": "R3.1", "net": "OUT"},
                {"op": "remove_symbols", "sheet": "board.kicad_sch", "refs": ["R4"]},
                {"op": "no_connect", "sheet": "board.kicad_sch", "pins": ["R1.1"]},
                {"op": "tidy_fields", "sheet": "board.kicad_sch", "refs": ["R1", "R2"]},
            ]
            # Read-only, as the broker hands every argument over (15 Nov: the first live run failed here).
            from types import MappingProxyType
            frozen = tuple(MappingProxyType({key: tuple(value) if isinstance(value, list) else value
                                             for key, value in op.items()}) for op in change)
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=frozen)
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            report = result.values["change_report"]
            self.assertEqual(list(report["unmet"]), [])
            # Each operation in order, then the sheet tidied as a whole (16 Nov).
            self.assertEqual([item["op"] for item in report["change_result"]],
                             [op["op"] for op in change] + ["tidy_sheet"])
            self.assertEqual(result.values["steps"]["schematic"], "needs_approval")
            text = next(Path(folder).rglob(checked["work_copy_id"] + "/project/board.kicad_sch")).read_text()
            self.assertIn('(lib_id "Device:C")', text)
            self.assertNotIn('"R4"', text)

    def test_a_change_that_does_not_hold_is_undone_and_says_why(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            copy = next(Path(folder).rglob(checked["work_copy_id"] + "/project/board.kicad_sch"))
            before = copy.read_text()
            # Wired to R2, and declared unconnected: both cannot hold.
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=[
                {"op": "wire", "sheet": "board.kicad_sch", "from": "R1.2", "to": "R2.1"},
                {"op": "no_connect", "sheet": "board.kicad_sch", "pins": ["R1.2"]},
            ])
            self.assertEqual(result.failure["code"], "change_unmet")
            self.assertTrue(any("R1.2 is joined to R2.1" in item for item in result.failure["recovery"]["unmet"]))
            self.assertEqual(result.failure["recovery"]["rollback_status"], "restored")
            self.assertEqual(copy.read_text(), before)
            # The same change previewed says the same, and keeps nothing.
            seen = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], dry_run=True,
                             change=[{"op": "wire", "sheet": "board.kicad_sch", "from": "R1.2", "to": "R2.1"},
                                     {"op": "no_connect", "sheet": "board.kicad_sch", "pins": ["R1.2"]}])
            self.assertTrue(seen.values["change_report"]["unmet"])
            self.assertEqual(copy.read_text(), before)

    def test_an_unannotated_imported_ground_symbol_is_copied_and_holds(self):
        """15 Nov: P0009's imported GND symbols are all #PWR?; none could be found to copy."""
        import uuid as _uuid
        power = (APP_SYMBOLS / "power.kicad_sym").read_text()
        start = power.index('(symbol "GND"')
        depth = 0
        for end in range(start, len(power)):
            depth += {"(": 1, ")": -1}.get(power[end], 0)
            if depth == 0:
                break
        gnd = power[start:end + 1].replace('(symbol "GND"', '(symbol "power:GND"', 1)
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            text = (project / "board.kicad_sch").read_text()
            text = text.replace("\t(lib_symbols\n", "\t(lib_symbols\n\t\t" + gnd + "\n", 1)
            root = text.split('(uuid "', 1)[1].split('"', 1)[0]
            placed = ('\t(symbol\n\t\t(lib_id "power:GND")\n\t\t(at 200 150 0)\n\t\t(unit 1)\n'
                      '\t\t(uuid "%s")\n\t\t(property "Reference" "#PWR?"\n\t\t\t(at 200 155 0)\n\t\t)\n'
                      '\t\t(property "Value" "GND"\n\t\t\t(at 200 153 0)\n\t\t)\n'
                      '\t\t(pin "1"\n\t\t\t(uuid "%s")\n\t\t)\n\t\t(instances\n\t\t\t(project "t"\n'
                      '\t\t\t\t(path "/%s"\n\t\t\t\t\t(reference "#PWR?")\n\t\t\t\t\t(unit 1)\n'
                      '\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n' % (_uuid.uuid4(), _uuid.uuid4(), root))
            text = text.replace("\t(sheet_instances", placed + "\t(sheet_instances", 1)
            (project / "board.kicad_sch").write_text(text)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=[
                {"op": "power", "sheet": "board.kicad_sch", "pin": "R1.2", "port": "power:GND"},
                {"op": "power", "sheet": "board.kicad_sch", "pin": "R3.2", "port": "GND"}])
            self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
            self.assertEqual(list(result.values["change_report"]["unmet"]), [])

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
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=[
                {"op": "power", "sheet": "board.kicad_sch", "pin": "R3.2", "port": "GND"}])
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
            first = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, change=[
                {"op": "label", "sheet": "board.kicad_sch", "pin": "R1.1", "net": "OLD"},
                {"op": "label", "sheet": "board.kicad_sch", "pin": "R3.1", "net": "OLD"}])
            self.assertEqual(first.state, CapabilityResultState.SUCCEEDED, first.failure)
            labelled = copy.read_text()
            # Taking the labels away splits R1.1 from R3.1: that is what it is for.
            cleared = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work, change=[
                {"op": "remove_labels", "sheet": "board.kicad_sch", "nets": ["OLD"]},
                {"op": "remove_items", "sheet": "board.kicad_sch", "area": [195, 145, 205, 160],
                 "kinds": ["power"]},
                {"op": "remove_dangling", "sheet": "board.kicad_sch"}])
            self.assertEqual(cleared.state, CapabilityResultState.SUCCEEDED, cleared.failure)
            done = [item["removed"] for item in cleared.values["change_report"]["change_result"]
                    if item["op"] != "tidy_sheet"]
            self.assertEqual([dict(d) if not isinstance(d, int) else d for d in done],
                             [2, {"power": 1}, {"no_connect": 1, "wire": 1}])
            text = copy.read_text()
            self.assertNotIn('"OLD"', text)
            self.assertNotIn("(no_connect", text)
            self.assertNotIn("(wire", text)
            self.assertNotIn('(lib_id "power:GND")', text)
            # Back to the labelled copy, then to the start.
            back = self.call(runtime, "edit_kicad_work_copy", work_copy_id=work,
                             revert_to=first.values["work_copy_revision"])
            self.assertEqual(back.state, CapabilityResultState.SUCCEEDED, back.failure)
            self.assertEqual(back.values["work_copy_revision"], first.values["work_copy_revision"])
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
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=[
                {"op": "power", "sheet": "board.kicad_sch", "pin": "R1.1", "port": "GND"}])
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
        self.assertEqual(users, {"edit", "make_hierarchical", "take_edits", "_revert"})

if __name__ == "__main__":
    unittest.main()
