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
    def test_a_step_is_found_a_taper_passes_and_a_pad_on_wide_copper_is_no_joint(self) -> None:
        # U1: 1.2 mm copper starts at a 0.5 mm pad. U2: tapers from 0.5 to 1.2 mm.
        # U3: sits on the side of a wide strip that runs past it.
        with tempfile.TemporaryDirectory() as scratch:
            findings = check_layout_rules(FIXTURES / "rules_board.kicad_pcb", scratch_root=Path(scratch),
                                          kicad_python=PYTHON, execute_python=run)
        self.assertEqual(findings, [{"rule": "taper_where_copper_is_wider_than_its_pad", "pad": "U1.1",
                                     "net": "STEP", "side": "bottom", "pad_mm": 0.5, "copper_mm": 1.2}])

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
            findings = check_layout_rules(board, scratch_root=Path(folder) / "scratch",
                                          kicad_python=PYTHON, execute_python=run)
            self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
