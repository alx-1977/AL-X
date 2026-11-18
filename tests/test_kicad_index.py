"""One reading of a board, and selection over it (18 Nov 2026)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.kicad_index import board_index, net_groups, netlist_model, netlist_nets, pad_nets, select  # noqa: E402

BOARD = '''(kicad_pcb
\t(net 0 "")
\t(footprint "Lib:R_0603"
\t\t(layer "F.Cu")
\t\t(uuid "fp-r1")
\t\t(at 10 20 90)
\t\t(property "Reference" "R1"
\t\t\t(at 0 1.4 90)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "ref-r1")
\t\t\t(effects (font (size 0.5 0.5) (thickness 0.12))))
\t\t(property "Value" "10k"
\t\t\t(at 0 -1.4 90)
\t\t\t(layer "F.Fab")
\t\t\t(hide yes)
\t\t\t(uuid "val-r1")
\t\t\t(effects (font (size 1 1) (thickness 0.15))))
\t\t(pad "1" smd roundrect (at -0.8 0 90) (size 0.9 0.95) (layers "F.Cu" "F.Mask") (net "GND") (uuid "pad-r1-1"))
\t\t(pad "2" smd roundrect (at 0.8 0 90) (size 0.9 0.95) (layers "F.Cu" "F.Mask") (net "VCC") (uuid "pad-r1-2")))
\t(footprint "Lib:C_0402"
\t\t(layer "B.Cu")
\t\t(uuid "fp-c1")
\t\t(at 30 40 0)
\t\t(property "Reference" "C1" (at 0 1 0) (layer "B.SilkS") (uuid "ref-c1")
\t\t\t(effects (font (size 1 1) (thickness 0.15))))
\t\t(property "Value" "100n" (at 0 -1 0) (layer "B.Fab") (uuid "val-c1"))
\t\t(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "B.Cu") (net "GND"))
\t\t(pad "2" smd rect (at 1 0) (size 0.5 0.5) (layers "B.Cu") (net "VCC")))
\t(segment (start 10 20) (end 13 24) (width 0.2) (layer "F.Cu") (net "GND") (uuid "seg-1"))
\t(via (at 12 22) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "GND") (uuid "via-1"))
\t(zone (net "GND") (layer "F.Cu") (uuid "zone-1") (name "pour"))
\t(zone (net "GND") (layer "F.Cu") (uuid "drop-1") (attr (teardrop (type padvia))))
\t(gr_text "Keepout" (at 50 60 0) (layer "F.SilkS") (uuid "text-1") (effects (font (size 1.26 1.26) (thickness 0.254))))
)'''


class BoardIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = board_index(BOARD)

    def test_every_object_has_its_own_address(self):
        ids = [o["id"] for o in self.index["objects"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("C1.1", ids)  # a pad without a uuid is REF.NUMBER
        kinds = {o["id"]: o["kind"] for o in self.index["objects"]}
        self.assertEqual((kinds["zone-1"], kinds["drop-1"], kinds["text-1"]), ("zone", "teardrop", "text"))

    def test_fields_carry_size_thickness_and_visibility(self):
        reference, value = (select(self.index, "field", refs=["R1"], name=name)[0] for name in ("Reference", "Value"))
        self.assertEqual((reference["size"], reference["thickness"], reference["visible"]), ([0.5, 0.5], 0.12, True))
        self.assertFalse(value["visible"])

    def test_select_filters_by_exact_values(self):
        self.assertEqual([o["id"] for o in select(self.index, "via", nets=["GND"])], ["via-1"])
        self.assertEqual([o["ref"] for o in select(self.index, "part", layer="B")], ["C1"])
        self.assertEqual([o["ref"] for o in select(self.index, "part", footprint="R_0603")], ["R1"])
        self.assertEqual(len(select(self.index, "field", name="Reference")), 2)
        self.assertEqual([o["ref"] for o in select(self.index, "field", name="Reference", area=[0, 0, 20, 30])], ["R1"])
        self.assertEqual([o["id"] for o in select(self.index, "track", area=[10, 20, 13, 24])], ["seg-1"])
        self.assertEqual(select(self.index, "text", reads="Keepout")[0]["thickness"], 0.254)
        with self.assertRaises(ValueError):
            select(self.index, "everything")

    def test_pad_nets_and_groups_come_from_the_index(self):
        self.assertEqual(pad_nets(self.index), {"R1.1": "GND", "R1.2": "VCC", "C1.1": "GND", "C1.2": "VCC"})
        self.assertEqual(net_groups(self.index), {frozenset({("R1", "1"), ("C1", "1")}),
                                                  frozenset({("R1", "2"), ("C1", "2")})})

    def test_the_netlist_is_read_once_its_one_way(self):
        model = netlist_model('(export (components (comp (ref "R1") (value "10k") (footprint "Lib:R")'
                              ' (fields (field (name "MPN") "RC0603")) (sheetpath (names "/A/") (tstamps "/a/"))'
                              ' (tstamps "r1"))) (nets (net (code "1") (name "GND")'
                              ' (node (ref "R1") (pin "1") (pinfunction "~")) (node (ref "C1") (pin "2")))))')
        self.assertEqual(model["components"][0]["fields"], {"MPN": "RC0603"})
        self.assertEqual(model["components"][0]["sheet"], "/A/")
        self.assertEqual(netlist_nets(model), {frozenset({("R1", "1"), ("C1", "2")}): "GND"})


if __name__ == "__main__":
    unittest.main()
