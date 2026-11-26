"""RF widths from the board's stack-up and rounded bends in one call (26 Nov 2026).

The P0009 RF job worked out 50-ohm widths by hand six times and built arcs by hand eleven
times. The impedance maths runs anywhere; the board helpers need KiCad's own Python.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.providers.kicad_helpers import HELPERS, script_runner  # noqa: E402
from alx.providers.kicad_rf import RF_HELPERS  # noqa: E402

PYTHON = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
FIXTURES = REPOSITORY_ROOT / "tests/fixtures"
# P0009's top dielectric: 0.2104 mm of epsilon_r 4.3 to In1 (JLC's 7628 four-layer stack).
STACKUP = '''	(setup
		(stackup
			(layer "F.Cu" (type "copper") (thickness 0.035))
			(layer "dielectric 1" (type "core") (thickness 0.2104) (material "PP-017") (epsilon_r 4.3) (loss_tangent 0.02))
			(layer "In1.Cu" (type "copper") (thickness 0.0125))
			(layer "dielectric 2" (type "prepreg") (thickness 1.065) (material "Core-039") (epsilon_r 4.8) (loss_tangent 0.02))
			(layer "B.Cu" (type "copper") (thickness 0.035))
			(copper_finish "None")
			(dielectric_constraints no)
		)
'''


def maths():
    space = {}
    exec(RF_HELPERS, space)  # noqa: S102 - the helpers' own source, as the KiCad script gets it
    return space


class ImpedanceTests(unittest.TestCase):
    def test_coplanar_lines_on_p0009s_stack_up(self):
        coplanar = maths()["coplanar_impedance"]
        # What the job found by hand: 0.20 mm is about 69 ohm, 0.38 mm about 50 ohm, with a 0.2 mm gap.
        self.assertAlmostEqual(coplanar(0.20, 0.2, 0.2104, 4.3), 69.9, delta=0.5)
        self.assertAlmostEqual(coplanar(0.38, 0.2, 0.2104, 4.3), 50.2, delta=0.5)

    def test_microstrip_matches_the_usual_figure(self):
        # A 50-ohm microstrip on 1.6 mm FR4 (epsilon_r 4.5) is close to 3 mm wide.
        self.assertAlmostEqual(maths()["microstrip_impedance"](3.0, 1.6, 4.5), 50.0, delta=1.5)


def run(script, board):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "board.kicad_pcb"
        path.write_text(board)
        program = Path(folder) / "edit.py"
        program.write_text(HELPERS + RF_HELPERS + script_runner(script))
        return subprocess.run([str(PYTHON), str(program), str(path)], capture_output=True, text=True,
                              cwd=folder, timeout=120)


@unittest.skipUnless(PYTHON.is_file(), "KiCad is not installed")
class BoardTests(unittest.TestCase):
    def setUp(self):
        self.board = (FIXTURES / "copper_board.kicad_pcb").read_text().replace("\t(setup\n", STACKUP, 1)

    def test_the_width_comes_from_the_boards_stack_up(self):
        done = run('b = load()\nprint("width", rf_width(b, 50, "F", gap=0.2), rf_width(b, 50, "F"))\n', self.board)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        coplanar, microstrip = map(float, done.stdout.split("width")[1].split())
        self.assertAlmostEqual(coplanar, 0.38, delta=0.01)
        self.assertGreater(microstrip, coplanar)

    def test_route_connection_sizes_an_rf_line_from_the_stack_up(self):
        from alx.providers.kicad_board_helpers import BOARD_HELPERS
        from alx.providers.kicad_schematic_helpers import SCHEMATIC_HELPERS
        script = 'b = load()\nroute_connection(b, (60, 60), (70, 64), layers=["F"], net="ARC", impedance=50, gap=0.2)\n' \
                 'print("widths", sorted({round(_pcbnew.ToMM(t.GetWidth()), 3) for t in b.GetTracks() ' \
                 'if t.GetNetname() == "ARC" and xy(t.GetStart())[0] >= 59}))\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.kicad_pcb"
            path.write_text(self.board)
            program = Path(folder) / "edit.py"
            program.write_text(HELPERS + SCHEMATIC_HELPERS + BOARD_HELPERS + RF_HELPERS + script_runner(script))
            done = subprocess.run([str(PYTHON), str(program), str(path)], capture_output=True, text=True,
                                  cwd=folder, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("widths [0.38]", done.stdout)

    def test_a_board_without_a_stack_up_says_so(self):
        plain = (FIXTURES / "copper_board.kicad_pcb").read_text()
        done = run('b = load()\nrf_width(b)\n', plain)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no stack-up", done.stderr)

    def test_mitred_corners_become_arcs_and_nothing_else_changes(self):
        script = '''
b = load()
add_track(b, "ARC", [(60, 60), (64, 60), (66, 62), (66, 66)], 0.38)
others = sorted((t.GetNetname(), xy(t.GetStart()), xy(t.GetEnd())) for t in b.GetTracks() if t.GetNetname() != "ARC")
done = round_bends(b, "ARC")
after = sorted((t.GetNetname(), xy(t.GetStart()), xy(t.GetEnd())) for t in b.GetTracks() if t.GetNetname() != "ARC")
mine = [t for t in b.GetTracks() if t.GetNetname() == "ARC" and xy(t.GetStart())[0] >= 55]
arcs = [t for t in mine if t.GetClass() == "PCB_ARC"]
ends = sorted(xy(p) for t in mine for p in (t.GetStart(), t.GetEnd()))
save(b)
print("result", done["rounded"], done["runs"], len(arcs), others == after, ends[0], ends[-1])
print("again", round_bends(load(), "ARC")["rounded"])
'''
        done = run(script, self.board)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("result 2 1 2 True (60.0, 60.0) (66.0, 66.0)", done.stdout)
        self.assertIn("again 0", done.stdout)

    def test_a_curve_that_would_touch_another_net_leaves_the_run_as_it_was(self):
        script = '''
b = load()
add_track(b, "ARC", [(60, 60), (64, 60), (64, 64)], 0.38)
# Copper of another net right where the full curve and the half-radius one would run.
for spot in ((63.12, 60.88), (63.56, 60.44)):
    dot = _pcbnew.PCB_TRACK(b)
    dot.SetStart(point(*spot)); dot.SetEnd(point(spot[0] + 0.01, spot[1]))
    dot.SetWidth(_pcbnew.FromMM(0.15)); dot.SetLayer(layer("F")); dot.SetNet(b.FindNet("SIDE"))
    b.Add(dot)
before = sorted((xy(t.GetStart()), xy(t.GetEnd())) for t in b.GetTracks() if t.GetNetname() == "ARC")
done = round_bends(b, "ARC", radius=3.0)
after = sorted((xy(t.GetStart()), xy(t.GetEnd())) for t in b.GetTracks() if t.GetNetname() == "ARC")
print("result", done["rounded"], len(done["left"]), before == after)
'''
        done = run(script, self.board)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("result 0 1 True", done.stdout)

    def test_a_narrower_branch_keeps_its_junction(self):
        script = '''
b = load()
add_track(b, "ARC", [(60, 60), (64, 60), (64, 64)], 0.38)
add_track(b, "ARC", [(64, 60), (68, 56)], 0.2)
done = round_bends(b, "ARC")
print("result", done["rounded"])
'''
        done = run(script, self.board)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("result 0", done.stdout)


if __name__ == "__main__":
    unittest.main()
