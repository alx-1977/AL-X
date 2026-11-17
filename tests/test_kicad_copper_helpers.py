"""The copper-shape helpers, run on a real KiCad board (2026-11-12).

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

PYTHON = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


def run(executable, script, board):
    return subprocess.run([str(executable), str(script), str(board)], capture_output=True, text=True,
                          cwd=Path(script).parent, timeout=120)


@unittest.skipUnless(PYTHON.is_file(), "KiCad is not installed")
class CopperHelperTests(unittest.TestCase):
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
set_field(b, "U1", "MPN", "CC0402FRNPO9BN820")
set_field(b, "U1", "MPN", "GCM1555C1H820JA16D")
save(b)
print("result", len(before), mine["shape"], sorted(mine["corners"]))
print("fields", fields(load(), "U1"))
'''
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "board.kicad_pcb"
            board.write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            program = Path(folder) / "edit.py"
            program.write_text(HELPERS + script_runner(script))
            done = run(PYTHON, program, board)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("guard this zone has no outline", done.stdout)
            self.assertIn("removed 1", done.stdout)
            # A new field keeps its own name in KiCad 10, and setting it again changes it.
            self.assertIn("'MPN': 'GCM1555C1H820JA16D'", done.stdout)
            self.assertIn("'Datasheet': ''", done.stdout)
            self.assertIn("result 1 polygon [(14.4, 9.0), (14.7, 10.0), (15.3, 10.0), (15.6, 9.0)]", done.stdout)

    def test_a_part_swap_curved_tracks_lengths_and_teardrops(self) -> None:
        """14 Nov: the P0009 switch swap needed each of these, by hand."""
        footprint = (FIXTURES / "kicad" / "two_pad.kicad_mod").read_text()
        script = """
b = load()
before = describe(b, "U7")
swapped = replace_part(b, "U7", "Test/TwoPad", pin_nets={"1": "ARC", "2": "TURNED"})
print("swapped", swapped["at"] == before["at"], swapped["rotation"], sorted(p["net"] for p in swapped["pads"]),
      swapped["unconnected"], part(b, "U7").GetFPID().GetUniStringLibItemName())
already = track_length(b, "ARC")
made = add_track(b, "ARC", [(100, 100), (105, 100), (105, 105)], 0.3, radius=1.0)
print("made", sorted(item.GetClass() for item in made))
print("length", round(track_length(b, "ARC") - already, 3))
try:
    add_track(b, "ARC", [(0, 0), (0.5, 0), (0.5, 5)], 0.3, radius=1.0)
except ValueError as error:
    print("tight", "does not fit" in str(error))
try:
    add_track(b, "ARC", [(0, 0), (5, 0), (5, 5)], 0.3)
except ValueError as error:
    print("touch", "would touch pad U1.1 of net STEP" in str(error))
added = add_part(b, "U99", "Test/TwoPad", (40, 40), 90, "B", "NEW", {"1": "BRAND_NEW", "2": "ARC"})
print("added", added["side"], added["rotation"], added["at"], sorted(p["net"] for p in added["pads"]))
save(b)
t = _pcbnew.LoadBoard(BOARD_PATH.replace("board.kicad_pcb", "teardrops.kicad_pcb"))
print("teardrops", len(teardrops(t)), len(teardrops(t, (1000, 1000, 1001, 1001))))
"""
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "board.kicad_pcb"
            board.write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            (Path(folder) / "teardrops.kicad_pcb").write_bytes((FIXTURES / "teardrops.kicad_pcb").read_bytes())
            program = Path(folder) / "edit.py"
            program.write_text(HELPERS + "_LIBRARY_FOOTPRINTS.update(%r)\n" % {"Test/TwoPad": footprint}
                               + script_runner(script))
            done = run(PYTHON, program, board)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("swapped True", done.stdout)
            self.assertIn("['ARC', 'TURNED'] [] TwoPad", done.stdout)
            self.assertIn("made ['PCB_ARC', 'PCB_TRACK', 'PCB_TRACK']", done.stdout)
            # Two 4 mm runs and a quarter circle of 1 mm: 8 + pi/2.
            self.assertIn("length 9.571", done.stdout)
            self.assertIn("tight True", done.stdout)
            # A track onto another net's pad is refused before anything is drawn (17 Nov).
            self.assertIn("touch True", done.stdout)
            self.assertIn("added B 90.0 (40.0, 40.0) ['ARC', 'BRAND_NEW']", done.stdout)
            self.assertNotIn(".alx-fp-", " ".join(p.name for p in Path(folder).iterdir()))
            self.assertRegex(done.stdout, r"teardrops [1-9]\d* 0")

if __name__ == "__main__":
    unittest.main()
