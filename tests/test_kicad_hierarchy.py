"""Separate top-level sheets joined into one hierarchical design (2026-11-13)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.kicad_hierarchy import make_hierarchical, netlist_nets  # noqa: E402


def sheet(sheet_uuid: str, symbol_uuid: str, reference: str) -> str:
    return f'''(kicad_sch
	(version 20260306)
	(uuid "{sheet_uuid}-file")
	(title_block
		(title "Scanner")
	)
	(lib_symbols)
	(symbol
		(lib_id "Device:R")
		(at 10 10 0)
		(uuid "{symbol_uuid}")
		(instances
			(project "scanner"
				(path "/{sheet_uuid}"
					(reference "{reference}")
					(unit 1)
				)
			)
		)
	)
	(sheet_instances
		(path "/"
			(page "1")
		)
	)
)
'''


def footprint(reference: str, symbol_uuid: str, sheet_name: str, sheet_file: str) -> str:
    return f'''	(footprint "R_0402"
		(layer "F.Cu")
		(property "Reference" "{reference}"
		)
		(path "/{symbol_uuid}")
		(sheetname "/{sheet_name}/")
		(sheetfile "{sheet_file}")
	)
'''


class MakeHierarchicalTests(unittest.TestCase):
    def test_one_top_sheet_places_every_sheet_and_every_link_gains_its_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "scanner.kicad_sch").write_text(sheet("aaaa", "s1", "R1"))
            (project / "rfid.kicad_sch").write_text(sheet("bbbb", "s2", "R2"))
            (project / "scanner.kicad_pro").write_text(json.dumps({"schematic": {"top_level_sheets": [
                {"filename": "scanner.kicad_sch", "name": "LED Halo", "uuid": "aaaa"},
                {"filename": "rfid.kicad_sch", "name": "RFID", "uuid": "bbbb"}]},
                "sheets": [["aaaa", "LED Halo"], ["bbbb", "RFID"]]}))
            (project / "scanner.kicad_pcb").write_text(
                "(kicad_pcb\n" + footprint("R1", "s1", "LED Halo", "scanner.kicad_sch")
                + footprint("R2", "s2", "RFID", "rfid.kicad_sch") + footprint("H1", "zz", "", "") + ")\n")
            result = make_hierarchical(project)
            self.assertEqual(result["renamed"], {"scanner.kicad_sch": "LED_Halo.kicad_sch"})
            self.assertEqual((result["footprints_relinked"], result["footprints_not_found"]), (2, ["H1"]))
            settings = json.loads((project / "scanner.kicad_pro").read_text())
            [top] = settings["schematic"]["top_level_sheets"]
            root = top["uuid"]
            self.assertEqual(top["filename"], "scanner.kicad_sch")
            self.assertEqual(settings["sheets"], [[root, "Root"], ["aaaa", "LED Halo"], ["bbbb", "RFID"]])
            top_sheet = (project / "scanner.kicad_sch").read_text()
            self.assertIn('(property "Sheetfile" "LED_Halo.kicad_sch"', top_sheet)
            self.assertIn('(property "Sheetfile" "rfid.kicad_sch"', top_sheet)
            self.assertIn('(title "Scanner")', top_sheet)
            moved = (project / "LED_Halo.kicad_sch").read_text()
            self.assertIn(f'(path "/{root}/aaaa"', moved)
            self.assertNotIn("sheet_instances", moved)
            board = (project / "scanner.kicad_pcb").read_text()
            self.assertIn('(path "/bbbb/s2")', board)
            self.assertIn('(sheetfile "LED_Halo.kicad_sch")', board)
            self.assertIn('(path "/zz")', board)
            # Run again: already one top sheet, nothing changes.
            self.assertFalse(make_hierarchical(project)["converted"])

    def test_netlist_nets_are_read_as_their_connections(self) -> None:
        netlist = '''(export (nets
		(net
			(code "1")
			(name "/RFID/FEED")
			(node
				(ref "C64")
				(pin "2")
			)
			(node
				(ref "AE2")
				(pin "4")
			)
		)
	))'''
        self.assertEqual(netlist_nets(netlist), {frozenset({("C64", "2"), ("AE2", "4")}): "/RFID/FEED"})


if __name__ == "__main__":
    unittest.main()
