"""The schematic helpers, checked by KiCad's own netlist (D-082).

Needs KiCad; skipped where it is not installed (CI).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.providers.kicad_helpers import HELPERS, script_runner  # noqa: E402
from alx.providers.kicad_index import netlist_model, netlist_nets  # noqa: E402
from alx.providers.kicad_schematic_helpers import SCHEMATIC_HELPERS  # noqa: E402

APP = Path("/Applications/KiCad/KiCad.app/Contents")
PYTHON = APP / "Frameworks/Python.framework/Versions/Current/bin/python3"
CLI = APP / "MacOS/kicad-cli"
DEVICE = APP / "SharedSupport/symbols/Device.kicad_sym"


def library_symbol(name):
    text = DEVICE.read_text()
    start = text.index('(symbol "%s"' % name)
    depth = 0
    for end in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[end], 0)
        if depth == 0:
            return text[start:end + 1]
    raise ValueError(name)


def placed(ref, at, root, rotation=0, mirror=None):
    return ('\t(symbol\n\t\t(lib_id "Device:R")\n\t\t(at %g %g %g)\n%s\t\t(unit 1)\n\t\t(uuid "%s")\n'
            '\t\t(property "Reference" "%s"\n\t\t\t(at %g %g 0)\n\t\t)\n'
            '\t\t(property "Value" "10k"\n\t\t\t(at %g %g 0)\n\t\t)\n'
            '\t\t(pin "1"\n\t\t\t(uuid "%s")\n\t\t)\n\t\t(pin "2"\n\t\t\t(uuid "%s")\n\t\t)\n'
            '\t\t(instances\n\t\t\t(project "t"\n\t\t\t\t(path "/%s"\n\t\t\t\t\t(reference "%s")\n'
            '\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
            % (at[0], at[1], rotation, '\t\t(mirror %s)\n' % mirror if mirror else '', uuid.uuid4(), ref,
               at[0] + 2, at[1], at[0] + 4, at[1], uuid.uuid4(), uuid.uuid4(), root, ref))


def schematic():
    root = str(uuid.uuid4())
    resistor = library_symbol("R").replace('(symbol "R"', '(symbol "Device:R"', 1)
    return ('(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n'
            '\t(uuid "%s")\n\t(paper "A4")\n\t(lib_symbols\n\t\t%s\n\t)\n%s%s%s%s'
            '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n'
            % (root, resistor.replace("\n", "\n\t\t"), placed("R1", (101.6, 101.6), root),
               placed("R2", (127, 101.6), root, rotation=90), placed("R3", (152.4, 101.6), root, mirror="y"),
               placed("R4", (177.8, 101.6), root, rotation=270)))


@unittest.skipUnless(PYTHON.is_file() and CLI.is_file() and DEVICE.is_file(), "KiCad is not installed")
class SchematicHelperTests(unittest.TestCase):
    def run_script(self, folder, script, symbols=None):
        program = Path(folder) / "edit.py"
        program.write_text(HELPERS + SCHEMATIC_HELPERS + "_LIBRARY_SYMBOLS.update(%r)\n" % (symbols or {})
                           + script_runner(script))
        done = subprocess.run([str(PYTHON), str(program), str(Path(folder) / "t.kicad_pcb")],
                              capture_output=True, text=True, cwd=folder, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        return done.stdout

    def netlist(self, folder):
        output = Path(folder) / "t.net"
        done = subprocess.run([str(CLI), "sch", "export", "netlist", "--output", str(output),
                               str(Path(folder) / "t.kicad_sch")], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return {name: nodes for nodes, name in netlist_nets(netlist_model(output.read_text())).items()}

    def test_new_sheet_and_region_move_keep_netlist_and_instance_identity(self):
        from alx.providers.kicad import sexpr
        with tempfile.TemporaryDirectory() as folder:
            top = Path(folder, "t.kicad_sch")
            top.write_text(schematic())
            self.run_script(folder, """
s = sheet("t.kicad_sch")
s.wire_pin_to("R1","2",s.symbol("R2")["pins"]["1"])
s.add_wire((50,90),(200,90))
s.save()
""")
            nets = self.netlist(folder)
            original = sexpr(top.read_text())
            uid = original.named("uuid")[0].children[1]
            symbols = [s.named("uuid")[0].children[1] for s in original.named("symbol")][:2]
            info = self.run_script(folder, """
