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
from alx.providers.kicad_hierarchy import netlist_nets  # noqa: E402
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
        return {name: nodes for nodes, name in netlist_nets(output.read_text()).items()}

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
            # Straight down out of R1 pin 2 first, then across and on to R2 pin 1.
            self.assertIn("route [(101.6, 105.41), (101.6, 107.95), (123.19, 107.95), (123.19, 101.6)]", out)
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


if __name__ == "__main__":
    unittest.main()
