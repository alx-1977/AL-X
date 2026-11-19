"""A new FireFli part built from its datasheet (19 Nov 2026).

P0005's LED circuit needed a 74LVC1G123 that was not in the library; copied from KiCad's
library it came in two units and without its power pins, and was dropped. A part is now
built from the datasheet only: the symbol from the pin table, the footprint from the land
pattern. And a write-back refuses parts new to his project that are not library parts.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.config.settings import KiCadSettings  # noqa: E402
from alx.providers.kicad import KiCadError, KiCadProvider, _non_library_parts, library_inventory  # noqa: E402
from alx.providers.kicad_library_parts import part_name, with_catalogue_row  # noqa: E402
from alx.providers.coding_process import run_kicad_engine  # noqa: E402
from alx.providers.kicad_part_builder import PartSpecError, build_footprint, build_symbol  # noqa: E402

KICAD = Path("/Applications/KiCad/KiCad.app/Contents")
CLI = KICAD / "MacOS/kicad-cli"
CATALOGUE = "Reference,Value,Footprint,Datasheet,Description,Manufacturer,MPN,LCSC,Note\n"
PINS8 = [{"number": str(i), "name": name, "type": kind} for i, (name, kind) in enumerate(
    [("A", "input"), ("B", "input"), ("~{CLR}", "input"), ("GND", "power_in"), ("Q", "output"),
     ("CEXT", "passive"), ("RCEXT", "passive"), ("VCC", "power_in")], 1)]
DFN8 = {"package": "two_row", "pins": 8, "pitch": 0.5, "pad_width": 0.25, "pad_length": 0.7, "row_spacing": 2.9,
        "body": [2.0, 3.0], "height": 0.5, "exposed_pad": {"width": 0.9, "height": 2.3, "number": "9"}}


def pads(footprint: str) -> dict[str, tuple[float, float, float, float]]:
    found = {}
    for number, x, y, w, h in re.findall(
            r'\(pad "([^"]*)" \w+ \w+\n\t\t\(at ([-\d.]+) ([-\d.]+)[^)]*\)\n\t\t\(size ([\d.]+) ([\d.]+)\)', footprint):
        found[number] = (float(x), float(y), float(w), float(h))
    return found


class BuilderTests(unittest.TestCase):
    def test_the_symbol_is_one_unit_in_package_order(self) -> None:
        symbol = build_symbol("SN74", {"Reference": "U", "Value": "SN74", "MPN": "SN74"}, PINS8)
        self.assertNotIn('"SN74_2_1"', symbol)                     # one unit, power pins included
        at = {n: (float(x), float(y)) for x, y, n in re.findall(
            r'\(pin \w+ line\n\t\t\t\t\(at ([-\d.]+) ([-\d.]+) \d+\).*?\(number "([^"]+)"', symbol, re.S)}
        self.assertEqual(sorted(at, key=int), [str(i) for i in range(1, 9)])
        self.assertTrue(all(at[str(i)][0] < 0 for i in range(1, 5)))      # 1-4 down the left
        self.assertTrue(all(at[str(i)][0] > 0 for i in range(5, 9)))      # 5-8 up the right
        self.assertGreater(at["1"][1], at["4"][1])
        self.assertGreater(at["8"][1], at["5"][1])
        self.assertIn('(property "MPN" "SN74"', symbol)

    def test_a_two_row_footprint_numbers_counter_clockwise_with_its_exposed_pad(self) -> None:
        footprint, summary = build_footprint("SN74", DFN8, {})
        found = pads(footprint)
        self.assertEqual(len(found), 9)
        self.assertEqual(found["1"][:2], (-1.45, -0.75))   # top of the left row
        self.assertEqual(found["4"][:2], (-1.45, 0.75))
        self.assertEqual(found["5"][:2], (1.45, 0.75))     # bottom of the right row
        self.assertEqual(found["8"][:2], (1.45, -0.75))
        self.assertEqual(found["9"], (0.0, 0.0, 0.9, 2.3))
        self.assertEqual(summary["courtyard"], [-2.05, -1.75, 2.05, 1.75])
        self.assertIn('"F.CrtYd"', footprint)
        self.assertIn("(fp_circle", footprint)             # pin 1 mark

    def test_a_quad_footprint_goes_round_all_four_sides(self) -> None:
        footprint, _ = build_footprint("QFN48", {"package": "quad", "pins": 48, "pitch": 0.5, "pad_width": 0.25,
                                                 "pad_length": 0.8, "row_spacing": 6.9, "body": [7, 7]}, {})
        found = pads(footprint)
        self.assertEqual(len(found), 48)
        self.assertEqual(found["1"][:2], (-3.45, -2.75))   # left side, top
        self.assertEqual(found["13"][:2], (-2.75, 3.45))   # bottom side, left
        self.assertEqual(found["25"][:2], (3.45, 2.75))    # right side, bottom
        self.assertEqual(found["37"][:2], (2.75, -3.45))   # top side, right

    def test_figures_that_draw_nothing_are_refused_with_the_reason(self) -> None:
        with self.assertRaises(PartSpecError):
            build_footprint("X", dict(DFN8, pins=7), {})
        with self.assertRaises(PartSpecError):
            build_footprint("X", dict(DFN8, package="dip"), {})
        with self.assertRaises(PartSpecError):
            build_symbol("X", {}, [{"number": "1", "name": "A", "type": "wire"}])
        with self.assertRaises(PartSpecError):
            build_symbol("X", {}, [{"number": "1", "name": "A", "type": "input"},
                                   {"number": "1", "name": "B", "type": "input"}])

    def test_names_and_catalogue_rows(self) -> None:
        self.assertEqual(part_name("PN5321A3HN/C106,55"), "PN5321A3HN_C106_55")
        rows = with_catalogue_row(CATALOGUE, {"MPN": "SN74", "Reference": "U", "Extra": "ignored"})
        self.assertEqual(rows.splitlines()[1], "U,,,,,,SN74,,")


class LibraryCopyTests(unittest.TestCase):
    def library(self, folder: Path) -> Path:
        root = folder / "designs" / "FireFli-KiCad-Lib"
        (root / "symbols").mkdir(parents=True)
        (root / "footprints" / "FireFli_ICs.pretty").mkdir(parents=True)
        (root / "catalogue").mkdir()
        (root / "symbols" / "FireFli_ICs.kicad_sym").write_text(
            '(kicad_symbol_lib\n\t(version 20231120)\n\t(generator "firefli_lib")\n)\n')
        (root / "catalogue" / "FireFli_ICs.csv").write_text(CATALOGUE)
        return root

    def test_the_catalogue_is_part_of_a_library_copy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = self.library(Path(folder))
            self.assertIn("catalogue/FireFli_ICs.csv", library_inventory(root))

    def test_parts_that_are_not_library_parts_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            sheet = ('(kicad_sch\n\t(symbol\n\t\t(lib_id "Device:D_Schottky")\n\t\t(property "Reference" "D14")\n\t)\n'
                     '\t(symbol\n\t\t(lib_id "FireFli_ICs:LM393A-DFGR")\n\t\t(property "Reference" "IC4")\n\t)\n'
                     '\t(symbol\n\t\t(lib_id "power:GND")\n\t\t(property "Reference" "#PWR01")\n\t)\n)\n')
            (Path(folder) / "a.kicad_sch").write_text(sheet)
            self.assertEqual(_non_library_parts(folder), [("D14", "Device:D_Schottky")])

    @unittest.skipUnless(CLI.is_file(), "KiCad is not installed")
    def test_a_part_is_built_from_the_datasheet_and_kicad_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            root = self.library(folder)
            settings = KiCadSettings.from_environment({"ALX_KICAD_DESIGN_ROOTS": str(folder / "designs")},
                                                      folder / "storage")
            provider = KiCadProvider(settings, lambda exe, args: subprocess.run([exe, *args], capture_output=True,
                                                                                text=True))
            copy = provider.copy_library({"library_dir": str(root)})
            part = {"work_copy_id": copy["work_copy_id"], "library": "ICs", "mpn": "SN74LVC1G123DQER",
                    "manufacturer": "Texas Instruments", "description": "Monostable, DFN-8",
                    "datasheet": "https://www.ti.com/lit/ds/symlink/sn74lvc1g123.pdf", "reference": "U",
                    "pins": PINS8 + [{"number": "9", "name": "EP", "type": "passive", "side": "bottom"}],
                    "footprint": DFN8, "spice_none": "TI publishes IBIS only for this logic family"}
            added = provider.add_part(part)
            self.assertEqual(added["symbol"], "FireFli_ICs:SN74LVC1G123DQER")
            self.assertEqual(added["built"]["pads"], 9)
            project = settings.work_root / copy["work_copy_id"] / "project"
            for argv in (["sym", "export", "svg", "-s", "SN74LVC1G123DQER", "-o", str(folder / "svg"),
                          str(project / "symbols" / "FireFli_ICs.kicad_sym")],
                         ["fp", "export", "svg", "--fp", "SN74LVC1G123DQER", "-o", str(folder / "svg"),
                          str(project / "footprints" / "FireFli_ICs.pretty")]):
                done = subprocess.run([str(CLI), *argv], capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
            catalogue = (project / "catalogue" / "FireFli_ICs.csv").read_text()
            self.assertIn("SN74LVC1G123DQER", catalogue)
            self.assertIn("no SPICE model from the maker", catalogue)
            # The generated body is a file in the library, named by the footprint (Fusion needs it).
            self.assertTrue((project / "3dmodels" / "FireFli_ICs.3dshapes" / "SN74LVC1G123DQER.step").is_file())
            footprint = (project / "footprints" / "FireFli_ICs.pretty" / "SN74LVC1G123DQER.kicad_mod").read_text()
            self.assertIn('${FIREFLI_KICAD_LIB}/3dmodels/FireFli_ICs.3dshapes/SN74LVC1G123DQER.step', footprint)
            self.assertIn("3dmodels/FireFli_ICs.3dshapes/SN74LVC1G123DQER.step", library_inventory(project))
            with self.assertRaises(KiCadError):
                provider.add_part(part)
            # Without its SPICE model or a reason there is none, a part is not added.
            with self.assertRaises(KiCadError) as refused:
                provider.add_part({k: v for k, v in dict(part, mpn="OTHER").items() if k != "spice_none"})
            self.assertIn("spice", refused.exception.recovery["detail"])

    @unittest.skipUnless(CLI.is_file() and Path("/opt/homebrew/bin/ngspice").is_file(), "KiCad or ngspice missing")
    def test_a_pspice_dialect_model_from_the_inbox_is_accepted(self) -> None:
        # A Diodes Inc 2N7002 model: BSIM3 written LEVEL=7, which ngspice loads only in KiCad's
        # PSpice/LTspice compatibility (19 Nov 2026 it failed the check with no reason given).
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            root = self.library(folder)
            inbox = folder / "inbox"
            inbox.mkdir()
            (inbox / "2N7002 (SPICE MODEL).LIB").write_text(
                ".SUBCKT F2N7002 2 1 3\nRg 1 6 10\nM1 2 6 3 3 Bsim3 W=0.033 L=2.0e-6\n"
                ".MODEL Bsim3 NMOS (LEVEL=7 VERSION=3.1 TOX=1000e-10 VTH0=1.937 U0=700)\n.ENDS\n")
            settings = KiCadSettings.from_environment({"ALX_KICAD_DESIGN_ROOTS": str(folder / "designs")},
                                                      folder / "storage")
            provider = KiCadProvider(settings, lambda exe, args: subprocess.run([exe, *args], capture_output=True,
                                                                                text=True))
            provider.parts_inbox = inbox
            provider.engine_paths = {"ngspice": Path("/opt/homebrew/bin/ngspice")}
            provider.execute_engine = run_kicad_engine
            provider._firefli_library = lambda: root
            copy = provider.copy_library({"library_dir": "${FIREFLI_KICAD_LIB}"})
            added = provider.add_part({
                "work_copy_id": copy["work_copy_id"], "library": "ICs", "mpn": "2N7002", "manufacturer": "Diodes",
                "description": "N-MOSFET", "datasheet": "https://example.test/2n7002.pdf", "reference": "Q",
                "pins": [{"number": "1", "name": "G", "type": "input"}, {"number": "2", "name": "S", "type": "passive"},
                         {"number": "3", "name": "D", "type": "passive"}],
                "footprint": {"package": "pads", "body": [1, 0.6], "height": 0.37, "pads": [
                    {"number": "1", "x": -0.35, "y": -0.225, "width": 0.4, "height": 0.25},
                    {"number": "2", "x": -0.35, "y": 0.225, "width": 0.4, "height": 0.25},
                    {"number": "3", "x": 0.35, "y": 0, "width": 0.4, "height": 0.7}]},
                "spice": {"file": "2N7002 (SPICE MODEL).LIB", "model": "F2N7002", "pins": {"1": "1", "2": "3", "3": "2"}}})
            self.assertEqual(added["symbol"], "FireFli_ICs:2N7002")
            # Without a folder, the copy is of the FireFli library.
            self.assertTrue(provider.copy_library({})["work_copy_id"])


if __name__ == "__main__":
    unittest.main()