import json
new_sheet("Power","power.kicad_sch")
print(json.dumps(move_region("t.kicad_sch","power.kicad_sch",[95,89,134,118])))
""")
            import json
            report = json.loads(info)
            self.assertEqual(report["references"], ["R1", "R2"])
            self.assertIn({"kind":"wire","points":[[50,90],[200,90]]}, report["crossings"])
            self.assertEqual(nets, self.netlist(folder))
            block = sexpr(top.read_text()).named("sheet")[0]
            sheet_id = block.named("uuid")[0].children[1]
            moved = sexpr(Path(folder, "power.kicad_sch").read_text()).named("symbol")
            self.assertEqual(symbols, [s.named("uuid")[0].children[1] for s in moved])
            for s in moved:
                path = s.named("instances")[0].named("project")[0].named("path")[0].children[1]
                self.assertEqual(path, f"/{uid}/{sheet_id}")

    def test_copy_layout_moves_the_target_fields_and_wiring_and_keeps_its_values(self):
        from alx.providers.kicad import sexpr
        with tempfile.TemporaryDirectory() as folder:
            top = Path(folder, "t.kicad_sch")
            text = schematic()
            start = text.index('(property "Reference" "R3"')
            top.write_text(text[:start] + text[start:].replace('"10k"', '"22k"', 1))
            self.run_script(folder, """
s = sheet("t.kicad_sch")
s.wire_pin_to("R1","2",s.symbol("R2")["pins"]["1"])
s.save()
copy_layout("t.kicad_sch", {"R1":"R3","R2":"R4"}, [95,88,134,120], (50.8,0))
""")
            tree = sexpr(top.read_text())
            r3 = next(s for s in tree.named("symbol") if s.named("property")[0].children[2] == "R3")
            self.assertEqual(r3.named("property")[1].children[2], "22k")
            self.assertEqual(r3.named("at")[0].children[1:], ["152.400000","101.600000","0"])
            self.assertIn(frozenset({("R3","2"),("R4","1")}), self.netlist(folder).values())

    def test_swap_to_a_derived_library_mpn_uses_its_base_pins_and_its_own_fields(self):
        from alx.providers.kicad import flat_symbol, sexpr
        with tempfile.TemporaryDirectory() as folder:
            top = Path(folder, "t.kicad_sch")
            top.write_text(schematic())
            library = '(kicad_symbol_lib ' + library_symbol("R") + (
                ' (symbol "Intermediate" (extends "R"))'
                ' (symbol "MPN" (extends "Intermediate") (property "Value" "14.5k" (at 0 0 0))))')
            tree = sexpr(library)
            flat = flat_symbol(library, tree, tree.named("symbol")[-1], "MPN")
            self.run_script(folder, 's = sheet("t.kicad_sch"); s.swap_symbol("R1","Local:MPN"); s.save()',
                            {"Local:MPN": flat})
            saved = sexpr(top.read_text())
            cached = next(s for s in saved.named("lib_symbols")[0].named("symbol") if s.children[1] == "Local:MPN")
            self.assertFalse(cached.named("extends"))
            self.assertTrue(any(unit.named("pin") for unit in cached.named("symbol")))
            value = next(p.children[2] for p in cached.named("property") if p.children[1] == "Value")
            self.assertEqual(value, "14.5k")
            self.assertTrue(any(("R1","1") in nodes for nodes in self.netlist(folder).values()))

    def test_wires_and_labels_at_pin_points_connect_in_kicads_netlist(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            self.run_script(folder, """
s = sheet("t.kicad_sch")
pins = {item["ref"]: item["pins"] for item in s.symbols()}
s.add_wire(pins["R1"]["2"], (pins["R1"]["2"][0], 115.57), (pins["R2"]["1"][0], 115.57), pins["R2"]["1"])
s.add_label("MIRRORED", pins["R3"]["1"])
s.add_label("TURNED", pins["R4"]["2"])
s.add_label("TURNED", pins["R2"]["2"])
s.add_label("MIRRORED", pins["R1"]["1"])
s.save()
print(pins)
""")
            nets = self.netlist(folder)
            joined = [nodes for nodes in nets.values() if ("R1", "2") in nodes]
            self.assertEqual(joined, [frozenset({("R1", "2"), ("R2", "1")})])
            # A local label's net takes the sheet's path, as KiCad names it.
            self.assertEqual(nets["/TURNED"], frozenset({("R4", "2"), ("R2", "2")}))
            self.assertEqual(nets["/MIRRORED"], frozenset({("R3", "1"), ("R1", "1")}))

    def test_removing_a_symbol_takes_only_its_own_wiring(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            self.run_script(folder, """
