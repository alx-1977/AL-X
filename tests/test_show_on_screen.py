"""She shows Friedl a board over the reader wave (D-073)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.providers.kicad_section import SECTION_SCRIPT, SectionError, read_section  # noqa: E402
from alx.tools.display import (  # noqa: E402
    DEFINITIONS,
    SHOW_ON_SCREEN,
    DisplayBoard,
    build_display_executors,
)

ASSETS = REPOSITORY_ROOT / "src/alx/interfaces/assets"
GEOMETRY = {"window": [133, 113, 181, 138], "tracks": [], "vias": [], "planes": [], "keepouts": [],
            "edges": [], "pads": [], "parts": [{"ref": "C61", "value": "4.3p", "at": [150.2, 119.6],
                                                "rotation": -90, "side": "F"}]}


def display(**changes):
    values = {
        "title": "AE1", "subtitle": "Cellular", "context": "P0020 · Antenna layout",
        "verdict": {"text": "1 differs from Würth", "tone": "bad"},
        "items": [{"title": "Feed · 13.4 mm", "detail": "Würth ≈6.5 mm", "tone": "bad"}],
        "said": "It's on screen.",
        "views": [{"label": "Yours", "board": "/designs/P0020/board.kicad_pcb",
                   "window": [133, 113, 181, 138], "refs": ["C61"], "nets": ["Net-(AE1-Feed)"],
                   "marks": {"C61": "ok", "net:Net-(AE1-Feed)": "bad"}, "rotate": True}],
    }
    values.update(changes)
    return values


class ShowOnScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = DisplayBoard()
        self.read = []

        def section(arguments):
            self.read.append(dict(arguments))
            return GEOMETRY
        self.show = build_display_executors(self.board, section, lambda: "call-1")[SHOW_ON_SCREEN]

    def test_a_display_is_published_with_the_board_geometry(self) -> None:
        result = self.show(display())
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values["version"], 1)
        snapshot = self.board.snapshot()
        self.assertEqual(snapshot["version"], 1)
        shown = snapshot["display"]
        self.assertEqual(shown["title"], "AE1")
        self.assertEqual(shown["views"][0]["geometry"], GEOMETRY)
        self.assertTrue(shown["views"][0]["rotate"])
        self.assertEqual(self.read[0]["board"], "/designs/P0020/board.kicad_pcb")
        self.assertEqual(self.read[0]["refs"], ["C61"])

    def test_each_show_is_a_new_version(self) -> None:
        self.show(display())
        self.assertEqual(self.show(display(views=[])).values["version"], 2)

    def test_unknown_tones_and_ambiguous_boards_are_refused(self) -> None:
        for bad in (
            display(verdict={"text": "x", "tone": "purple"}),
            display(items=[{"title": "x", "tone": "loud"}]),
            display(views=[{"label": "x", "window": [0, 0, 1, 1]}]),
            display(views=[{"label": "x", "window": [0, 0, 1, 1], "board": "a", "work_copy_id": "b"}]),
            display(title=""),
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.show(bad).failure["code"], "arguments_unusable")
        self.assertEqual(self.board.snapshot()["version"], 0)

    def test_a_board_that_cannot_be_read_is_reported_and_nothing_is_shown(self) -> None:
        def broken(_):
            raise SectionError("board_unavailable", "not a .kicad_pcb file")
        show = build_display_executors(self.board, broken, lambda: "call-1")[SHOW_ON_SCREEN]
        result = show(display())
        self.assertEqual(result.failure["code"], "board_unavailable")
        self.assertEqual(self.board.snapshot()["display"], None)

    def test_a_restart_gives_the_board_a_new_identity(self) -> None:
        # A screen left open across a restart must not take version 1 of the
        # new run for one it has already seen.
        self.assertNotEqual(DisplayBoard().snapshot()["id"], DisplayBoard().snapshot()["id"])
        script = (ASSETS / "display.js").read_text()
        self.assertIn("id !== seenId || version > seenVersion", script)
        # One poll at a time: a late, older answer cannot reopen a closed viewer.
        self.assertIn("if (polling) return;", script)

    def test_an_overlong_label_is_named_before_the_board_is_read(self) -> None:
        for view in ({"label": "x" * 41}, {"label": "x", "sublabel": "y" * 61}):
            with self.subTest(view=view):
                bad = display(views=[{**display()["views"][0], **view}])
                result = self.show(bad)
                self.assertEqual(result.failure["code"], "arguments_unusable")
                self.assertIn("label", result.failure["reason"])
        self.assertEqual(self.read, [])

    def test_it_changes_nothing_outside_her_screen(self) -> None:
        self.assertEqual(DEFINITIONS[0].side_effect.name, "NONE")


class ReadSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.board = root / "designs" / "board.kicad_pcb"
        self.board.parent.mkdir()
        self.board.write_text("(kicad_pcb)")
        self.scratch = root / "scratch"
        self.seen = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_python(self, python, script, board):
        board = Path(board)
        self.seen.append(board)
        args = json.loads((board.parent / "section-args.json").read_text())
        (board.parent / "section.json").write_text(json.dumps({"window": args["window"], "parts": []}))
        return SimpleNamespace(returncode=0, stderr="")

    def test_the_board_is_read_from_a_copy_that_is_then_removed(self) -> None:
        out = read_section(self.board, {"window": [0, 0, 10, 10], "refs": ["C1"]},
                           scratch_root=self.scratch, kicad_python=Path("python"),
                           execute_python=self.run_python)
        self.assertEqual(out["window"], [0, 0, 10, 10])
        self.assertNotEqual(self.seen[0].parent, self.board.parent)
        self.assertTrue(self.seen[0].is_relative_to(self.scratch))
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(self.board.read_text(), "(kicad_pcb)")

    def test_bad_windows_and_files_are_refused_before_anything_runs(self) -> None:
        for arguments in ({"window": [10, 0, 0, 10]}, {"window": [0, 0, 1]},
                          {"window": [0, 0, 1, 1], "refs": "C1"}):
            with self.subTest(arguments=arguments), self.assertRaises(SectionError):
                read_section(self.board, arguments, scratch_root=self.scratch,
                             kicad_python=Path("python"), execute_python=self.run_python)
        with self.assertRaises(SectionError):
            read_section(self.board.with_suffix(".txt"), {"window": [0, 0, 1, 1]},
                         scratch_root=self.scratch, kicad_python=Path("python"),
                         execute_python=self.run_python)
        self.assertEqual(self.seen, [])

    def test_layers_are_read_by_number_not_by_name(self) -> None:
        # Boards converted from Altium call their layers "Top Layer".
        self.assertIn("pcbnew.F_Cu", SECTION_SCRIPT)
        self.assertNotIn('"F.Cu"', SECTION_SCRIPT)
        self.assertIn("Edge_Cuts", SECTION_SCRIPT)


class ViewerBehaviourTests(unittest.TestCase):
    """Review of c53ffdd: run display.js itself against scripted answers."""

    def test_closed_stays_closed_and_a_stalled_poll_does_not_stop_polling(self) -> None:
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        answers = [
            {"id": "a", "version": 1, "display": {}},   # on load: not reopened
            {"id": "a", "version": 2, "display": {}},   # new: opens
            {"id": "a", "version": 1, "display": {}},   # closed, then a late older answer
            {"id": "a", "version": 2, "display": {}},   # the same version again
            "stall",                                    # never answers: abandoned
            "error",                                    # network error
            {"id": "b", "version": 1, "display": {}},   # after a restart: opens
        ]
        output = subprocess.run(
            [node, str(REPOSITORY_ROOT / "tests/display_harness.mjs"),
             str(ASSETS / "display.js"), json.dumps(answers)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        result = json.loads(output)
        self.assertEqual(result["open_after_each_step"], [1, 0, 0, 0, 0, 1])
        self.assertEqual(result["opened"], 2)
        self.assertEqual(result["requests"], len(answers))


class ViewerAssetTests(unittest.TestCase):
    def test_the_viewer_sits_on_the_reader_wave(self) -> None:
        page = (ASSETS / "show.html").read_text()
        self.assertIn('src="/bhl-field?backdrop"', page)
        field = (ASSETS / "bhl-field.html").read_text()
        self.assertIn("has('backdrop')", field)
        # The backdrop polls nothing and shows no readers.
        self.assertIn("if(!document.documentElement.classList.contains('backdrop')){poll();", field)

    def test_a_rotated_view_is_turned_not_mirrored(self) -> None:
        # Swapping the axes alone mirrors the board; one axis must be reversed.
        self.assertIn("(x, y) => [(y - y0) * S, (x1 - x) * S]", (ASSETS / "show.js").read_text())

    def test_planes_take_their_net_tone_and_the_column_scrolls(self) -> None:
        self.assertIn("tone(marks['net:' + plane.net], COPPER)", (ASSETS / "show.js").read_text())
        css = (ASSETS / "show.css").read_text()
        self.assertIn("max-height: 72vh; overflow-y: auto;", css)

    def test_board_outlines_keep_their_shape(self) -> None:
        for kind in ("SHAPE_T_ARC", "SHAPE_T_RECT", "SHAPE_T_CIRCLE", "SHAPE_T_POLY"):
            self.assertIn(kind, SECTION_SCRIPT)

    def test_her_screen_opens_the_viewer_on_a_new_version(self) -> None:
        self.assertIn('src="/display.js"', (ASSETS / "index.html").read_text())
        script = (ASSETS / "display.js").read_text()
        self.assertIn("/display.json", script)
        self.assertIn("'/show'", script)

    def test_the_server_serves_the_display_and_viewer(self) -> None:
        source = (REPOSITORY_ROOT / "src/alx/interfaces/server.py").read_text()
        for route in ('"/display.json"', '"/show": "show.html"', '"/show.js"', '"/show.css"', '"/display.js"'):
            self.assertIn(route, source)


if __name__ == "__main__":
    unittest.main()
