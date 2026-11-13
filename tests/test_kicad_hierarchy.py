"""Separate top-level sheets joined into one hierarchical design (2026-11-13)."""

from __future__ import annotations

import json
import itertools
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.kicad_hierarchy import make_hierarchical, netlist_nets  # noqa: E402
from alx.providers.kicad_hierarchy import connect_with_sheet_pins, _top_sheet  # noqa: E402
from alx.providers.kicad_top_layout import crossing_count, layout, _route, _segments  # noqa: E402


# Four blocks, five links, two buses: a cycle with a diagonal, like RegScanner.
LINKS = {"HALO": [0, 1], "ENABLE": [1, 2], "IRQ": [2, 3],
         "DATA[0..1]": [3, 0], "RFID{TX RX}": [0, 2]}


def scanner_fixture(project: Path):
    """Real KiCad symbols and bus entries, with every bus member populated."""
    sheets = [{"uuid": str(uuid.uuid4()), "name": name, "filename": name + ".kicad_sch"}
              for name in ("MCU", "Halo", "RFID", "Power")]
    library = '''(lib_symbols
        (symbol "Test:Terminal"
            (pin_names (offset 0)) (in_bom yes) (on_board yes)
            (property "Reference" "J" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "Terminal" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (symbol "Terminal_1_1"
                (pin passive line (at 0 0 0) (length 2.54)
                    (name "P" (effects (font (size 1.27 1.27))))
                    (number "1" (effects (font (size 1.27 1.27)))))
            )
        )
    )'''
    for index, info in enumerate(sheets):
        parts = [f'(kicad_sch (version 20260306) (generator "eeschema")\n'
                 f'(uuid "{info["uuid"]}") (paper "A3")\n', library]
        for slot, (label, members) in enumerate(LINKS.items()):
            if index not in members:
                continue
            y = 25.4 + slot * 30.48
            bus = "[" in label or "{" in label
            parts.append(f'\n\t(global_label "{label}"\n\t\t(shape passive)\n'
                         f'\t\t(at 25.4 {y:.2f} 0)\n'
                         f'\t\t(effects (font (size 1.27 1.27)))\n'
                         f'\t\t(uuid "{uuid.uuid4()}")\n\t)\n')
            names = ["DATA0", "DATA1"] if "[" in label else ["RFID.TX", "RFID.RX"] if bus else [label]
            if bus:
                parts.append(f'(bus (pts (xy 25.4 {y:.2f}) (xy 25.4 {y + 10.16:.2f}))'
                             f' (stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))')
            for member, net in enumerate(names):
                sy = y + (member * 5.08 + 5.08 if bus else 0)
                if bus:
                    parts.append(f'(bus_entry (at 25.4 {sy - 2.54:.2f}) (size 2.54 2.54)'
                                 f' (stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))'
                                 f'\n(label "{net}" (at 40.64 {sy:.2f} 0)'
                                 f' (effects (font (size 1.27 1.27))) (uuid "{uuid.uuid4()}"))')
                parts.append(f'(wire (pts (xy {27.94 if bus else 25.4} {sy:.2f}) (xy 50.8 {sy:.2f}))'
                             f' (stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))')
                reference = f'J{index + 1}{slot}{member}'
                parts.append(f'''\n\t(symbol
\t\t(lib_id "Test:Terminal")
\t\t(at 50.8 {sy:.2f} 0)
\t\t(unit 1)
\t\t(in_bom yes) (on_board yes) (dnp no)
\t\t(uuid "{uuid.uuid4()}")
\t\t(property "Reference" "{reference}" (at 50.8 {sy:.2f} 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "Terminal" (at 50.8 {sy:.2f} 0) (effects (font (size 1.27 1.27))))
\t\t(pin "1" (uuid "{uuid.uuid4()}"))
\t\t(instances (project "scanner" (path "/{info['uuid']}" (reference "{reference}") (unit 1))))
\t)\n''')
        (project / info["filename"]).write_text("\n".join(parts) + "\n)\n")
    (project / "scanner.kicad_pro").write_text(json.dumps({"schematic": {"top_level_sheets": sheets}}))
    return sheets


