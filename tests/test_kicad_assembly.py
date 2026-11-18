"""JLC assembly files from KiCad's own BOM and position exports (18 Nov 2026)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.kicad_assembly import correction, jlc_bom, jlc_positions  # noqa: E402

KICAD_BOM = ('"Comment","Designator","Footprint","LCSC Part #","Quantity"\n'
             '"1u","C2,C15","FireFli_Capacitors:C_0603_1608Metric","C92848","2"\n'
             '"S-82D1AAJ-A8T2U7","B1","FireFli_ICs:S-82D1AAJ-A8T2U7","","1"\n')
KICAD_POS = ('Ref,Val,Package,PosX,PosY,Rot,Side\n'
             '"J2","SM3","SM3",24.000000,39.790000,180.000000,top\n'
             '"SW4","TL","TL",105.000000,71.300000,90.000000,top\n'
             '"U4","MPQ","MPQ",50.250080,49.648820,-90.000000,top\n'
             '"R1","10k","R",84.800000,9.998200,-90.000000,top\n'
             '"Q9","x","x",10.000000,10.000000,0.000000,bottom\n')


class AssemblyTests(unittest.TestCase):
    def test_bom_in_jlc_columns_names_lines_without_lcsc(self):
        bom, missing = jlc_bom(KICAD_BOM)
        self.assertEqual(bom.splitlines(), ["Comment,Designator,Footprint,LCSC Part #",
                                            '1u,"C2,C15",C_0603_1608Metric,C92848',
                                            "S-82D1AAJ-A8T2U7,B1,S-82D1AAJ-A8T2U7,"])
        self.assertEqual(missing, ["B1"])

    def test_positions_match_the_last_p0009_order(self):
        """P0009's October order (Fabrication Toolkit): J2 59.4575 at 180, SW4 104.5, U4 0 degrees."""
        fixes = {"J2": (0.0, (0.0, -19.6675)), "SW4": (0.0, (0.0, 0.5)), "U4": (90.0, (0.0, 0.0)),
                 "Q9": (0.0, (1.0, 0.0))}
        cpl, report = jlc_positions(KICAD_POS, fixes)
        rows = cpl.splitlines()
        self.assertEqual(rows[0], "Designator,Mid X,Mid Y,Layer,Rotation")
        self.assertIn("J2,24.0000,59.4575,Top,180", rows)
        self.assertIn("SW4,104.5000,71.3000,Top,90", rows)
        self.assertIn("U4,50.2501,49.6488,Top,0", rows)
        self.assertIn("R1,84.8000,9.9982,Top,270", rows)
        self.assertIn("Q9,9.0000,10.0000,Bottom,0", rows)
        self.assertEqual(report, {"parts": 5, "corrected": ["J2", "SW4", "U4", "Q9"], "bottom": ["Q9"]})

    def test_a_correction_is_read_from_the_library_footprint(self):
        text = ('(footprint "X"\n\t(property "JLC Rotation Offset" "90"\n\t\t(at 0 0 0))\n'
                '\t(property "JLC Position Offset" "0,-19.6675"\n\t\t(at 0 0 0)))')
        self.assertEqual(correction(text), (90.0, (0.0, -19.6675)))
        self.assertIsNone(correction('(footprint "X" (property "Reference" "REF**"))'))
        with self.assertRaises(ValueError):
            correction('(footprint "X" (property "JLC Position Offset" "left"))')


if __name__ == "__main__":
    unittest.main()
