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
        self.assertEqual(plan["touched"], ["C37", "U6"])
        for wrong, said in (([], "1 to 200"), ([{"op": "draw"}], "op is one of"),
                            ([{"op": "wire", "sheet": "a", "from": "U6.9"}], "needs to"),
                            ([{"op": "wire", "sheet": "a", "from": "U6-9", "to": "C1.1"}], "REF.PIN"),
                            ([{"op": "track", "width": 0.3}], "points, or from and to")):
            with self.subTest(said=said), self.assertRaisesRegex(ChangeError, said):
                validate_change(wrong)

    def test_the_result_is_checked_against_the_change_and_nothing_else_moves(self):
        before = {"R1.1": frozenset({"R1.1", "R9.1"}), "R9.1": frozenset({"R1.1", "R9.1"}),
                  "R5.1": frozenset({"R5.1", "R6.1"}), "R6.1": frozenset({"R5.1", "R6.1"})}
        after = {"R1.1": frozenset({"R1.1"}), "R9.1": frozenset({"R9.1"}),
                 "R5.1": frozenset({"R5.1"}), "R6.1": frozenset({"R6.1"})}
        problems = unmet([{"op": "wire", "sheet": "a", "from": "R1.1", "to": "R9.1"}], before, after,
                         {}, {}, ["R1", "R9"])
        self.assertIn("R1.1 is not joined to R9.1", problems)
        # R5 and R6 were never named, and came apart: that is reported too.
        self.assertTrue(any(p.startswith("R5.1 changed connection") for p in problems))


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
            self.assertEqual([item["op"] for item in report["change_result"]],
                             [op["op"] for op in change])
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

    def test_a_missing_power_port_names_what_the_sheet_has(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime, project = self.runtime(folder)
            checked = self.call(runtime, "check_kicad_board", project_dir=str(project)).values
            result = self.call(runtime, "edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], change=[
                {"op": "power", "sheet": "board.kicad_sch", "pin": "R1.1", "port": "GND"}])
            self.assertEqual(result.failure["code"], "script_failed")
            self.assertIn("no GND port", result.failure["recovery"]["stderr"])


if __name__ == "__main__":
    unittest.main()
