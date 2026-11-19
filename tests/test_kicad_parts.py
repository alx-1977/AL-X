"""Finding parts in the FireFli library by what they are (19 Nov 2026)."""

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.providers.kicad_parts import find_parts, value_of  # noqa: E402

CAPACITORS = """Reference,Value,Footprint,Datasheet,Description,Manufacturer,MPN,Capacitance,Tolerance,Voltage,Dielectric,Temperature,Thickness,Package,LCSC,JLC library
C,1u,FireFli_Capacitors:C_0603_1608Metric,,"Multilayer ceramic capacitor, 1 µF, 0603",Taiyo Yuden,UMK107AB7105KA-T,1 µF,,50 V,X7R,,,0603 (1608 metric),C105174,Extended
C,1u,FireFli_Capacitors:C_0402_1005Metric,,"Multilayer ceramic capacitor, 1 µF, 0402",YAGEO,CC0402KRX5R6BB105,1 µF,,10 V,X5R,,,0402 (1005 metric),C52923,Basic
C,100n,FireFli_Capacitors:C_0603_1608Metric,,"Multilayer ceramic capacitor, 100 nF, 0603",Taiyo Yuden,UMK107B7104KAHT,100 nF,,50 V,X7R,,,0603 (1608 metric),C386155,Extended
"""
ICS = """Reference,Value,Footprint,Datasheet,Description,Manufacturer,MPN,LCSC,Note
IC,LM393A-DFGR,FireFli_ICs:LM393A-DFGR,,"Dual comparator, DFN-8 2x2 mm",3PEAK,LM393A-DFGR,C22401890,
IC,"PN5321A3HN/C106,55",FireFli_ICs:PN5321A3HN_C106_55,,"NFC controller, HVQFN-40",NXP,"PN5321A3HN/C106,55",C1,
"""


class FindPartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "catalogue").mkdir()
        (self.root / "catalogue" / "FireFli_Capacitors.csv").write_text(CAPACITORS, encoding="utf-8")
        (self.root / "catalogue" / "FireFli_ICs.csv").write_text(ICS, encoding="utf-8")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_values_compare_as_numbers(self) -> None:
        self.assertEqual(value_of("1 µF"), value_of("1u"))
        self.assertEqual(value_of("4k7"), 4700.0)
        self.assertIsNone(value_of("74LVC1G123"))

    def test_value_and_package_give_the_exact_names(self) -> None:
        found = find_parts(self.root, value="1uF", package="0603")
        self.assertEqual([p["symbol"] for p in found["parts"]], ["FireFli_Capacitors:UMK107AB7105KA-T"])
        self.assertEqual(found["parts"][0]["lcsc"], "C105174")
        self.assertEqual(found["parts"][0]["footprint"], "FireFli_Capacitors:C_0603_1608Metric")

    def test_words_kind_and_package_names(self) -> None:
        found = find_parts(self.root, terms="comparator")
        self.assertEqual(found["parts"][0]["package"], "DFN-8 2x2 mm")
        self.assertEqual(find_parts(self.root, terms="NFC")["parts"][0]["symbol"], "FireFli_ICs:PN5321A3HN_C106_55")
        self.assertEqual(find_parts(self.root, kind="C", value="100n")["count"], 1)
        self.assertEqual(find_parts(self.root, terms="74LVC1G123")["count"], 0)

    def test_an_unreadable_value_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            find_parts(self.root, value="lots")


if __name__ == "__main__":
    unittest.main()
