"""Her whole KiCad job on real KiCad: copy, edit, save, write back (2026-11-13).

The unit tests run a stand-in pcbnew, which never writes the settings file
KiCad itself writes beside a board on every save. That file refused AL/X's
first write-back on 2026-11-13. This test runs the real thing end to end.
Needs KiCad; skipped where it is not installed (CI).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from kicad_actions import kicad_call  # noqa: E402
from alx.bootstrap.kicad import build_kicad_runtime  # noqa: E402
from alx.contracts import CapabilityResultState  # noqa: E402
from alx.providers.coding_process import kicad_containment_available  # noqa: E402

KICAD = Path("/Applications/KiCad/KiCad.app")
FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


@unittest.skipUnless(KICAD.is_dir() and kicad_containment_available(), "KiCad is not installed")
class RealRoundTripTests(unittest.TestCase):
    def test_an_edited_copy_writes_back_to_the_design(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            project = root / "designs" / "board"
            project.mkdir(parents=True)
            (project / "board.kicad_pcb").write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            (project / "board.kicad_pro").write_text(json.dumps({"meta": {"filename": "board.kicad_pro",
                                                                          "version": 1}}))
            runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                          root / "storage")

            provider = runtime.provider
            provider.render_folder = root / "renders"
            provider.viewer_settle_seconds = 0
            provider.open_viewer = lambda what, path, index: type("V", (), {"terminate": lambda self: None})()
            provider.capture_screen = lambda target: Path(target).write_bytes(b"png")

            def success(name, **arguments):
                if name == "approve_step":
                    # He approves what he was shown (D-082b): the copy is opened for him first.
                    kicad_call(runtime.executors, "open_in_kicad",
                               {"work_copy_id": arguments["work_copy_id"], "what": "board"})
                result = kicad_call(runtime.executors, name, arguments)
                self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
                return result.values

            # Copper left floating is a new unconnected item his original did not
            # have: approved or not, it is not written (review, 15 Nov).
            floating = success("check_kicad_board", project_dir=str(project))
            loose = success("edit_kicad_work_copy", work_copy_id=floating["work_copy_id"], script=(
                'b = load()\nadd_copper(b, "SIDE", "F", [(30, 2), (31, 2), (31, 3), (30, 3)])\n'
                'refill(b)\nsave(b)\n'))
            success("approve_step", step="routing", work_copy_id=loose["work_copy_id"],
                    work_copy_revision=loose["work_copy_revision"])
            refused = kicad_call(runtime.executors, "write_back_kicad_board", {
                "work_copy_id": loose["work_copy_id"], "work_copy_revision": loose["work_copy_revision"],
                "project_dir": str(project)})
            self.assertEqual(refused.failure["code"], "board_not_clean")
            self.assertNotIn("(xy 30 2)", (project / "board.kicad_pcb").read_text())
            # Copper joined to the net's own copper is written.
            checked = success("check_kicad_board", project_dir=str(project))
            edited = success("edit_kicad_work_copy", work_copy_id=checked["work_copy_id"], script=(
                'b = load()\nadd_copper(b, "SIDE", "F", [(26, 2), (28, 2), (28, 3), (26, 3)])\n'
                'refill(b)\nsave(b)\n'))
            # KiCad completes this minimal project file on save; the settings
            # file it also writes is not reported as a design change.
            self.assertIn("board.kicad_pcb", edited["change_report"]["files_changed"])
            self.assertNotIn("board.kicad_prl", edited["change_report"]["files_changed"])
            # KiCad's settings file appeared in her copy on save, and is not the design.
            work = root / "storage"
            self.assertTrue(list(work.rglob(checked["work_copy_id"] + "/project/board.kicad_prl")))
            # Copper drawn as a shape is routing, which he approves before it is written (D-082).
            self.assertEqual(edited["steps"]["routing"], "needs_approval")
            success("approve_step", step="routing", work_copy_id=edited["work_copy_id"],
                    work_copy_revision=edited["work_copy_revision"])
            written = success("write_back_kicad_board", work_copy_id=edited["work_copy_id"],
                              work_copy_revision=edited["work_copy_revision"], project_dir=str(project))
            self.assertIn("board.kicad_pcb", [Path(path).name for path in written["written"]])
            self.assertIn('(net "SIDE")', (project / "board.kicad_pcb").read_text())
            self.assertFalse((project / "board.kicad_prl").exists())


if __name__ == "__main__":
    unittest.main()
