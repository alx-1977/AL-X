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

    def test_route_in_his_style_follows_a_line_and_fits_rf_arcs(self) -> None:
        script = '''
import math
b = load()
print("style", style(b))
print("rf", rf_radius(0.355), rf_radius(0.5))
runs, vias = route(b, "TAPER", (40.0, 20.0), (52.0, 26.0), 0.15, "F", layers=["F"])
for name, points in runs:
    add_track(b, name and "TAPER", points, 0.15, name)
angles = set()
for name, points in runs:
    for p, q in zip(points, points[1:]):
        angles.add(round(math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 45, 3))
print("angles", sorted(angles))
runs2, _ = route(b, "SIDE", (40.0, 20.6), (52.0, 26.6), 0.15, "F", layers=["F"], follow=["TAPER"])
gaps = []
for name, points in runs2:
    for p in points[1:-1]:
        gaps.append(min(math.hypot(p[0] - x, p[1] - y) for _, pts in runs for x, y in pts))
print("bundled", max(gaps) < 1.0 if gaps else True)
made = add_track(b, "ARC", [(40, 30), (44, 30), (44.4, 30.4)], 0.355, "F", radius=1.0, fit=True)
print("arcs", sum(1 for m in made if m.GetClass() == "PCB_ARC"))
'''
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "board.kicad_pcb"
            board.write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            program = Path(folder) / "edit.py"
            program.write_text(HELPERS + script_runner(script))
            done = run(PYTHON, program, board)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("rf 1.0 1.5", done.stdout)
            self.assertIn("angles [0.0]", done.stdout)
            self.assertIn("bundled True", done.stdout)
            # A corner too tight for the full radius gets the largest arc that fits, not a refusal.
            self.assertIn("arcs 1", done.stdout)

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
runs, vias = route(b, "ARC", (110, 110), (115, 113), 0.2, "F")
import math as _rm
angles = {round(_rm.degrees(_rm.atan2(q[1] - p[1], q[0] - p[0]))) % 45 for _, pts in runs for p, q in zip(pts, pts[1:])}
print("routed", len(runs), len(vias), angles == {0})
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
            # A connection routed by code: one layer here, only 0/45/90-degree runs (17 Nov).
            self.assertIn("routed 1 0 True", done.stdout)
            self.assertIn("added B 90.0 (40.0, 40.0) ['ARC', 'BRAND_NEW']", done.stdout)
            self.assertNotIn(".alx-fp-", " ".join(p.name for p in Path(folder).iterdir()))
            self.assertRegex(done.stdout, r"teardrops [1-9]\d* 0")

    def test_set_properties_changes_only_what_is_named_by_address(self) -> None:
        """18 Nov: Altium's Inspector. A size change keeps the stroke; vias, tracks and texts too."""
        script = """
pb = _pcbnew.LoadBoard(BOARD_PATH.replace("board.kicad_pcb", "parts.kicad_pcb"))
items = items_by_id(pb)
refs = [f.Reference() for f in pb.GetFootprints()]
strokes = {f.m_Uuid.AsString(): f.GetTextThickness() for f in refs}
print("set", set_properties(pb, list(strokes), {"size": 0.5}))
print("strokes kept", all(f.GetTextThickness() == strokes[f.m_Uuid.AsString()] for f in refs),
      {round(_pcbnew.ToMM(f.GetTextHeight()), 3) for f in refs})
via = _pcbnew.PCB_VIA(pb)
via.SetPosition(point(20, 20))
pb.Add(via)
set_properties(pb, [via.m_Uuid.AsString()], {"size": 0.6, "drill": 0.3})
print("via", round(_pcbnew.ToMM(via.GetWidth(_pcbnew.F_Cu)), 3), round(_pcbnew.ToMM(via.GetDrill()), 3))
track = next(t for t in pb.GetTracks() if t.GetClass() == "PCB_TRACK")
set_properties(pb, [track.m_Uuid.AsString()], {"width": 0.25})
print("track", round(_pcbnew.ToMM(track.GetWidth()), 3))
b = load()
text = next(d for d in b.GetDrawings() if d.GetClass() == "PCB_TEXT")
set_properties(b, [text.m_Uuid.AsString()], {"text": "Keepout", "at": [110, 100]})
set_properties(b, [text.m_Uuid.AsString()], {"move_by": [0, -1.5], "visible": False})
print("text", text.GetText(), round(_pcbnew.ToMM(text.GetPosition().x), 3), round(_pcbnew.ToMM(text.GetPosition().y), 3), text.IsVisible())
try:
    set_properties(b, ["no-such-id"], {"size": 1})
except ValueError as error:
    print("missing", "no-such-id" in str(error))
"""
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "board.kicad_pcb"
            board.write_bytes((FIXTURES / "kicad" / "minimal.kicad_pcb").read_bytes())
            (Path(folder) / "parts.kicad_pcb").write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            program = Path(folder) / "edit.py"
            program.write_text(HELPERS + script_runner(script))
            done = run(PYTHON, program, board)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("strokes kept True {0.5}", done.stdout)
            self.assertIn("via 0.6 0.3", done.stdout)
            self.assertIn("track 0.25", done.stdout)
            self.assertIn("text Keepout 110.0 98.5 False", done.stdout)
            self.assertIn("missing True", done.stdout)

if __name__ == "__main__":
    unittest.main()