s = sheet("t.kicad_sch")
pins = {item["ref"]: item["pins"] for item in s.symbols()}
r1, r2, r3 = pins["R1"], pins["R2"], pins["R3"]
s.add_wire(r1["2"], (r1["2"][0], 115.57), (r2["1"][0], 115.57), r2["1"])
s.add_wire(r3["2"], (r3["2"][0], 120.65))
s.add_label("STUB", (r3["2"][0], 120.65))
s.add_no_connect(r3["1"])
s.add_label("KEEP", r1["1"])
s.save()
""")
            before = Path(folder, "t.kicad_sch").read_text()
            self.run_script(folder, """
s = sheet("t.kicad_sch")
left = s.remove_symbol("R3")
s.save()
print("left", len(left), len(s.wires()), sorted(l["name"] for l in s.labels()))
""")
            after = Path(folder, "t.kicad_sch").read_text()
            self.assertNotIn('"R3"', after)
            self.assertEqual(after.count("(no_connect"), 0)
            self.assertEqual(before.count("(wire") - after.count("(wire"), 1)
            self.assertNotIn('"STUB"', after)
            self.assertIn('"KEEP"', after)
            nets = self.netlist(folder)
            self.assertIn(frozenset({("R1", "2"), ("R2", "1")}), nets.values())

    def test_removing_items_names_only_the_pins_wired_to_them(self):
        """18 Nov review: remove_items excused every pin on the sheet for a small rectangle."""
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            done = self.run_script(folder, """
s = sheet("t.kicad_sch")
pins = {item["ref"]: item["pins"] for item in s.symbols()}
r1, r2, r3 = pins["R1"], pins["R2"], pins["R3"]
s.add_wire(r1["2"], (r1["2"][0], 115.57), (r2["1"][0], 115.57), r2["1"])
s.add_wire(r3["2"], (r3["2"][0], 120.65))
s.save()
s = sheet("t.kicad_sch")
mid = ((r1["2"][0] + r2["1"][0]) / 2, 115.57)
s.remove_items([r1["2"][0] - 0.1, 115.4, r2["1"][0] + 0.1, 115.7], ("wire",))
print("scope", s.removed_scope)
""")
            self.assertIn("scope ['R1.2', 'R2.1']", done)

    def test_a_swapped_symbol_keeps_its_place_and_gets_the_new_pins(self):
        capacitor = library_symbol("C")
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            out = self.run_script(folder, """
s = sheet("t.kicad_sch")
loose = s.swap_symbol("R1", "Device:C", value="100n", footprint="FireFli_Capacitors:C0402")
s.add_label("CAP", s.symbol("R1")["pins"]["1"])
s.add_label("CAP", s.symbol("R2")["pins"]["1"])
s.save()
print("swapped", s.symbol("R1")["lib_id"], s.symbol("R1")["value"], s.symbol("R1")["at"], loose)
""", symbols={"Device:C": capacitor})
            self.assertIn("swapped Device:C 100n (101.6, 101.6)", out)
            text = Path(folder, "t.kicad_sch").read_text()
            self.assertEqual(text.count('(symbol "Device:C"'), 1)
            self.assertIn('"FireFli_Capacitors:C0402"', text)
            nets = self.netlist(folder)
            self.assertEqual(nets["/CAP"], frozenset({("R1", "1"), ("R2", "1")}))

    def test_pins_are_wired_his_way_and_kicad_agrees(self):
        """15 Nov: wires straight out of each pin first, GND down on a short wire, text tidy."""
        with tempfile.TemporaryDirectory() as folder:
            text = schematic().replace('(property "Reference" "R4"', '(property "Reference" "#PWR01"', 1)
            text = text.replace('(reference "R4")', '(reference "#PWR01")', 1)
            (Path(folder) / "t.kicad_sch").write_text(text)
            out = self.run_script(folder, """
s = sheet("t.kicad_sch")
print("dirs", s.pin_direction("R1", "1"), s.pin_direction("R1", "2"), s.pin_direction("R2", "1"),
      s.pin_direction("R3", "1"))
