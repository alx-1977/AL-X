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

    def test_a_sheet_file_outside_the_copy_is_refused(self) -> None:
        # Review of #239: a project file must never lead the conversion outside the copy.
        from alx.providers.kicad_hierarchy import HierarchyError
        with tempfile.TemporaryDirectory() as folder:
            outside = Path(folder) / "outside.kicad_sch"
            outside.write_text(sheet("cccc", "s3", "R3"))
            project = Path(folder) / "copy"
            project.mkdir()
            (project / "scanner.kicad_sch").write_text(sheet("aaaa", "s1", "R1"))
            for name in ("../outside.kicad_sch", str(outside)):
                (project / "scanner.kicad_pro").write_text(json.dumps({"schematic": {"top_level_sheets": [
                    {"filename": "scanner.kicad_sch", "name": "A", "uuid": "aaaa"},
                    {"filename": name, "name": "B", "uuid": "cccc"}]}}))
                with self.assertRaises(HierarchyError):
                    make_hierarchical(project)
            self.assertIn('(path "/cccc"', outside.read_text())

    def test_shared_global_labels_become_sheet_pins_joined_on_the_top_sheet(self) -> None:
        """Friedl, 13 Nov: Altium showed how the blocks connect; KiCad's global labels did not."""
        from alx.providers.kicad_hierarchy import connect_with_sheet_pins

        def label(name: str, shape: str) -> str:
            return (f'\t(global_label "{name}"\n\t\t(shape {shape})\n\t\t(at 10 10 0)\n'
                    f'\t\t(fields_autoplaced yes)\n\t\t(effects (font (size 1.27 1.27)))\n'
                    f'\t\t(uuid "u-{name}-{shape}")\n\t\t(property "Intersheetrefs" "${{INTERSHEET_REFS}}"\n'
                    f'\t\t\t(at 10 10 0)\n\t\t\t(hide yes)\n\t\t)\n\t)\n')
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "scanner.kicad_pro").write_text(json.dumps({"schematic": {"top_level_sheets": [
                {"filename": "scanner.kicad_sch", "name": "Root", "uuid": "root"}]}}))
            (project / "led.kicad_sch").write_text("(kicad_sch\n" + label("HALO", "input") + label("ONLY", "output") + ")\n")
            (project / "mcu.kicad_sch").write_text("(kicad_sch\n" + label("HALO", "output") + label("RFID{Serial}", "passive") + ")\n")
            (project / "rfid.kicad_sch").write_text("(kicad_sch\n" + label("RFID{Serial}", "passive") + ")\n")
            sheets = [{"uuid": u, "name": n, "filename": f} for u, n, f in (
                ("aa", "LED", "led.kicad_sch"), ("bb", "MCU", "mcu.kicad_sch"), ("cc", "RFID", "rfid.kicad_sch"))]
            from alx.providers.kicad_hierarchy import _top_sheet
            (project / "scanner.kicad_sch").write_text(_top_sheet("root", "scanner", sheets, ""))
            result = connect_with_sheet_pins(project)
            self.assertEqual(result["connected"], ["HALO", "RFID{Serial}"])
            led = (project / "led.kicad_sch").read_text()
            self.assertIn('(hierarchical_label "HALO"', led)
            self.assertIn('(global_label "ONLY"', led)   # used on one sheet only: left as it was
            top = (project / "scanner.kicad_sch").read_text()
            self.assertEqual(top.count('(pin "HALO"'), 2)
            self.assertEqual(top.count('(pin "RFID{Serial}"'), 2)
            self.assertIn("\t(bus\n", top)              # a bus label is joined by a bus
            self.assertIn('(global_label "HALO"', top)  # the net keeps its name
            # Each block's pin keeps its own sheet's direction (review of #240).
            self.assertIn('(pin "HALO" input', top)
            self.assertIn('(pin "HALO" output', top)
            # Done once: a top sheet that already has its own content is left alone.
            self.assertEqual(connect_with_sheet_pins(project)["connected"], [])
            # A plain top sheet with a note of his on it is left alone too (review of #240).
            plain = _top_sheet("root", "scanner", sheets, "").replace(
                "\t(sheet_instances", '\t(text "Rev B notes"\n\t\t(at 10 10 0)\n\t)\n\t(sheet_instances', 1)
            (project / "scanner.kicad_sch").write_text(plain)
            self.assertEqual(connect_with_sheet_pins(project)["connected"], [])
            self.assertIn("Rev B notes", (project / "scanner.kicad_sch").read_text())

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
