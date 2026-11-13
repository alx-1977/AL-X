"""She shows Friedl a board over the reader wave (D-073)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.records import freeze_data  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.providers.kicad_section import SECTION_SCRIPT, SectionError, read_section  # noqa: E402
from alx.providers.page_image import PageError, render_section  # noqa: E402
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


# Page-rendering tests show a datasheet without a measured comparison.
RENDERING_ONLY = {"no_layout": "a page-rendering test, nothing compared"}


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
        self.show = build_display_executors(self.board, section, lambda: "call-1",
                                            draw_wait_seconds=0.01)[SHOW_ON_SCREEN]

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

    def test_frozen_views_serialize_through_the_asset_handshake(self) -> None:
        payload = display()
        view = payload["views"][0]
        view["planes"] = ["GND"]
        view["dims"] = [{"from": [150.2, 119.6], "to": [160, 119.6],
                         "text": "9.8 mm", "tone": "info"}]
        # Capability arguments arrive deeply frozen, including each dimension.
        arguments = freeze_data(payload)
        result = self.show(arguments)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        supplied = []

        def snapshot():
            value = self.board.snapshot()
            supplied.append(value)
            return value

        server = LiveVoiceServer(session=None, host="127.0.0.1", port=0,
                                 sample_rate_hz=16_000, asset_root=ASSETS, display=snapshot)
        response = server._serve_asset(None, SimpleNamespace(path="/display.json"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), json.loads(json.dumps(supplied[0])))
        shown = supplied[0]["display"]
        for key in ("title", "subtitle", "verdict", "items"):
            self.assertEqual(shown[key], payload[key])
        self.assertEqual(shown["views"][0]["marks"], view["marks"])
        self.assertEqual(shown["views"][0]["dims"], view["dims"])
        self.assertEqual(shown["views"][0]["geometry"], GEOMETRY)
        for key in ("board", "window", "refs", "planes"):
            self.assertEqual(self.read[0][key], arguments["views"][0][key])
        # Mutating a frontend snapshot must not alter the stored display or input.
        shown["views"][0]["dims"][0]["from"][0] = 0
        self.assertEqual(self.board.snapshot()["display"]["views"][0]["dims"], view["dims"])
        self.assertEqual(arguments["views"][0]["dims"][0]["from"][0], 150.2)

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
        show = build_display_executors(self.board, broken, lambda: "call-1",
                                       draw_wait_seconds=0.01)[SHOW_ON_SCREEN]
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

    def test_it_is_shown_only_when_the_screen_reports_drawing_it(self) -> None:
        # 2026-11-10: the screen had crashed and she said it was on screen.
        self.assertFalse(self.show(display()).values["shown"])
        board_id = self.board.snapshot()["id"]

        def screen():
            while self.board.snapshot()["version"] < 2:
                time.sleep(0.005)
            self.board.mark_drawn(board_id, 2)

        drawer = threading.Thread(target=screen)
        drawer.start()
        show = build_display_executors(self.board, lambda _: GEOMETRY, lambda: "call-2",
                                       draw_wait_seconds=5)[SHOW_ON_SCREEN]
        self.assertTrue(show(display()).values["shown"])
        drawer.join()

    def test_a_report_from_another_run_or_a_future_version_counts_for_nothing(self) -> None:
        self.board.publish({"title": "x"})
        self.board.mark_drawn("someone-else", 1)
        self.board.mark_drawn(self.board.snapshot()["id"], 5)
        self.assertFalse(self.board.wait_drawn(1, 0.01))
        self.board.mark_drawn(self.board.snapshot()["id"], 1)
        self.assertTrue(self.board.wait_drawn(1, 0.01))

    def test_the_viewer_reports_after_drawing_and_the_server_takes_it(self) -> None:
        script = (ASSETS / "show.js").read_text()
        drawn_at = script.index("/display-drawn.json")
        self.assertGreater(drawn_at, script.index("svg.setAttribute('viewBox'"))
        source = (REPOSITORY_ROOT / "src/alx/interfaces/server.py").read_text()
        self.assertIn('parsed.path == "/display-drawn.json"', source)
        wiring = (REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertIn("display_drawn=display_board.mark_drawn", wiring)

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


def run_show_js(test, script, *arguments):
    """Run a script against show.js's pure functions in node."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        test.skipTest("node is not installed")
    prelude = "const show = require(process.argv[1]);\n"
    result = subprocess.run([node, "-e", prelude + script, str(ASSETS / "show.js"), *arguments],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


class ScreenTruthTests(unittest.TestCase):
    """12 Nov: a guessed net name drew nothing, and she showed it anyway."""

    def setUp(self) -> None:
        self.board = DisplayBoard()

    def show_with(self, geometry, capture=None, drawn=True):
        def section(_):
            return geometry
        show = build_display_executors(self.board, section, lambda: "c1", draw_wait_seconds=2,
                                       capture=capture)[SHOW_ON_SCREEN]
        if drawn:
            def screen():
                while self.board.snapshot()["version"] < 1:
                    time.sleep(0.005)
                self.board.mark_drawn(self.board.snapshot()["id"], 1)
            threading.Thread(target=screen).start()
        return show(display())

    def test_names_not_on_the_board_are_refused_and_nothing_is_shown(self) -> None:
        geometry = {**GEOMETRY, "missing": {"refs": ["C99"], "nets": ["Net-(AE2-Feed_Low)"], "planes": []}}
        result = self.show_with(geometry, drawn=False)
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("Net-(AE2-Feed_Low)", result.failure["reason"])
        self.assertIn("C99", result.failure["reason"])
        self.assertIsNone(self.board.snapshot()["display"])

    def test_the_screen_as_drawn_comes_back_to_her_by_digest(self) -> None:
        asked = []

        def capture(board_id, version):
            asked.append((board_id, version))
            return "f" * 64
        result = self.show_with({**GEOMETRY, "nets_drawn": ["GND", "Net-(AE2-Feed_Low_Band)"]}, capture)
        self.assertTrue(result.values["shown"])
        self.assertEqual(result.values["image_sha256"], "f" * 64)
        self.assertEqual(asked, [(self.board.snapshot()["id"], 1)])
        self.assertEqual(list(result.values["nets_drawn"][0]), ["GND", "Net-(AE2-Feed_Low_Band)"])

    def test_no_picture_is_claimed_when_none_was_taken(self) -> None:
        def broken(*_):
            raise OSError("no browser")
        for capture, drawn in ((broken, True), (lambda *_: None, True), (lambda *_: "f" * 64, False)):
            with self.subTest(drawn=drawn):
                self.board = DisplayBoard()
                result = self.show_with(GEOMETRY, capture, drawn)
                self.assertNotIn("image_sha256", result.values)
                self.assertEqual(result.values["screen_image"], "not captured")

    def test_the_section_read_reports_missing_names_and_draws_every_connection(self) -> None:
        self.assertIn('out["missing"] = {"refs": sorted(refs - present_refs)', SECTION_SCRIPT)
        self.assertIn('if fp.GetReference() in refs:', SECTION_SCRIPT)
        self.assertIn('not pad.GetNetname().startswith("unconnected-")', SECTION_SCRIPT)
        self.assertIn("pcbnew.F_SilkS, pcbnew.B_SilkS", SECTION_SCRIPT)
        script = (ASSETS / "show.js").read_text()
        self.assertIn("for (const mark of geo.silk || [])", script)

    def test_a_capture_draws_but_never_reports_the_screen_as_drawn(self) -> None:
        script = (ASSETS / "show.js").read_text()
        guard = script.index("if (asked.has('capture')) return;")
        self.assertIn("asked.get('capture') !== id || Number(asked.get('version')) !== version", script)
        self.assertLess(guard, script.index("/display-drawn.json"))

    def test_the_capture_loads_only_her_viewer_and_keeps_the_picture(self) -> None:
        from alx.providers.look_image import png
        from alx.providers.screen_capture import capture_screen
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "chrome"
            browser.write_text("")
            runs = []

            def execute(tool, arguments, folder):
                runs.append(list(arguments))
                (Path(folder) / "screen.png").write_bytes(png(4, 3, b"\x00" * 36))
                return SimpleNamespace(returncode=0)
            digest = capture_screen("run-1", 7, base_url="http://127.0.0.1:8765", browser=browser,
                                    store=root / "looks", scratch_root=root / "scratch", execute=execute)
            self.assertTrue((root / "looks" / (digest + ".png")).is_file())
            self.assertEqual(runs[0][-1], "http://127.0.0.1:8765/show?capture=run-1&version=7")
            self.assertIn("--window-size=1568,980", runs[0])
            self.assertEqual(list((root / "scratch").iterdir()), [])
            self.assertIsNone(capture_screen("run-1", 7, base_url="x", browser=root / "missing",
                                             store=root / "looks", scratch_root=root / "scratch",
                                             execute=execute))

    def test_a_picture_of_a_replaced_display_is_not_returned(self) -> None:
        # Review of 8a5d1f0: another show could replace it while Chrome starts.
        def capture(board_id, version):
            self.board.publish({"title": "newer"})
            return "f" * 64
        result = self.show_with(GEOMETRY, capture)
        self.assertNotIn("image_sha256", result.values)

    def test_the_drawing_settles_where_it_fits_and_fills_and_is_drawn_there_last(self) -> None:
        # Review of 8a5d1f0: the third pass could change the scale without drawing.
        cases = [
            # area, board mm, label room that grows a little with scale
            {"area": 1100, "mm": 50, "fixed": 300, "per": 0.0},
            {"area": 1100, "mm": 50, "fixed": 250, "per": 4.0},
            {"area": 600, "mm": 120, "fixed": 400, "per": 0.5},
        ]
        results = run_show_js(self, (
            "const cases = JSON.parse(process.argv[2]);\n"
            "console.log(JSON.stringify(cases.map((c) => {\n"
            "  const drawn = [];\n"
            "  const fit = (S) => { drawn.push(S); return (c.area - c.fixed - c.per * S) / c.mm; };\n"
            "  const S = show.settleScale(1, fit);\n"
            "  return {S, last: drawn[drawn.length - 1], best: (c.area - c.fixed) / (c.mm + c.per)};\n"
            "})));"), json.dumps(cases))
        for result in results:
            self.assertEqual(result["S"], result["last"])
            self.assertLessEqual(result["S"] * 1.0, result["best"] + 1e-9)
            self.assertGreater(result["S"], result["best"] * 0.95)
        script = (ASSETS / "show.js").read_text()
        self.assertIn("settleScale(fitScale(area.width, area.height, views.map(extent)), drawAndFit);", script)
        self.assertIn("const clip = 'clip' + (clipCount += 1);", script)


class TwoViewTests(unittest.TestCase):
    """Friedl, 12 Nov: two views are headed Current and Amended; silk under pads."""

    def test_two_views_are_headed_current_and_amended(self) -> None:
        board = DisplayBoard()
        show = build_display_executors(board, lambda _: GEOMETRY, lambda: "c1",
                                       draw_wait_seconds=0.01)[SHOW_ON_SCREEN]
        two = display(views=[{**display()["views"][0], "label": "Your board"},
                             {**display()["views"][0], "label": "My copy", "sublabel": "Würth p.2"}])
        self.assertEqual(show(two).state, CapabilityResultState.SUCCEEDED)
        views = board.snapshot()["display"]["views"]
        self.assertEqual([(v["label"], v["sublabel"]) for v in views],
                         [("Current", "Your board"), ("Amended", "My copy · Würth p.2")])
        show(display())
        self.assertEqual(board.snapshot()["display"]["views"][0]["label"], "Yours")

    def test_a_heading_her_words_only_repeat_is_not_shown_twice(self) -> None:
        board = DisplayBoard()
        show = build_display_executors(board, lambda _: GEOMETRY, lambda: "c1",
                                       draw_wait_seconds=0.01)[SHOW_ON_SCREEN]
        two = display(views=[{**display()["views"][0], "label": "Current"},
                             {**display()["views"][0], "label": "AMENDED", "sublabel": "Würth p.2"}])
        show(two)
        views = board.snapshot()["display"]["views"]
        self.assertEqual([(v["label"], v["sublabel"]) for v in views],
                         [("Current", ""), ("Amended", "Würth p.2")])

    def test_unframed_views_share_one_frame(self) -> None:
        # 12 Nov: five different windows in one day made every screen look different.
        board = DisplayBoard()
        asked = []

        def section(arguments):
            asked.append(arguments["window"])
            if arguments["window"] is None:
                own = [0, 0, 10, 10] if len(asked) == 1 else [2, -1, 12, 9]
                return {**GEOMETRY, "window": own}
            return {**GEOMETRY, "window": list(arguments["window"])}
        show = build_display_executors(board, section, lambda: "c1", draw_wait_seconds=0.01)[SHOW_ON_SCREEN]
        view = {key: value for key, value in display()["views"][0].items() if key != "window"}
        self.assertEqual(show(display(views=[view, {**view, "label": "Copy"}])).state,
                         CapabilityResultState.SUCCEEDED)
        self.assertEqual(asked, [None, None, [0, -1, 12, 10], [0, -1, 12, 10]])
        frames = [v["geometry"]["window"] for v in board.snapshot()["display"]["views"]]
        self.assertEqual(frames[0], frames[1])
        # A window she names is kept as she named it.
        asked.clear()
        show(display())
        self.assertEqual(asked, [[133, 113, 181, 138]])

    def test_the_section_frames_named_parts_and_draws_named_signal_pours(self) -> None:
        self.assertIn('if args["window"] is None:', SECTION_SCRIPT)
        self.assertIn("fp.GetBoundingBox(False, False)", SECTION_SCRIPT)
        self.assertIn('zone.GetNetname() in named_nets and zone.GetNetname() not in planes', SECTION_SCRIPT)
        script = (ASSETS / "show.js").read_text()
        self.assertIn("let x = Math.max(0, (area.width - total) / 2);", script)

    def test_the_real_section_script_frames_and_sorts_pours_on_a_real_board(self) -> None:
        # Review of fd11712: run SECTION_SCRIPT itself on a small KiCad board.
        # Parts R1 and R2 (pads 9.4-15.5 x 9.7-10.3 mm), a SIG track and small
        # SIG pour on F, a GND plane on B. Needs KiCad's Python; skipped without it.
        import subprocess
        python = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
        if not python.is_file():
            self.skipTest("KiCad is not installed")
        board = REPOSITORY_ROOT / "tests/fixtures/section_board.kicad_pcb"

        def run(executable, script, copy):
            return subprocess.run([executable, str(script), str(copy)], capture_output=True, text=True,
                                  cwd=script.parent, timeout=120)
        with tempfile.TemporaryDirectory() as scratch:
            def read(**arguments):
                return read_section(board, {"window": None, "refs": ["R1", "R2"], **arguments},
                                    scratch_root=Path(scratch), kicad_python=python, execute_python=run)
            signal = read(nets=["SIG"], planes=["GND"])
            # 2 mm margin, outwards to the half millimetre.
            self.assertEqual(signal["window"], [7.0, 7.5, 17.5, 12.5])
            self.assertEqual([(s["net"], s["layer"]) for s in signal["shapes"]], [("SIG", "F")])
            self.assertEqual({(p["net"], p["layer"]) for p in signal["planes"]}, {("GND", "B")})
            both = read(nets=["SIG"], planes=["GND", "SIG"])
            self.assertEqual(both["shapes"], [])
            self.assertIn(("SIG", "F"), {(p["net"], p["layer"]) for p in both["planes"]})
            wrong = read_section(board, {"window": None, "refs": ["R1", "R9"], "nets": ["NOPE"],
                                         "planes": []}, scratch_root=Path(scratch),
                                 kicad_python=python, execute_python=run)
            self.assertEqual((wrong["missing"]["refs"], wrong["missing"]["nets"]), (["R9"], ["NOPE"]))

    def test_every_read_is_checked_for_names_not_on_the_board(self) -> None:
        # Review of fd11712: the board can change between the first and shared-frame reads.
        board = DisplayBoard()
        reads = []

        def section(arguments):
            reads.append(arguments["window"])
            if arguments["window"] is None:
                return {**GEOMETRY, "window": [0, 0, 10, 10] if len(reads) == 1 else [1, 1, 11, 11]}
            return {**GEOMETRY, "window": list(arguments["window"]), "missing": {"refs": ["C61"]}}
        show = build_display_executors(board, section, lambda: "c1", draw_wait_seconds=0.01)[SHOW_ON_SCREEN]
        view = {key: value for key, value in display()["views"][0].items() if key != "window"}
        result = show(display(views=[view, {**view, "label": "Copy"}]))
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("C61", result.failure["reason"])
        self.assertIsNone(board.snapshot()["display"])

    def test_silkscreen_lies_under_the_pads(self) -> None:
        script = (ASSETS / "show.js").read_text()
        self.assertLess(script.index("for (const mark of geo.silk || [])"),
                        script.index("for (const pad of geo.pads || []) {\n      el('path'"))
        self.assertNotIn("changed_tone", script)


class BoardDrawingTests(unittest.TestCase):
    """Friedl, 11 Nov 2026: a name sits beside its part; no leader lines."""

    # The renderer's own rule: free, or asked for a cost, the area covered.
    FREE = ("const obstacles = JSON.parse(process.argv[2]);\n"
            "const free = (b, cost = false) => cost ? obstacles.reduce((sum, o) => sum"
            " + Math.max(0, Math.min(b.x + b.w, o.x + o.w) - Math.max(b.x, o.x))"
            " * Math.max(0, Math.min(b.y + b.h, o.y + o.h) - Math.max(b.y, o.y)), 0)"
            " : !obstacles.some((o) => show.overlap(b, o, show.CLEAR));\n")

    def test_a_name_sits_right_beside_a_small_part(self):
        spot = run_show_js(self, self.FREE + (
            "console.log(JSON.stringify(show.nameSpot({x: 100, y: 100, w: 10, h: 20, large: false}, 24, 14, free)));"),
            "[]")
        self.assertEqual(spot, [114, 103])

    def test_a_crowded_name_takes_the_nearest_free_place_without_a_line(self):
        # Copper right, left, above and below the part, as at C61 on P0020.
        obstacles = [{"x": 114, "y": 90, "w": 60, "h": 40}, {"x": 40, "y": 90, "w": 56, "h": 40},
                     {"x": 90, "y": 70, "w": 30, "h": 26}, {"x": 90, "y": 124, "w": 30, "h": 26}]
        spot = run_show_js(self, self.FREE + (
            "const s = show.nameSpot({x: 100, y: 100, w: 10, h: 20, large: false}, 24, 14, free);\n"
            "console.log(JSON.stringify({s, free: free({x: s[0], y: s[1], w: 24, h: 14})}));"),
            json.dumps(obstacles))
        self.assertTrue(spot["free"])
        centre = (spot["s"][0] + 12, spot["s"][1] + 7)
        self.assertLess(abs(centre[0] - 105) + abs(centre[1] - 110), 120)

    def test_when_nowhere_is_free_the_least_covered_place_is_chosen_not_the_first(self):
        # Review of 6474114: the first try was returned even when rejected.
        result = run_show_js(self, self.FREE + (
            "const body = {x: 100, y: 100, w: 10, h: 20, large: false};\n"
            "const s = show.nameSpot(body, 24, 14, free);\n"
            "const cost = ([x, y]) => free({x, y, w: 24, h: 14}, true);\n"
            "const d = show.dimensionSpot([0, 300], [100, 300], 40, 14, free);\n"
            "console.log(JSON.stringify({chosen: cost(s), first: cost([114, 103]),"
            " dim: free({x: d[0], y: d[1], w: 40, h: 14}, true), dimFirst: free({x: 30, y: 277, w: 40, h: 14}, true)}));"),
            json.dumps([{"x": 0, "y": 0, "w": 400, "h": 400}, {"x": 400, "y": 0, "w": 100, "h": 100}]))
        # Everything is covered somewhere; the choice covers less than the first try.
        self.assertLess(result["chosen"], result["first"])
        self.assertLessEqual(result["dim"], result["dimFirst"])

    def test_a_narrow_screen_shrinks_the_spacing_not_the_boards_to_nothing(self):
        # Review of 6474114: below ~578 px the scale went negative.
        scales = run_show_js(self, (
            "const sizes = [[25.5, 47], [25.5, 47]];\n"
            "console.log(JSON.stringify([show.fitScale(400, 600, sizes), show.fitScale(1120, 766, sizes)]));"))
        narrow, wide = scales
        self.assertGreater(narrow, 1)
        self.assertAlmostEqual(wide, min((1120 - 370) / 51, (766 - 72) / 47))

    def test_a_wide_datasheet_section_fits_the_width(self):
        # Review of 6474114: a wide section ran past the right edge.
        width = run_show_js(self, (
            "console.log(JSON.stringify(show.fitReference(1000, 800, [{w: 800, h: 100}], 18)));"))
        self.assertEqual(width, 1000 - 2 * 190)
        tall = run_show_js(self, (
            "console.log(JSON.stringify(show.fitReference(1000, 800, [{w: 200, h: 300}], 18)));"))
        self.assertAlmostEqual(tall, 800 * 200 / 300)

    def test_only_what_is_on_screen_is_named(self):
        # Review of 6474114: names went to the off-screen centre of a part.
        source = (ASSETS / "show.js").read_text()
        self.assertIn("if (right > left && bottom > top) {", source)
        self.assertIn("for (const [ref, body] of Object.entries(named)) {", source)

    def test_a_large_part_is_named_inside(self):
        spot = run_show_js(self, self.FREE + (
            "console.log(JSON.stringify(show.nameSpot({x: 0, y: 0, w: 100, h: 400, large: true}, 30, 14, free)));"),
            "[]")
        self.assertEqual(spot, [35, 193])

    def test_dimension_text_is_level_beside_the_middle_of_its_line(self):
        spots = run_show_js(self, self.FREE + (
            "console.log(JSON.stringify([show.dimensionSpot([0, 50], [100, 50], 40, 14, free),"
            " show.dimensionSpot([50, 0], [50, 100], 40, 14, free)]));"), "[]")
        self.assertEqual(spots, [[30, 27], [60, 43]])
        source = (ASSETS / "show.js").read_text()
        self.assertNotIn("rotate(", source)

    def test_only_one_board_drawing_path_exists(self):
        # Law 0: the collision router and its label columns are deleted, not kept.
        source = (ASSETS / "show.js").read_text()
        for gone in ("placeLabels", "leaderRouter", "label_layout_exhausted", "displaced"):
            self.assertNotIn(gone, source)
        self.assertEqual(source.count("function nameSpot("), 1)
        self.assertEqual(source.count("function dimensionSpot("), 1)
        self.assertEqual(source.count("function view("), 1)


class ReferencePageTests(unittest.TestCase):
    """The datasheet section is shown as printed, behind a link (D-073, 11 Nov)."""

    SECTION = [575.3, 87.7, 783.4, 391.7]

    def setUp(self) -> None:
        self.board = DisplayBoard()
        self.pages = []

        def page(arguments):
            self.pages.append(dict(arguments))
            return b"\x89PNG\r\n\x1a\n" + bytes([len(self.pages)])
        self.show = build_display_executors(self.board, lambda _: GEOMETRY, lambda: "call-1",
                                            draw_wait_seconds=0.01, read_page=page)[SHOW_ON_SCREEN]

    def reference(self, **changes):
        values = {"label": "Würth 7488918022", "sublabel": "Datasheet p.2, as printed",
                  "document": "/designs/P0020/datasheets/ae1.pdf",
                  "pages": [{"page": 2, "section": self.SECTION, "caption": "Layout", "boxes": [
                      {"at": [623.2, 259.5, 718.9, 292.9], "label": "feed + ground line", "side": "left"},
                      {"at": [700, 380, 800, 400], "label": "yours 0.32"}]}]}
        values.update(changes)
        return values

    def test_boxes_become_places_on_the_image_and_the_page_is_read_once(self):
        result = self.show(display(reference=self.reference(), checked=RENDERING_ONLY))
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values["reference_pages"], 1)
        self.assertEqual(self.pages, [{"document": "/designs/P0020/datasheets/ae1.pdf", "page": 2,
                                       "section": self.SECTION}])
        shown = self.board.snapshot()["display"]["reference"]
        page = shown["pages"][0]
        self.assertEqual((page["asset"], round(page["w"], 1), round(page["h"], 1)), (0, 208.1, 304.0))
        first, second = page["boxes"]
        self.assertAlmostEqual(first["x"], (623.2 - 575.3) / 208.1)
        self.assertAlmostEqual(first["h"], (292.9 - 259.5) / 304.0)
        self.assertEqual((first["tone"], first["side"]), ("bad", "left"))
        # A box running past the section is cut at its edge.
        self.assertAlmostEqual(second["x"] + second["w"], 1.0)
        self.assertAlmostEqual(second["y"] + second["h"], 1.0)
        # The image itself stays out of the display her screen polls.
        self.assertNotIn("PNG", json.dumps(self.board.snapshot()))

    def test_images_are_served_for_the_version_on_screen_only(self):
        self.show(display(reference=self.reference(), checked=RENDERING_ONLY))
        board_id = self.board.snapshot()["id"]
        server = LiveVoiceServer(session=None, host="127.0.0.1", port=0, sample_rate_hz=16_000,
                                 asset_root=ASSETS, display=self.board.snapshot,
                                 display_asset=self.board.asset)

        def get(query):
            return server._serve_asset(None, SimpleNamespace(path="/display-asset.png?" + query))
        served = get(f"id={board_id}&version=1&n=0")
        self.assertEqual(served.status_code, 200)
        self.assertTrue(served.body.startswith(b"\x89PNG"))
        self.assertEqual(served.headers["Content-Type"], "image/png")
        for query in (f"id=other&version=1&n=0", f"id={board_id}&version=1&n=1",
                      f"id={board_id}&version=x&n=0"):
            with self.subTest(query=query):
                self.assertEqual(get(query).status_code, 404)
        self.show(display())
        self.assertEqual(get(f"id={board_id}&version=1&n=0").status_code, 404)

    def test_a_bad_reference_is_refused_before_anything_is_read(self):
        page = self.reference()["pages"][0]
        for bad in (
            self.reference(pages=[]),
            self.reference(pages=[page, page, page]),
            self.reference(pages=[{**page, "section": [10, 10, 5, 20]}]),
            self.reference(pages=[{**page, "boxes": [{"at": [0, 0, 10, 10], "label": "outside"}]}]),
            self.reference(pages=[{**page, "boxes": [{"at": [600, 100, 620, 120], "label": "x", "side": "up"}]}]),
            self.reference(pages=[{**page, "boxes": [{"at": [600, 100, 620, 120], "label": "x", "tone": "loud"}]}]),
            self.reference(document=""),
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.show(display(reference=bad)).failure["code"], "arguments_unusable")
        self.assertEqual(self.pages, [])
        self.assertEqual(self.board.snapshot()["version"], 0)

    def test_a_page_that_cannot_be_rendered_is_reported_and_nothing_is_shown(self):
        def broken(_):
            raise PageError("page_failed", "renderer stopped")
        show = build_display_executors(self.board, lambda _: GEOMETRY, lambda: "call-1",
                                       draw_wait_seconds=0.01, read_page=broken)[SHOW_ON_SCREEN]
        self.assertEqual(show(display(reference=self.reference(), checked=RENDERING_ONLY)).failure["code"], "page_failed")
        show = build_display_executors(self.board, lambda _: GEOMETRY, lambda: "call-1",
                                       draw_wait_seconds=0.01)[SHOW_ON_SCREEN]
        self.assertEqual(show(display(reference=self.reference(), checked=RENDERING_ONLY)).failure["code"], "unavailable")
        self.assertIsNone(self.board.snapshot()["display"])

    def test_titles_are_short(self):
        result = self.show(display(title="P0020 AE1 — yours vs Würth 7488918022 layout"))
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("title", result.failure["reason"])

    def test_the_viewer_opens_the_page_from_a_link_and_closes_back_to_the_boards(self):
        page = (ASSETS / "show.html").read_text()
        for element in ('id="reflink"', 'id="refpage"', 'id="refback"', 'id="refview"'):
            self.assertIn(element, page)
        script = (ASSETS / "show.js").read_text()
        self.assertIn("/display-asset.png?id=", script)
        self.assertIn("filter: 'url(#lineart)'", script)
        wiring = (REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertIn("display_asset=display_board.asset", wiring)
        self.assertIn("read_page=None if kicad_runtime.provider is None else kicad_runtime.provider.datasheet_section",
                      wiring)


class RenderSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.document = root / "designs" / "ae1.pdf"
        self.document.parent.mkdir()
        self.document.write_bytes(b"%PDF-1.4 datasheet")
        self.renderer = root / "pdftoppm"
        self.renderer.write_text("")
        self.scratch = root / "scratch"
        self.seen = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def execute(self, renderer, arguments, directory, output=b"\x89PNG\r\n\x1a\nimage"):
        self.seen.append((list(map(str, arguments)), Path(directory)))
        self.assertEqual((Path(directory) / "document.pdf").read_bytes(), b"%PDF-1.4 datasheet")
        (Path(directory) / "section.png").write_bytes(output)
        return SimpleNamespace(returncode=0, stderr="")

    def render(self, arguments, execute=None):
        return render_section(self.document, arguments, scratch_root=self.scratch,
                              renderer=self.renderer, execute=execute or self.execute)

    def test_one_section_of_one_page_is_rendered_from_a_copy_that_is_then_removed(self):
        image = self.render({"page": 2, "section": [72, 144, 216, 360]})
        self.assertTrue(image.startswith(b"\x89PNG"))
        arguments, directory = self.seen[0]
        # 2000 px on the long side: 216 points high at 666 dpi, capped at 600.
        self.assertEqual(arguments, ["-f", "2", "-l", "2", "-r", "600", "-x", "600", "-y", "1200",
                                     "-W", "1200", "-H", "1800", "-png", "-singlefile",
                                     "document.pdf", "section"])
        self.assertTrue(directory.is_relative_to(self.scratch))
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(self.document.read_bytes(), b"%PDF-1.4 datasheet")

    def test_bad_requests_are_refused_before_anything_runs(self):
        for arguments in ({"page": 0, "section": [0, 0, 1, 1]}, {"page": True, "section": [0, 0, 1, 1]},
                          {"page": 1, "section": [5, 0, 1, 1]}, {"page": 1, "section": [0, 0, 1]},
                          {"page": 1, "section": [0, 0, 4000, 10]}):
            with self.subTest(arguments=arguments), self.assertRaises(PageError) as caught:
                self.render(arguments)
            self.assertEqual(caught.exception.code, "arguments_unusable")
        with self.assertRaises(PageError) as caught:
            render_section(self.document.with_suffix(".txt"), {"page": 1, "section": [0, 0, 1, 1]},
                           scratch_root=self.scratch, renderer=self.renderer, execute=self.execute)
        self.assertEqual(caught.exception.code, "document_unavailable")
        self.assertEqual(self.seen, [])

    def test_what_is_not_an_image_is_not_shown(self):
        with self.assertRaises(PageError) as caught:
            self.render({"page": 1, "section": [0, 0, 100, 100]},
                        lambda r, a, d: self.execute(r, a, d, output=b"<html>"))
        self.assertEqual(caught.exception.code, "page_failed")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_the_renderer_runs_in_the_kicad_script_confinement(self):
        import os
        import pwd
        from unittest.mock import patch
        from alx.providers.coding_process import run_confined_converter
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("alx.providers.coding_process.kicad_containment_available", return_value=True), \
                patch("alx.providers.coding_process.run_coding_subprocess", return_value=result) as run:
            run_confined_converter("pdftoppm", ["-png", "document.pdf", "section"], self.scratch)
        argv = run.call_args.args[1]
        self.assertEqual(argv[:2], ["/usr/bin/sandbox-exec", "-p"])
        directory = json.dumps(str(self.scratch.resolve()))
        home = json.dumps(str(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()))
        for rule in ("(deny file-read* (subpath " + home + "))", "(deny file-write*)",
                     "(allow file-write* (subpath " + directory, "(deny network*)", "(deny process-fork)"):
            self.assertIn(rule, argv[2])
        self.assertEqual(argv[3:], ["pdftoppm", "-png", "document.pdf", "section"])
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], str(self.scratch.resolve()))
        with patch("alx.providers.coding_process.kicad_containment_available", return_value=False), \
                self.assertRaises(OSError):
            run_confined_converter("pdftoppm", [], self.scratch)


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

    def test_copper_shapes_and_the_board_area_are_read(self) -> None:
        # 11 Nov: Wurth's 1.2 mm feed was a copper shape and went undrawn.
        self.assertIn('drawing.GetLayer() in COPPER and drawing.GetClass() == "PCB_SHAPE"', SECTION_SCRIPT)
        self.assertIn('out["shapes"].append', SECTION_SCRIPT)
        self.assertIn("board.GetBoardPolygonOutlines(outline, False)", SECTION_SCRIPT)
        script = (ASSETS / "show.js").read_text()
        self.assertIn("geo.shapes", script)
        self.assertIn("geo.board", script)

    def test_her_screen_opens_the_viewer_on_a_new_version(self) -> None:
        self.assertIn('src="/display.js"', (ASSETS / "index.html").read_text())
        script = (ASSETS / "display.js").read_text()
        self.assertIn("/display.json", script)
        self.assertIn("'/show'", script)

    def test_the_server_serves_the_display_and_viewer(self) -> None:
        source = (REPOSITORY_ROOT / "src/alx/interfaces/server.py").read_text()
        for route in ('"/display.json"', '"/show": "show.html"', '"/show.js"', '"/show.css"', '"/display.js"'):
            self.assertIn(route, source)