points = s.wire_pin_to("R1", "2", s.symbol("R2")["pins"]["1"])
print("route", points)
s.label_pin("R2", "2", "OUT")
s.label_pin("R3", "1", "OUT")
end = s.ground_pin("R3", "2", "#PWR01")
print("gnd", end)
s.tidy_fields("R1")
s.tidy_fields("R2")
s.save()
""")
            self.assertIn("dirs (0, -1) (0, 1) (-1, 0) (0, -1)", out)
            # Straight down out of R1 pin 2 first, then across, and into R2 pin 1 the way that
            # pin points (from its left), never turning at the pin itself.
            self.assertIn("route [(101.6, 105.41), (101.6, 107.95), (120.65, 107.95), (120.65, 101.6), "
                          "(123.19, 101.6)]", out)
            self.assertIn("gnd (152.4, 107.95)", out)
            nets = self.netlist(folder)
            self.assertIn(frozenset({("R1", "2"), ("R2", "1")}), nets.values())
            self.assertEqual(nets["/OUT"], frozenset({("R2", "2"), ("R3", "1")}))
            saved = Path(folder, "t.kicad_sch").read_text()
            # R1 stands upright: its text beside it, centred; R2 lies down: above and below,
            # its text turned with it so it reads across (16 Nov).
            import re as _re
            r1 = _re.search(r'\(property "Reference" "R1"\n\t\t\t\(at ([-\d.]+) ([-\d.]+) 0\)', saved)
            self.assertTrue(r1 and abs(float(r1.group(1)) - 101.6) > 1.5, saved[:0])
            self.assertIn('(property "Reference" "R2"\n\t\t\t(at 127 ', saved)
            self.assertRegex(saved, r'\(property "Reference" "R2"\n\t\t\t\(at 127 [-\d.]+ 90\)')

    def test_a_power_symbol_is_copied_with_a_free_number(self):
        with tempfile.TemporaryDirectory() as folder:
            text = schematic()
            gnd = library_symbol("R")  # stands in: the copy only needs a #PWR reference
            text = text.replace('(property "Reference" "R4"', '(property "Reference" "#PWR01"', 1)
            text = text.replace('(reference "R4")', '(reference "#PWR01")', 1)
            (Path(folder) / "t.kicad_sch").write_text(text)
            self.run_script(folder, """
s = sheet("t.kicad_sch")
s.copy_symbol("#PWR01", (200.66, 101.6))
try:
    s.copy_symbol("R1", (10, 10))
except ValueError as error:
    print("refused", error)
s.save()
""")
            saved = Path(folder, "t.kicad_sch").read_text()
            self.assertIn('"#PWR0002"', saved)
            self.assertIn("(at 200.66 101.6 0)", saved)
            self.assertEqual(len(set(re.findall(r'\(uuid "([^"]+)"\)', saved))),
                             len(re.findall(r'\(uuid "([^"]+)"\)', saved)))

    def test_new_wiring_keeps_off_part_bodies_and_the_title_block(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            out = self.run_script(folder, """
s = sheet("t.kicad_sch")
print("block", s._title_block(s._tree()[0]))
for segment in (((127.0, 95.25), (127.0, 107.95)), ((180.34, 190.5), (190.5, 190.5)), ((115.57, 90.17), (139.7, 90.17))):
    try:
        s._check_segments([segment + ("test",)], set())
        print("clear")
    except ValueError as error:
        print("refused", error)
""")
            # A4 without his template: KiCad's own title block, bottom right inside the margins.
            self.assertIn("block (177.0, 168.0, 287.0, 200.0)", out)
            self.assertIn("refused test: the new wire would run across R2", out)
            self.assertIn("refused test: the new wire would run into the title block", out)
            self.assertIn("clear", out)

    def test_a_wire_finds_its_own_way_round_a_part_in_the_way(self):
        """19 Nov 2026: twenty refused edits in one job, each a guessed via point. With R5 lying
        across both simple turns, the wire finds a clear grid path and never doubles back."""
        with tempfile.TemporaryDirectory() as folder:
            blocked = schematic().replace(
                "\t(sheet_instances", placed("R5", (127, 107.95), "r5", rotation=90) + "\t(sheet_instances", 1)
            (Path(folder) / "t.kicad_sch").write_text(blocked)
            out = self.run_script(folder, """
