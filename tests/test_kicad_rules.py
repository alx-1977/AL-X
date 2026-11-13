"""Friedl's taper rule, checked on a real KiCad board (2026-11-12, finished 2026-11-13).

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

from alx.providers.kicad_rules import check_layout_rules  # noqa: E402

PYTHON = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


def run(executable, script, board):
    return subprocess.run([str(executable), str(script), str(board)], capture_output=True, text=True,
                          cwd=Path(script).parent, timeout=120)


@unittest.skipUnless(PYTHON.is_file(), "KiCad is not installed")
class TaperRuleTests(unittest.TestCase):
    def test_every_step_is_found_and_tapers_and_side_strips_pass(self) -> None:
        # Steps: U1 a 1.2 mm shape at a 0.5 mm pad; U5 a wide track at a pad
        # turned 45 degrees; U6 on an inner layer; U7 an arc. From the review
        # of #238: U8 a wide track starting just off the pad; U9 a step beside
        # a separate side strip; U10 copper covering half an edge. From the
        # review of #239: U11 copper meeting the pad at its corner.
        # No steps: U2 tapers, U3 sits on the side of a wide strip, U4 bends.
        with tempfile.TemporaryDirectory() as scratch:
            result = check_layout_rules(FIXTURES / "copper_board.kicad_pcb", scratch_root=Path(scratch),
                                        kicad_python=PYTHON, execute_python=run)
        self.assertEqual(result["omitted"], 0)
        found = sorted((f["pad"], f["layer"], f["where"], f["copper_mm"]) for f in result["findings"])
        self.assertEqual(found, [
            ("U1.1", "F.Cu", "bottom", 1.2), ("U10.1", "F.Cu", "bottom", 1.2),
            ("U11.1", "F.Cu", "bottom", 1.2),
            ("U5.1", "F.Cu", "track", 1.2), ("U6.1", "In1.Cu", "track", 1.2),
            ("U7.1", "F.Cu", "track", 1.2), ("U8.1", "F.Cu", "track", 1.2),
            ("U9.1", "F.Cu", "bottom", 1.05)])


if __name__ == "__main__":
    unittest.main()
