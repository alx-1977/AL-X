"""Friedl's layout rule and the copper-shape helpers, run on real KiCad boards (2026-11-12).

Needs KiCad's own Python; skipped where KiCad is not installed (CI).
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
from alx.providers.kicad_rules import check_layout_rules  # noqa: E402

PYTHON = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


def run(executable, script, board):
    return subprocess.run([str(executable), str(script), str(board)], capture_output=True, text=True,
                          cwd=Path(script).parent, timeout=120)


@unittest.skipUnless(PYTHON.is_file(), "KiCad is not installed")
class LayoutRuleTests(unittest.TestCase):
    def test_steps_are_found_on_every_layer_and_angle_and_tapers_pass(self) -> None:
        # Steps: U1 a 1.2 mm shape at a 0.5 mm pad, U5 a wide track at a pad
        # turned 45 degrees, U6 a wide track on an inner layer, U7 a wide arc.
        # No steps: U2 tapers, U3 sits on the side of a wide strip, U4 a track
        # no wider than its pad that bends away.
        with tempfile.TemporaryDirectory() as scratch:
            result = check_layout_rules(FIXTURES / "rules_board.kicad_pcb", scratch_root=Path(scratch),
                                        kicad_python=PYTHON, execute_python=run)
        self.assertEqual(result["omitted"], 0)
        found = sorted((f["pad"], f["net"], f["layer"], f["where"], f["copper_mm"]) for f in result["findings"])
        self.assertEqual(found, [("U1.1", "STEP", "F.Cu", "bottom", 1.2), ("U5.1", "TURNED", "F.Cu", "track", 1.2),
                                 ("U6.1", "INNER", "In1.Cu", "track", 1.2), ("U7.1", "ARC", "F.Cu", "track", 1.2)])
        self.assertEqual({f["rule"] for f in result["findings"]}, {"taper_where_copper_is_wider_than_its_pad"})

    def test_copper_shape_helpers_and_the_outline_guard(self) -> None:
        script = '''
b = load()
before = copper_shapes(b, "STEP")
taper = add_taper(b, "TAPER", "F", (15.0, 9.0), 1.2, (15.0, 10.0), 0.6)
mine = [c for c in copper_shapes(b, "TAPER") if c["id"] == taper.m_Uuid.AsString()][0]
set_copper_corners(b, before[0]["id"], [(4.75, 5.25), (5.25, 5.25), (5.6, 6.25), (5.6, 9), (4.4, 9), (4.4, 6.25)])
try:
    zone_corners(type("Z", (), {"Outline": lambda self: _pcbnew.SHAPE_POLY_SET()})())
except ValueError as error:
    print("guard", error)
extra = add_copper(b, "SIDE", "F", [(30, 2), (31, 2), (31, 3), (30, 3)])
remove_copper(b, extra.m_Uuid.AsString())
print("removed", len(copper_shapes(b, "SIDE")))
save(b)
print("result", len(before), mine["shape"], sorted(mine["corners"]))
'''
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "board.kicad_pcb"
            board.write_bytes((FIXTURES / "rules_board.kicad_pcb").read_bytes())
            program = Path(folder) / "edit.py"
            program.write_text(HELPERS + script_runner(script))
            done = run(PYTHON, program, board)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("guard this zone has no outline", done.stdout)
            self.assertIn("removed 1", done.stdout)
            self.assertIn("result 1 polygon [(14.4, 9.0), (14.7, 10.0), (15.3, 10.0), (15.6, 9.0)]", done.stdout)
            # The step reshaped into a taper now passes the rule.
            result = check_layout_rules(board, scratch_root=Path(folder) / "scratch",
                                        kicad_python=PYTHON, execute_python=run)
            self.assertNotIn("U1.1", [f["pad"] for f in result["findings"]])


if __name__ == "__main__":
    unittest.main()