def emitted_geometry(text):
    """Inspect the serialized geometry, independently of the layout objects."""
    segments = []
    for kind, x1, y1, x2, y2 in re.findall(
            r'\t\((wire|bus)\n\s*\(pts\s*\(xy ([\d.]+) ([\d.]+)\) \(xy ([\d.]+) ([\d.]+)\)', text):
        segments.append((kind, tuple(map(float, (x1, y1))), tuple(map(float, (x2, y2)))))
    rectangles = [tuple(map(float, values)) for values in re.findall(
        r'\t\(sheet\n\s*\(at ([\d.]+) ([\d.]+)\)\n\s*\(size ([\d.]+) ([\d.]+)\)', text)]
    return rectangles, segments


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
    def test_planar_scanner_layout_geometry_and_determinism(self):
        outputs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                scanner_fixture(project)
                make_hierarchical(project)
                result = connect_with_sheet_pins(project)
                self.assertEqual(result["top_sheet_crossings"], 0)
                top = (project / "scanner.kicad_sch").read_text()
                rectangles, segments = emitted_geometry(top)
                self.assertEqual(len(rectangles), 4)
                self.assertEqual({kind for kind, _, _ in segments}, {"wire", "bus"})
                self.assertGreater(max(x for x, _, _, _ in rectangles) - min(x for x, _, _, _ in rectangles), 100)
                self.assertGreater(max(y for _, y, _, _ in rectangles) - min(y for _, y, _, _ in rectangles), 100)
                for _, a, b in segments:
                    self.assertTrue(a[0] == b[0] or a[1] == b[1])
                    self.assertNotEqual(a, b)
                    for coord in (*a, *b):
                        self.assertAlmostEqual(coord / 2.54, round(coord / 2.54))
                    for x, y, w, h in rectangles:
                        if a[0] == b[0]:
                            self.assertFalse(x < a[0] < x + w and max(min(a[1], b[1]), y) < min(max(a[1], b[1]), y + h))
                        else:
                            self.assertFalse(y < a[1] < y + h and max(min(a[0], b[0]), x) < min(max(a[0], b[0]), x + w))
                # Count proper wire/bus crossings directly from emitted segments.
                hits = []
                for (_, a, b), (_, c, d) in itertools.combinations(segments, 2):
                    if a[0] == b[0] and c[1] == d[1]:
                        a, b, c, d = c, d, a, b
                    if a[1] == b[1] and c[0] == d[0]:
                        if min(a[0], b[0]) < c[0] < max(a[0], b[0]) and min(c[1], d[1]) < a[1] < max(c[1], d[1]):
                            hits.append((c[0], a[1]))
                self.assertEqual(len(hits), result["top_sheet_crossings"])
                # Compare geometry including the chosen pin sides/order, ignoring IDs.
                outputs.append(re.sub(r'\(uuid "[^"]*"\)|\(path "/[^\"]*"', '', top))
        self.assertEqual(*outputs)

    def test_crossing_semantics_and_multi_terminal_tree(self):
        horizontal = [((0, 2), (4, 2))]
        self.assertEqual(crossing_count({"wire": horizontal, "bus": [((2, 0), (2, 4))]}), 1)
        self.assertEqual(crossing_count({"a": horizontal, "end": [((2, 2), (2, 4))]}), 0)
        self.assertEqual(crossing_count({"a": horizontal, "bend": [((2, 0), (2, 2)), ((2, 2), (3, 2))]}), 0)
        rects, pins, routes, joins, count, _ = layout(4, {"COMMON": [0, 1, 2, 3], "PAIR": [0, 1]})
        self.assertEqual(count, crossing_count(routes))
        # Every pin belongs to the same connected tree; every branch is emitted.
        for label, segments in routes.items():
            points = set()
            for a, b in segments:
                dx, dy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
                p = a
                while True:
                    points.add(p)
                    if p == b:
                        break
                    p = p[0] + dx, p[1] + dy
            for (i, net), (p, _, _) in pins.items():
                if net == label:
                    self.assertIn(p, points)
            self.assertTrue(all(p in points for p in joins[label]))

    def test_parallel_links_have_matching_pin_orders(self):
        uses = {name: [0, 1] for name in ("A", "B", "C[0..1]", "D{TX RX}")}
        _, pins, _, _, crossings, _ = layout(2, uses)
        self.assertEqual(crossings, 0)
        for name in uses:
            a, _, aa = pins[0, name]
            b, _, ba = pins[1, name]
            self.assertEqual((aa - ba) % 360, 180)
            self.assertEqual(a[1] if aa in (0, 180) else a[0], b[1] if ba in (0, 180) else b[0])

    def test_router_crosses_only_straight_interiors_when_forced(self):
        occupied = {(8, y): 2 if 8 < y < 20 else 3 for y in range(8, 21)}
        path = _route((4, 14), {(12, 14)}, set(), occupied, (12, 20), 1)
        self.assertIsNotNone(path)
        segments, _ = _segments({tuple(sorted(pair)) for pair in zip(path, path[1:])})
        self.assertEqual(crossing_count({"wire": segments, "bus": [((8, 8), (8, 20))]}), 1)
        at = path.index((8, 14))
        self.assertEqual((path[at - 1][1], path[at + 1][1]), (14, 14))

    def test_real_kicad_netlist_identity_with_two_buses(self):
        cli = shutil.which("kicad-cli")
        mac_cli = Path('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')
        cli = cli or (str(mac_cli) if mac_cli.is_file() else None)
        if not cli:
            self.skipTest("kicad-cli required for canonical netlist comparison")
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            scanner_fixture(project)
            make_hierarchical(project)

            def export(name):
                output = project / (name + ".net")
                result = subprocess.run([cli, "sch", "export", "netlist", "--output", str(output),
                                         str(project / "scanner.kicad_sch")], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return netlist_nets(output.read_text())

            before = export("before")
            self.assertEqual(len(before), 7)  # three wires, two members of each bus
            self.assertTrue(all(len(nodes) == 2 for nodes in before))
            self.assertEqual(connect_with_sheet_pins(project)["top_sheet_crossings"], 0)
            self.assertEqual(export("after"), before)

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
