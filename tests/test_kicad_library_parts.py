"""A new FireFli part from a chosen symbol and footprint (19 Nov 2026).

P0005's LED circuit needed a 74LVC1G123 that was not in the library, and the job
stopped. add_part copies the chosen symbol and footprint into a library work copy
under the part number, with FireFli's fields and a catalogue row.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.config.settings import KiCadSettings  # noqa: E402
from alx.providers.kicad import KiCadError, KiCadProvider, library_inventory  # noqa: E402
from alx.providers.kicad_library_parts import (  # noqa: E402
    part_name, renamed_footprint, renamed_symbol, with_catalogue_row,
)

KICAD = Path("/Applications/KiCad/KiCad.app/Contents")
CLI = KICAD / "MacOS/kicad-cli"
SYMBOL = '''(symbol "74LVC1G123"
\t\t(property "Reference" "U"
\t\t\t(at 0 0 0)
\t\t)
\t\t(property "Value" "74LVC1G123"
\t\t\t(at 0 0 0)
\t\t)
\t\t(symbol "74LVC1G123_1_1"
\t\t\t(pin input line
\t\t\t\t(at 0 0 0)
\t\t\t)
\t\t)
\t)'''
CATALOGUE = "Reference,Value,Footprint,Datasheet,Description,Manufacturer,MPN,LCSC,Note\n"


class LibraryPartTextTests(unittest.TestCase):
    def test_a_symbol_takes_the_part_number_and_its_fields(self) -> None:
        text = renamed_symbol(SYMBOL, "74LVC1G123", "SN74LVC1G123DQER", {
            "Reference": "U", "Value": "SN74LVC1G123DQER", "Footprint": "FireFli_ICs:SN74LVC1G123DQER",
            "Manufacturer": "Texas Instruments", "MPN": "SN74LVC1G123DQER", "LCSC": ""})
        self.assertIn('(symbol "SN74LVC1G123DQER"', text)
        self.assertIn('(symbol "SN74LVC1G123DQER_1_1"', text)
        self.assertIn('(property "Value" "SN74LVC1G123DQER"', text)
        self.assertIn('(property "Manufacturer" "Texas Instruments"', text)
        self.assertNotIn('(property "LCSC"', text)   # an empty field is not added
        self.assertNotIn('"74LVC1G123"', text)

    def test_names_footprints_and_catalogue_rows(self) -> None:
        self.assertEqual(part_name("PN5321A3HN/C106,55"), "PN5321A3HN_C106_55")
        footprint = renamed_footprint('(footprint "X2SON-8"\n\t(property "Value" "X2SON-8")\n)', "SN74")
        self.assertIn('(footprint "SN74"', footprint)
        self.assertIn('(property "Value" "SN74")', footprint)
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

    @unittest.skipUnless(CLI.is_file(), "KiCad is not installed")
    def test_a_part_is_added_from_kicads_own_symbol_and_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            root = self.library(folder)
            settings = KiCadSettings.from_environment({"ALX_KICAD_DESIGN_ROOTS": str(folder / "designs")},
                                                      folder / "storage")
            provider = KiCadProvider(settings, lambda exe, args: subprocess.run([exe, *args], capture_output=True,
                                                                                text=True))
            copy = provider.copy_library({"library_dir": str(root)})
            part = {"work_copy_id": copy["work_copy_id"], "library": "ICs", "mpn": "SN74LVC1G123DQER",
                    "manufacturer": "Texas Instruments", "description": "Monostable, USON-8",
                    "datasheet": "https://www.ti.com/lit/ds/symlink/sn74lvc1g123.pdf", "reference": "U",
                    "symbol_from": "74xGxx:74LVC1G123",
                    "footprint_from": "Package_SON/Texas_R-PUSON-N8_USON-8-1EP_1.6x2.1mm_P0.5mm_EP0.4x1.7mm"}
            added = provider.add_part(part)
            self.assertEqual(added["symbol"], "FireFli_ICs:SN74LVC1G123DQER")
            project = settings.work_root / copy["work_copy_id"] / "project"
            done = subprocess.run([str(CLI), "sym", "export", "svg", "-s", "SN74LVC1G123DQER", "-o",
                                   str(folder / "svg"), str(project / "symbols" / "FireFli_ICs.kicad_sym")],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((project / "footprints" / "FireFli_ICs.pretty" / "SN74LVC1G123DQER.kicad_mod").is_file())
            self.assertIn("SN74LVC1G123DQER", (project / "catalogue" / "FireFli_ICs.csv").read_text())
            with self.assertRaises(KiCadError):
                provider.add_part(part)


if __name__ == "__main__":
    unittest.main()