class CheckedAgainstDatasheetTests(unittest.TestCase):
    """2026-11-13: a clean DRC was shown twice as a layout that matched Würth's.

    A view of her copy needs a layout check KiCad made of that copy; the
    screen shows what was measured, never values she typed.
    """

    CHECK = {"check_id": "c" * 32, "work_copy_id": "5a61", "current": True, "passed": False,
             "document": "7488912455.pdf", "document_path": "/designs/P0020/datasheets/7488912455.pdf",
             "page": 2, "section": [200, 80, 600, 470], "part": "AE2", "rf": True, "confirmed": False,
             "counts": {"matches": 1, "differs": 1, "unaccounted": 1, "not_applicable": 1},
             "numbers": [
                 {"id": "a", "value": 1.46, "status": "matches", "measures": "AE2.1 width",
                  "board_mm": 1.46, "at": [280, 438, 290, 450]},
                 {"id": "b", "value": 1.9, "status": "differs", "measures": "C62 → C64 (x)",
                  "board_mm": 2.1, "at": [324, 224, 338, 236]},
                 {"id": "c", "value": 4.9, "status": "unaccounted", "at": [524, 321, 538, 333]},
                 {"id": "d", "value": 1.8, "status": "not_applicable", "reason": "part value (pF)",
                  "at": [508, 189, 522, 201]}]}

    def setUp(self) -> None:
        self.board = DisplayBoard()
        self.stored = {self.CHECK["check_id"]: dict(self.CHECK)}
        self.show = build_display_executors(
            self.board, lambda _: GEOMETRY, lambda: "c1", draw_wait_seconds=0.01,
            read_page=lambda request: b"\x89PNG\r\n\x1a\n", read_layout_check=self.stored.get,
        )[SHOW_ON_SCREEN]
        his = display()["views"][0]
        mine = {key: value for key, value in his.items() if key != "board"}
        self.views = [his, {**mine, "label": "Copy", "work_copy_id": "5a61"}]

    def reason(self, arguments):
        result = self.show(arguments)
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        return result.failure["reason"]

    def test_her_copy_is_not_shown_without_a_layout_check_of_it(self) -> None:
        self.assertIn("needs checked", self.reason(display(views=self.views)))
        self.assertIn("no layout check", self.reason(display(views=self.views, checked={"layout_check": "x"})))
        # His board alone needs none.
        self.assertEqual(self.show(display()).state, CapabilityResultState.SUCCEEDED)

    def test_a_check_of_another_copy_or_an_older_state_is_refused(self) -> None:
        checked = {"layout_check": self.CHECK["check_id"]}
        self.stored[self.CHECK["check_id"]]["work_copy_id"] = "other"
        self.assertIn("not of the copy on screen", self.reason(display(views=self.views, checked=checked)))
        self.stored[self.CHECK["check_id"]].update(work_copy_id="5a61", current=False)
        self.assertIn("changed after", self.reason(display(views=self.views, checked=checked)))

    def test_what_kicad_measured_is_shown_and_boxed_on_the_page(self) -> None:
        checked = {"layout_check": self.CHECK["check_id"]}
        ok = {"text": "Matches Würth", "tone": "ok"}
        self.assertIn("did not pass", self.reason(display(views=self.views, checked=checked, verdict=ok)))
        self.assertEqual(self.show(display(views=self.views, checked=checked)).state,
                         CapabilityResultState.SUCCEEDED)
        shown = self.board.snapshot()["display"]
        rows = shown["checked"]["rows"]
        # Differs first, then not accounted for, then matches; part values left out.
        self.assertEqual([(row["tone"], row["board_mm"]) for row in rows],
                         [("bad", 2.1), ("warn", None), ("ok", 1.46)])
        self.assertIn("readings not yet confirmed by Friedl", shown["checked"]["summary"])
        boxes = shown["reference"]["pages"][0]["boxes"]
        self.assertEqual([(box["tone"], box["label"]) for box in boxes], [("bad", "board 2.10"), ("warn", "?")])

    def test_no_layout_is_said_plainly(self) -> None:
        self.assertEqual(self.show(display(views=self.views, checked={
            "no_layout": "The datasheet gives no layout for C12"})).state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(self.board.snapshot()["display"]["checked"]["no_layout"],
                         "The datasheet gives no layout for C12")
        self.assertIn("not both", self.reason(display(views=self.views, checked={
            "no_layout": "none", "layout_check": self.CHECK["check_id"]})))

    def test_a_datasheet_beside_her_typed_comparison_is_refused(self) -> None:
        """13 Nov: U.FL readings typed into items under a green verdict, no measurement."""
        typed = display(views=[], reference={"label": "636101111001", "document": "/designs/umrf.pdf",
                                              "pages": [{"page": 1, "section": [0, 0, 100, 100]}]},
                        verdict={"text": "Footprint matches on every dimension", "tone": "ok"},
                        items=[{"title": "Signal pad 1.0 x 1.05", "tone": "ok"}])
        self.assertIn("never type readings", DEFINITIONS[0].purpose)
        self.assertIn("needs checked", self.reason(typed))
        # With KiCad's measurement of the footprint (no board view on screen) it is shown.
        self.stored[self.CHECK["check_id"]].update(passed=True, counts={"matches": 1})
        self.assertEqual(self.show(dict(typed, checked={"layout_check": self.CHECK["check_id"]})).state,
                         CapabilityResultState.SUCCEEDED)

    def test_the_screen_draws_the_rows(self) -> None:
        script = (ASSETS / "show.js").read_text()
        self.assertIn("Checked against", script)
        self.assertIn("Not checked against a datasheet", script)
        self.assertIn('id="checked"', (ASSETS / "show.html").read_text())

if __name__ == "__main__":
    unittest.main()