s = sheet("t.kicad_sch")
print("path", s.wire_pin_to("R1", "2", s.symbol("R3")["pins"]["2"]))
s.save()
""")
            path = eval(out.split("path ", 1)[1].strip())
            self.assertGreater(len(path), 4)
            # Never through R5's body or along its leads (y 107.95 between its pins).
            for a, b in zip(path, path[1:]):
                self.assertFalse(a[1] == b[1] == 107.95 and min(a[0], b[0]) < 130.81 and max(a[0], b[0]) > 123.19)
            nets = self.netlist(folder)
            self.assertIn(frozenset({("R1", "2"), ("R3", "2")}), nets.values())

    def test_a_swap_writes_the_footprint_the_way_kicad_reads_it(self):
        """19 Nov 2026: a swap wrote Library/Name into the Footprint field; KiCad needs Library:Name
        and ERC reported eight parts with an empty library nickname."""
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            program = Path(folder) / "edit.py"
            resistor = library_symbol("R").replace('(symbol "R"', '(symbol "Device:R"', 1)
            program.write_text(HELPERS + SCHEMATIC_HELPERS
                               + "_LIBRARY_SYMBOLS.update(%r)\n" % {"Device:R": resistor} + script_runner(
                                   's = sheet("t.kicad_sch"); s.swap_symbol("R1", '
                                   '"Device:R", footprint="Resistor_SMD/R_0603_1608Metric"); s.save()\n'))
            done = subprocess.run([str(PYTHON), str(program), str(Path(folder) / "t.kicad_pcb")],
                                  capture_output=True, text=True, cwd=folder, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            sheet = (Path(folder) / "t.kicad_sch").read_text()
            self.assertIn('"Footprint" "Resistor_SMD:R_0603_1608Metric"', sheet)
            self.assertNotIn("Resistor_SMD/R_0603", sheet)

    def test_a_missing_symbol_names_the_sheet_it_is_on(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            (Path(folder) / "other.kicad_sch").write_text(schematic().replace('"R1"', '"U9"'))
            out = self.run_script(folder, """
try:
    sheet("t.kicad_sch").symbol("U9")
except KeyError as error:
    print(error)
""")
            self.assertIn("no symbol U9 on t.kicad_sch; it is on other.kicad_sch", out)

    def test_a_part_is_added_as_a_copy_and_joined_onto_wiring_with_a_tee(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            out = self.run_script(folder, """
s = sheet("t.kicad_sch")
s.wire_pin_to("R1", "2", s.symbol("R3")["pins"]["2"])
try:
    s.copy_part("R1", "R3", (60.96, 152.4))
except ValueError as error:
    print("refused", error)
print("pins", s.copy_part("R1", "R9", (114.3, 132.08)))
print("tee", s.wire_pin_to("R9", "1", s.symbol("R3")["pins"]["2"]))
s.save()
""")
            self.assertIn("refused R3 is already used on t.kicad_sch", out)
            nets = self.netlist(folder)
            self.assertIn(frozenset({("R1", "2"), ("R3", "2"), ("R9", "1")}), nets.values())
            self.assertIn("(junction", Path(folder, "t.kicad_sch").read_text())

    def test_a_swapped_part_takes_its_fields_from_the_library(self):
        with tempfile.TemporaryDirectory() as folder:
            text = schematic().replace('(property "Value" "10k"', '(property "MPN" "OLD-PART")\n\t\t(property "Value" "10k"', 1)
            (Path(folder) / "t.kicad_sch").write_text(text)
            self.run_script(folder, """
s = sheet("t.kicad_sch")
s.swap_symbol("R1", "Device:R", value="22k")
s.save()
""")
            saved = Path(folder, "t.kicad_sch").read_text()
            block = saved[saved.index('(property "Reference" "R1"') - 400:saved.index('(property "Reference" "R2"')]
            self.assertNotIn("OLD-PART", block)
            self.assertIn('(property "Description" "Resistor"', block)
            self.assertIn('(property "Value" "22k"', block)

    def test_a_swap_takes_fields_from_his_library_not_the_sheets_bare_copy(self):
        """16 Nov: the stored 100 pF part had no MPN; parts swapped to it lost theirs."""
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "t.kicad_sch").write_text(schematic())
            full = library_symbol("R").replace('(symbol "R"', '(symbol "R"', 1).replace(
                '(property "Description"', '(property "MPN" "RC0603FR-073R9L"\n\t\t\t(at 0 0 0)\n\t\t)\n'
                '\t\t(property "Description"', 1)
            self.run_script(folder, """
s = sheet("t.kicad_sch")
s.swap_symbol("R1", "Device:R", value="3.9")
s.save()
""", symbols={"Device:R": full})
            saved = Path(folder, "t.kicad_sch").read_text()
            block = saved[saved.index('(property "Reference" "R1"') - 400:saved.index('(property "Reference" "R2"')]
            self.assertIn('(property "MPN" "RC0603FR-073R9L"', block)
            self.assertEqual(saved.count('(symbol "Device:R"'), 1)


if __name__ == "__main__":
    unittest.main()
