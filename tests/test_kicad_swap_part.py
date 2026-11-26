"""Package and value swaps in one call per step, and capacitors from JLC through the generator (26 Nov 2026).

The real-KiCad test needs KiCad and the FireFli library; it is skipped where either is missing (CI).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from alx.providers.kicad_passives import (decode, generator_entry, jlc_candidates, jlc_listing,  # noqa: E402
                                          with_entry, with_lcsc_row)
from alx.providers.kicad_swap import (board_fields, board_script, expand_refs, library_fields,  # noqa: E402
                                      placed_symbols, schematic_refs)

GENERATOR = '''PARTS = (
    # Taiyo Yuden X5R / X7R
    taiyo("UMK107B7104KAHT", "0603", "100n", "100 nF", "±10%", "50 V", "X7R", "0.8 mm"),
    # YAGEO C0G
    yageo("CC0603JRNPO9BN220", "C105620", "0603", "22p", "22 pF", "±5%", "0.8 mm"),
)

LCSC = lcsc()
'''


class PartNumbers(unittest.TestCase):
    def test_the_makers_numbering_is_read(self):
        self.assertEqual({k: v for k, v in decode("UMK105B7104KV-FR").items() if k != "mpn"},
                         {"manufacturer": "Taiyo Yuden", "size": "0402", "pf": 100000, "capacitance": "100 nF",
                          "value": "100n", "tolerance": "±10%", "volts": 50.0, "voltage": "50 V",
                          "dielectric": "X7R", "thickness": "0.5 mm"})
        self.assertEqual(decode("TMK107BBJ106MA-T")["dielectric"], "X5R")
        self.assertEqual(decode("TMK107BBJ106MA-T")["capacitance"], "10 µF")
        part = decode("CC0402BRNPO9BN1R8")
        self.assertEqual((part["capacitance"], part["value"], part["tolerance"], part["dielectric"]),
                         ("1.8 pF", "1.8p", "±0.1 pF", "C0G"))
        self.assertEqual(decode("CC0402CRNPO9BNR50")["capacitance"], "0.5 pF")

    def test_parts_outside_his_brand_rule_are_not_offered(self):
        # Taiyo Yuden C0G (YAGEO for C0G), YAGEO X7R, a size the generator has no footprint for.
        for mpn in ("UMK105CG4R7CV-F", "CC0402KRX7R9BB104", "TMK316B7105KL-T", "GRM155R71C104KA88D"):
            self.assertIsNone(decode(mpn), mpn)

    def test_the_generator_gets_the_part_where_its_maker_is_listed(self):
        taiyo = decode("UMK105B7104KV-FR")
        source = with_entry(GENERATOR, taiyo, "C2167491")
        self.assertIn('    taiyo(\'UMK105B7104KV-FR\', \'0402\', \'100n\', \'100 nF\', \'±10%\', \'50 V\', \'X7R\', '
                      '\'0.5 mm\'),\n    # YAGEO C0G\n', source)
        yageo = decode("CC0402JRNPO9BN101")
        source = with_entry(source, yageo, "C106200")
        self.assertIn("'C106200'", source.split("# YAGEO C0G")[1])
        self.assertTrue(source.endswith(")\n\nLCSC = lcsc()\n"))
        with self.assertRaises(ValueError):
            with_entry(source, taiyo, "C2167491")
        # Not 50 V: YAGEO's helper assumes 50 V, so the part is written out in full.
        self.assertIn('"C0G"', generator_entry(decode("CC0603JRNPO0BN102"), "C1"))
        self.assertIn('100 V', generator_entry(decode("CC0603JRNPO0BN102"), "C1"))

    def test_the_lcsc_table_gets_one_row(self):
        table = "MPN,LCSC,JLC library,Note\nA,C1,Basic,\n"
        self.assertEqual(with_lcsc_row(table, "B", "C2", "Extended"),
                         "MPN,LCSC,JLC library,Note\nA,C1,Basic,\nB,C2,Extended,\n")
        with self.assertRaises(ValueError):
            with_lcsc_row(table, "A", "C1", "Basic")


class JlcCatalogue(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cache = Path(self.folder.name) / "jlc-parts.sqlite3"
        connection = sqlite3.connect(self.cache)
        connection.execute("CREATE TABLE parts (lcsc TEXT PRIMARY KEY, mpn TEXT, manufacturer TEXT, library TEXT, "
                           "package TEXT, stock INTEGER)")
        connection.execute("CREATE INDEX by_mpn ON parts (mpn)")
        connection.executemany("INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?)", [
            ("C1", "EMK105BJ105KV-F", "Taiyo Yuden", "expand", "0402", 343020),
            ("C2", "JMK105BJ105KV-F", "Taiyo Yuden", "expand", "0402", 192433),
            ("C3", "UMK105B7105KV-F", "TAIYO YUDEN", "base", "0402", 5000),
            ("C4", "LMK105BJ105KVBF", "Taiyo Yuden", "expand", "0402", 13),
            ("C5", "LMK107BJ105KA-T", "Taiyo Yuden", "expand", "0603", 90000),
            ("C6", "GRM155R61A105KE15D", "Murata", "base", "0402", 9000000),
            ("C7", "TMK105BJ105MV-F", "Taiyo Yuden", "expand", "0402", 0),
            ("C8", "UMK105B7105KV-F", "Taiyo Yuden", "expand", "0402", 0),
        ])
        connection.commit()
        connection.close()

    def tearDown(self):
        self.folder.cleanup()

    def test_stocked_parts_of_the_value_and_size_under_his_brand_rule(self):
        found = jlc_candidates(self.cache, 1e6, "0402")
        self.assertEqual([p["mpn"] for p in found], ["UMK105B7105KV-F", "JMK105BJ105KV-F", "EMK105BJ105KV-F"])
        self.assertEqual((found[0]["lcsc"], found[0]["jlc_library"], found[0]["stock"]), ("C3", "Basic", 5000))

    def test_a_listing_with_a_handful_left_shows_only_when_nothing_else_does(self):
        connection = sqlite3.connect(self.cache)
        connection.execute("DELETE FROM parts WHERE stock >= 1000")
        connection.commit()
        connection.close()
        self.assertEqual([p["mpn"] for p in jlc_candidates(self.cache, 1e6, "0402")], ["LMK105BJ105KVBF"])

    def test_one_listing_of_the_exact_part(self):
        self.assertEqual(jlc_listing(self.cache, "UMK105B7105KV-F", "Taiyo Yuden"),
                         {"lcsc": "C3", "jlc_library": "Basic", "stock": 5000})
        self.assertIsNone(jlc_listing(self.cache, "NOPE", "Taiyo Yuden"))


class SwapPieces(unittest.TestCase):
    def test_ranges_are_spelt_out(self):
        self.assertEqual(expand_refs(["C94-C96", "R3", "C95"]), ["C94", "C95", "C96", "R3"])
        self.assertEqual(expand_refs(["C1-3"]), ["C1", "C2", "C3"])
        for bad in (["C9-C2"], ["C1-C900"], ["x y"], "C1"):
            with self.assertRaises(ValueError, msg=bad):
                expand_refs(bad)

    def test_the_footprint_takes_the_parts_own_fields(self):
        block = ('(symbol "FireFli_Capacitors:X" (property "Reference" "C") (property "Value" "100n")'
                 ' (property "Footprint" "FireFli_Capacitors:C_0402_1005Metric") (property "MPN" "X")'
                 ' (property "Description" "a \\"quoted\\" cap") (property "ki_keywords" "C")'
                 ' (property "Value" "C"))')
        fields = library_fields(block)
        self.assertEqual(fields["Value"], "100n")
        self.assertEqual(fields["Description"], 'a "quoted" cap')
        self.assertEqual(board_fields(fields), {"Value": "100n", "MPN": "X", "Description": 'a "quoted" cap'})

    def test_every_instance_reference_is_found(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "a.kicad_sch").write_text(
                '(kicad_sch\n\t(symbol\n\t\t(lib_id "Device:C")\n\t\t(property "Reference" "C1")\n'
                '\t\t(instances (project "p" (path "/x" (reference "C1")) (path "/y" (reference "C7"))))\n\t)\n'
                '\t(symbol\n\t\t(lib_id "power:GND")\n\t\t(property "Reference" "#PWR01")\n\t)\n)\n')
            shared = ("Device:C", "C1", ("C1", "C7"))
            placed = placed_symbols(Path(folder))
            self.assertEqual(placed, {"C1": shared, "C7": shared})
            # One symbol serves both uses of the sheet: C7 alone cannot change, both together can.
            with self.assertRaisesRegex(ValueError, "also changes C1"):
                schematic_refs(placed, ["C7"])
            self.assertEqual(schematic_refs(placed, ["C7", "C1"]), ["C1"])

    def test_a_part_that_keeps_its_footprint_takes_only_the_fields(self):
        script = board_script(["R1", "R2"], "Lib:F", {"Value": "1k"}, keep=("R2",))
        self.assertIn("if ref not in ['R2']:", script)
        self.assertIn("set_field(board, ref, name, value)", script)


FIREFLI = Path("/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib")
RESISTOR = "FireFli_Resistors:RC0603FR-0710KL"


def _firefli_configured():
    try:
        from test_kicad_schematic_helpers import CLI, DEVICE, PYTHON
    except Exception:  # noqa: BLE001 - no KiCad helpers, no real test
        return False
    settings = Path.home() / "Library/Preferences/kicad/10.0"
    return (PYTHON.is_file() and CLI.is_file() and DEVICE.is_file() and (settings / "sym-lib-table").is_file()
            and "FireFli_Resistors" in (settings / "sym-lib-table").read_text()
            and (FIREFLI / "catalogue/FireFli_Resistors.csv").is_file())


BOARD_MAKER = '''
import pcbnew, sys
board = pcbnew.BOARD()
library = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/Resistor_SMD.pretty"
for number, x in ((1, 101.6), (2, 127.0), (3, 152.4), (4, 177.8)):
    part = pcbnew.FootprintLoad(library, "R_0805_2012Metric")
    part.SetFPID(pcbnew.LIB_ID("Resistor_SMD", "R_0805_2012Metric"))
    part.SetReference("R%d" % number)
    part.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(101.6)))
    board.Add(part)
board.Save(sys.argv[1])
'''


@unittest.skipUnless(_firefli_configured(), "KiCad or the FireFli library is not installed")
class RealSwap(unittest.TestCase):
    def test_schematic_then_board_one_call_each(self):
        from alx.bootstrap.kicad import build_kicad_runtime
        from alx.contracts import CapabilityResultState
        from kicad_actions import kicad_call
        from test_kicad_schematic_helpers import PYTHON, schematic
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            project = root / "designs" / "board"
            project.mkdir(parents=True)
            maker = root / "make_board.py"
            maker.write_text(BOARD_MAKER)
            made = subprocess.run([str(PYTHON), str(maker), str(project / "board.kicad_pcb")],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(made.returncode, 0, made.stderr[-2000:])
            (project / "board.kicad_pro").write_text(json.dumps({"meta": {"filename": "board.kicad_pro", "version": 1}}))
            (project / "board.kicad_sch").write_text(schematic())
            runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                          root / "storage")

            def call(action, **arguments):
                return kicad_call(runtime.executors, action, arguments)
            work = call("check", project_dir=str(project)).values["work_copy_id"]
            first = call("swap_part", work_copy_id=work, refs=["R1-R2"], part=RESISTOR)
            self.assertEqual(first.state, CapabilityResultState.SUCCEEDED, first.failure)
            self.assertEqual((first.values["side"], list(first.values["swapped"])), ("schematic", ["R1", "R2"]))
            copy = runtime.provider._load(work)[0] / "project"
            self.assertEqual(placed_symbols(copy)["R1"][0], RESISTOR)
            self.assertEqual(placed_symbols(copy)["R3"][0], "Device:R")
            early = call("swap_part", work_copy_id=work, refs=["R1-R2"], part=RESISTOR)
            self.assertEqual(early.failure["code"], "step_not_approved", early.failure)
            # He approves what he was shown (17 Nov): the copy is opened for him first.
            provider = runtime.provider
            provider.render_folder = root / "renders"
            provider.viewer_settle_seconds = 0
            provider.open_viewer = lambda what, path, index: type("V", (), {"terminate": lambda self: None})()
            provider.capture_screen = lambda target: Path(target).write_bytes(b"png")
            call("open_in_kicad", work_copy_id=work, what="schematic")
            approved = call("approve_step", work_copy_id=work, step="schematic",
                            work_copy_revision=first.values["work_copy_revision"])
            self.assertEqual(approved.state, CapabilityResultState.SUCCEEDED, approved.failure)
            second = call("swap_part", work_copy_id=work, refs=["R1-R2"], part=RESISTOR)
            self.assertEqual(second.state, CapabilityResultState.SUCCEEDED, second.failure)
            self.assertEqual((second.values["side"], list(second.values["swapped"])), ("board", ["R1", "R2"]))
            board = (copy / "board.kicad_pcb").read_text()
            self.assertEqual(board.count('(footprint "FireFli_Resistors:R_0603_1608Metric"'), 2)
            self.assertEqual(board.count('(footprint "Resistor_SMD:R_0805_2012Metric"'), 2)
            self.assertIn('"RC0603FR-0710KL"', board)
            done = call("swap_part", work_copy_id=work, refs=["R1-R2"], part=RESISTOR)
            self.assertEqual(done.values["side"], "none", done.failure)
            # A value change on the same footprint: the board keeps the pads and takes the new fields.
            other = "FireFli_Resistors:RC0603FR-07100KL"
            value = call("swap_part", work_copy_id=work, refs=["R1"], part=other)
            self.assertEqual(value.values["side"], "schematic", value.failure)
            call("open_in_kicad", work_copy_id=work, what="schematic")
            again = call("approve_step", work_copy_id=work, step="schematic",
                         work_copy_revision=value.values["work_copy_revision"])
            self.assertEqual(again.state, CapabilityResultState.SUCCEEDED, again.failure)
            fields = call("swap_part", work_copy_id=work, refs=["R1"], part=other)
            self.assertEqual((fields.values["side"], list(fields.values["swapped"])), ("board", ["R1"]), fields.failure)
            board = (copy / "board.kicad_pcb").read_text()
            self.assertIn('"RC0603FR-07100KL"', board)
            self.assertEqual(board.count('(footprint "FireFli_Resistors:R_0603_1608Metric"'), 2)
            self.assertEqual(call("swap_part", work_copy_id=work, refs=["R1"], part=other).values["side"], "none")


@unittest.skipUnless(_firefli_configured(), "KiCad or the FireFli library is not installed")
class RealAddPassive(unittest.TestCase):
    def test_a_jlc_capacitor_goes_in_through_the_generator(self):
        import shutil
        from alx.bootstrap.kicad import build_kicad_runtime
        from alx.contracts import CapabilityResultState
        from kicad_actions import kicad_call
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            library = root / "designs" / "FireFli-KiCad-Lib"
            for name in ("generator/capacitors.py", "generator/common.py", "generator/lcsc.csv",
                         "symbols/FireFli_Capacitors.kicad_sym", "catalogue/FireFli_Capacitors.csv"):
                (library / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(FIREFLI / name, library / name)
            shutil.copytree(FIREFLI / "footprints/FireFli_Capacitors.pretty",
                            library / "footprints/FireFli_Capacitors.pretty")
            (library / "cache").mkdir()
            connection = sqlite3.connect(library / "cache/jlc-parts.sqlite3")
            connection.execute("CREATE TABLE parts (lcsc TEXT PRIMARY KEY, mpn TEXT, manufacturer TEXT, "
                               "library TEXT, package TEXT, stock INTEGER)")
            connection.execute("INSERT INTO parts VALUES ('C9999', 'UMK105B7105KV-F', 'Taiyo Yuden', 'expand', "
                               "'0402', 5000)")
            connection.commit()
            connection.close()
            runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                          root / "storage")
            runtime.provider._firefli_library = lambda: library

            def call(action, **arguments):
                return kicad_call(runtime.executors, action, arguments)
            offered = call("find_part", value="1u", package="0402", kind="C")
            self.assertEqual(offered.values["count"], 0, offered.failure)
            self.assertEqual([p["mpn"] for p in offered.values["jlc_candidates"]], ["UMK105B7105KV-F"])
            copied = call("copy_library", library_dir=str(library))
            self.assertEqual(copied.state, CapabilityResultState.SUCCEEDED, copied.failure)
            work = copied.values["work_copy_id"]
            added = call("add_passive", work_copy_id=work, mpn="UMK105B7105KV-F")
            self.assertEqual(added.state, CapabilityResultState.SUCCEEDED, added.failure)
            self.assertEqual(added.values["symbol"], "FireFli_Capacitors:UMK105B7105KV-F")
            self.assertEqual(added.values["footprint"], "FireFli_Capacitors:C_0402_1005Metric")
            copy = runtime.provider._load(work)[0] / "project"
            self.assertIn('(symbol "UMK105B7105KV-F"', (copy / "symbols/FireFli_Capacitors.kicad_sym").read_text())
            self.assertIn("UMK105B7105KV-F,C9999,Extended,", (copy / "generator/lcsc.csv").read_text())
            self.assertIn("C9999", (copy / "catalogue/FireFli_Capacitors.csv").read_text())
            self.assertIn("taiyo('UMK105B7105KV-F'", (copy / "generator/capacitors.py").read_text())
            # The original library is untouched until write_back.
            self.assertNotIn("UMK105B7105KV-F", (library / "generator/capacitors.py").read_text())
            again = call("add_passive", work_copy_id=work, mpn="UMK105B7105KV-F")
            self.assertEqual(again.failure["code"], "arguments_unusable", again.failure)


if __name__ == "__main__":
    unittest.main()
