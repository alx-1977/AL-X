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
from alx.contracts.records import freeze_data  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
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


class BoardLabelLayoutTests(unittest.TestCase):
    def render(self, rotate=False, view=None):
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        # The real asset runs with SVG font metrics supplied by this DOM double.
        # Negative glyph bearings catch incorrect baseline/box conversions.
        script = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const api = { module: { exports: {} } };
vm.runInNewContext(source, api);
const all = [];
// Model the cascade relevant to these labels using the loaded stylesheet:
// svg text overrides presentation attributes, while inline styles win.
const css = fs.readFileSync(process.argv[3], 'utf8');
const svgFont = css.match(/svg text\s*\{[^}]*font-family:\s*([^;}]+)/)[1];
const font = n => String(n.attrs.style || '').match(/font-family:\s*([^;]+)/)?.[1] || svgFont;
function element(tag) {
  const n = { tag, attrs: {}, children: [], textContent: '',
    setAttribute(k,v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { if (c.parent) c.parent.children = c.parent.children.filter(x => x !== c);
      this.children.push(c); c.parent = this; },
    replaceChildren() { this.children = []; }, append() {}, addEventListener() {},
    getBBox() { const size = Number(this.attrs['font-size']);
      return { x: -1.5, y: -size * .8, width: this.textContent.length * size * .8, height: size }; }
  };
  all.push(n); return n;
}
const ids = {};
const display = JSON.parse(process.argv[2]);
const context = { document: { getElementById(id) { return ids[id] ||= element(id); },
  createElementNS(ns, tag) { return element(tag); }, createElement: element },
  window: { addEventListener() {}, parent: { postMessage() { throw Error('viewer closed'); } } },
  fetch: async () => ({ json: async () => ({ display }) }) };
vm.runInNewContext(source, context);
setImmediate(() => {
  const texts = all.filter(n => n.tag === 'text').map(n => {
    const m = n.getBBox();
    let box = { x: Number(n.attrs.x) + m.x, y: Number(n.attrs.y) + m.y, width: m.width, height: m.height };
    const rotation = String(n.attrs.transform || '').match(/rotate\(([-.0-9]+),([-.0-9]+),([-.0-9]+)\)/);
    const angle = rotation ? Number(rotation[1]) : 0;
    if (rotation) {
      const [a, cx, cy] = rotation.slice(1).map(Number), r = a * Math.PI / 180;
      const points = [box.x, box.x + box.width].flatMap(x => [box.y, box.y + box.height].map(y =>
        [cx + (x-cx)*Math.cos(r) - (y-cy)*Math.sin(r), cy + (x-cx)*Math.sin(r) + (y-cy)*Math.cos(r)]));
      const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
      box = { x: Math.min(...xs), y: Math.min(...ys), width: Math.max(...xs)-Math.min(...xs), height: Math.max(...ys)-Math.min(...ys) };
    }
    return { angle, text: n.textContent, size: n.attrs['font-size'],
      font: font(n), hidden: n.attrs.visibility, clipped: !!n.parent.attrs['clip-path'],
      box };
  });
  const arrows = all.filter(n => n.tag === 'polygon').map(n => n.attrs.points.split(' ').map(p => p.split(',').map(Number)));
  const lines = all.filter(n => n.tag === 'line').map(n => n.attrs);
  const labels = texts.map(t => ({ kind: 'title', text: t.text, anchor: [100, 100], width: t.box.width, height: t.box.height }));
  const place = api.module.exports.placeLabels;
  const layout = place(labels, []);
  console.log(JSON.stringify({ texts, arrows, lines, layout,
    repeated: place(labels, []), reversed: place([...labels].reverse(), []),
    viewBox: ids.view.attrs.viewBox, transforms: ids.view.children.map(n => n.attrs.transform) }));
});
"""
        def pad(ref, x, y):
            return {"ref": ref, "polygons": [{"outer": [[x, y], [x+.6, y], [x+.6, y+.5], [x, y+.5]]}]}
        view = view or {"geometry": {"window": [142, 114, 162, 136], "pads": [
            pad("AE1", 148.7, 124), pad("C61", 149.2, 124.6), pad("L3", 149.6, 125)]},
            "rotate": rotate, "label": "Crowded board", "sublabel": "Readable dimensions",
            "dims": [
                {"from": [149.626, 122], "to": [149.626, 126.55], "text": "4.55"},
                {"from": [148, 124], "to": [149.2, 124], "text": "1.20"},
                {"from": [148.8, 124], "to": [148.8, 124.45], "text": "0.45"},
                {"from": [147.8, 122], "to": [147.8, 127], "text": "5.00"}]}
        result = subprocess.run([node, "-e", script, str(ASSETS / "show.js"),
                                 json.dumps({"views": [view, view]}), str(ASSETS / "show.css")],
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_offscreen_parts_do_not_change_labels_or_view_bounds(self):
        def pad(ref, box):
            x0, y0, x1, y1 = box
            return {"ref": ref, "polygons": [{"outer": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]}
        for rotate in (False, True):
            with self.subTest(rotate=rotate):
                view = {"geometry": {"window": [10, 20, 30, 40], "pads": []}, "rotate": rotate}
                empty = self.render(view=view)
                view["geometry"]["pads"] = [pad("OFF", [1000, -500, 1001, -499]),
                                             pad("TOUCH", [30, 25, 31, 26])]
                outside = self.render(view=view)
                self.assertEqual(outside["texts"], [])
                self.assertEqual(outside["viewBox"], empty["viewBox"])
                for full, clipped in (([5, 25, 12, 26], [10, 25, 12, 26]),
                                      ([28, 39, 35, 45], [28, 39, 30, 40])):
                    view["geometry"]["pads"] = [pad("EDGE", full)]
                    edge = self.render(view=view)
                    view["geometry"]["pads"] = [pad("EDGE", clipped)]
                    visible = self.render(view=view)
                    self.assertEqual(edge["texts"], visible["texts"])
                    self.assertEqual(edge["lines"], visible["lines"])
                    self.assertEqual(edge["viewBox"], visible["viewBox"])

    def test_reference_font_wins_over_the_loaded_svg_text_rule(self):
        for text in self.render()["texts"]:
            if text["text"] in ("AE1", "C61", "L3"):
                self.assertEqual(text["font"], '"Helvetica Neue",Arial,sans-serif')
            else:
                self.assertIn("Courier New", text["font"])

    def test_leaders_clear_all_text_including_the_unrotated_045_500_case(self):
        # Independently intersect each rendered segment with each ink box.
        def crosses(line, box):
            lo, hi = 0.0, 1.0
            for start, end, lower, size in ((line["x1"], line["x2"], box["x"], box["width"]),
                                             (line["y1"], line["y2"], box["y"], box["height"])):
                delta = end - start
                if abs(delta) < 1e-10:
                    if not lower <= start <= lower + size:
                        return False
                else:
                    a, b = sorted(((lower-start)/delta, (lower+size-start)/delta))
                    lo, hi = max(lo, a), min(hi, b)
                    if lo > hi:
                        return False
            return True
        for rotate in (False, True):
            result = self.render(rotate)
            for leader in result["lines"]:
                if leader.get("stroke-width") == .6:
                    for text in result["texts"]:
                        self.assertFalse(crosses(leader, text["box"]), (rotate, leader, text))
    def test_crowded_labels_clear_each_other_and_all_arrowheads(self):
        for rotate in (False, True):
            with self.subTest(rotate=rotate):
                result = self.render(rotate)
                # Two identical views have identical local layouts and disjoint
                # outer bounds. Every text kind is included in the same search.
                texts = result["texts"][:9]
                self.assertEqual(texts, result["texts"][9:])
                self.assertEqual(len(texts), 9)
                def overlaps(a, b):
                    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                            and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])
                for index, label in enumerate(texts):
                    self.assertGreaterEqual(label["size"], 12)
                    self.assertNotIn("hidden", label)
                    self.assertFalse(label["clipped"])
                    for other in texts[index+1:]:
                        expanded = {**label["box"], "x": label["box"]["x"]-4, "y": label["box"]["y"]-4,
                                    "width": label["box"]["width"]+8, "height": label["box"]["height"]+8}
                        self.assertFalse(overlaps(expanded, other["box"]), (label, other))
                    for arrow in result["arrows"][:8]:
                        xs, ys = zip(*arrow)
                        self.assertFalse(overlaps(label["box"], {"x": min(xs), "y": min(ys),
                            "width": max(xs)-min(xs), "height": max(ys)-min(ys)}))
                # Even short dimensions remain centred along their segment.
                # Clearance is perpendicular, with no endpoint callout.
                for dim in (
                    {"text": "4.55", "from": [149.626, 122], "to": [149.626, 126.55]},
                    {"text": "1.20", "from": [148, 124], "to": [149.2, 124]},
                    {"text": "0.45", "from": [148.8, 124], "to": [148.8, 124.45]},
                    {"text": "5.00", "from": [147.8, 122], "to": [147.8, 127]},
                ):
                    def point(p):
                        x, y = p
                        return ((y-114)*10, (162-x)*10) if rotate else ((x-142)*10, (y-114)*10)
                    a, b = point(dim["from"]), point(dim["to"])
                    text = next(t for t in texts if t["text"] == dim["text"])
                    box = text["box"]
                    cx, cy = box["x"]+box["width"]/2, box["y"]+box["height"]/2
                    dx, dy = b[0]-a[0], b[1]-a[1]
                    self.assertAlmostEqual((cx-(a[0]+b[0])/2)*dx + (cy-(a[1]+b[1])/2)*dy, 0)
                    self.assertTrue(-90 <= text["angle"] <= 90)
                    import math
                    self.assertAlmostEqual(math.sin(math.radians(text["angle"]))*dx -
                                           math.cos(math.radians(text["angle"]))*dy, 0)
                self.assertEqual(result["layout"], result["repeated"])
                self.assertEqual(result["layout"], result["reversed"])
                # The expanded viewBox encloses labels in both adjacent views.
                import re
                _, _, width, height = map(float, result["viewBox"].split())
                bounds = []
                for transform in result["transforms"]:
                    tx, ty = map(float, re.findall(r"[-0-9.]+", transform))
                    boxes = [t["box"] for t in texts]
                    self.assertTrue(all(b["x"]+tx >= 4 and b["y"]+ty >= 4
                        and b["x"]+tx+b["width"] <= width-4 and b["y"]+ty+b["height"] <= height-4 for b in boxes))
                    bounds.append((min(b["x"]+tx for b in boxes), max(b["x"]+tx+b["width"] for b in boxes)))
                self.assertLess(bounds[0][1], bounds[1][0])


    def test_preferred_sides_overflow_columns_and_dimension_angles(self):
        import math
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        ref = {"kind": "ref", "text": "R1", "anchor": [0, 0],
               "part": [-5, -5, 5, 5], "width": 20, "height": 12}
        cases = [
            {"labels": [ref], "obstacles": []},
            {"labels": [ref], "obstacles": [{"x": -40, "y": -25, "width": 80, "height": 14}]},
            {"labels": [{**ref, "text": f"R{i}", "anchor": [0, i*30],
                         "part": [-5, i*30-5, 5, i*30+5]} for i in range(4)],
             "obstacles": [{"x": -40, "y": -40, "width": 80, "height": 180, "copper": True}]},
            {"labels": [ref], "obstacles": [
                {"x": -40, "y": -40, "width": 80, "height": 80, "copper": True},
                {"x": -5, "y": -5, "width": 10, "height": 10, "dimension": True},
                {"x": 10, "y": -40, "width": 4, "height": 80, "owner": "R2"}]},
        ]
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (3, 4), (-3, -4), (-3, 4), (3, -4)):
            cases.append({"labels": [{"kind": "dim", "text": "0.45", "anchor": [0, 0],
                                      "width": 48, "height": 12, "direction": [dx/math.hypot(dx, dy), dy/math.hypot(dx, dy)]}],
                          "obstacles": [{"segment": [[-dx, -dy], [dx, dy]], "stroke": 1}]})
        script = """
const {placeLabels} = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(c => ({layout: placeLabels(c.labels, c.obstacles),
  again: placeLabels(c.labels, c.obstacles), reversed: placeLabels([...c.labels].reverse(), c.obstacles)}))));
"""
        result = json.loads(subprocess.run([node, "-e", script, str(ASSETS / "show.js"), json.dumps(cases)],
                            capture_output=True, text=True, check=True, timeout=30).stdout)
        for case in result:
            self.assertEqual(case["layout"], case["again"])
            self.assertEqual(case["layout"], case["reversed"])
        above, right = result[0]["layout"][0], result[1]["layout"][0]
        self.assertLess(above["box"]["y"] + above["box"]["height"], -5)
        self.assertGreater(right["box"]["x"], 5)
        for label in (above, right):
            self.assertEqual(label["angle"], 0)
            self.assertIsNone(label["leader"])
            self.assertFalse(label["displaced"])
        overflow = result[2]["layout"]
        self.assertEqual(len({l["box"]["x"] for l in overflow}), 1)
        for label in overflow:
            self.assertTrue(label["displaced"])
            self.assertTrue(label["leader"])
            self.assertEqual(label["angle"], 0)
            self.assertAlmostEqual(label["center"][1] % 20, 0)
        routed = result[3]["layout"][0]
        self.assertTrue(routed["displaced"])
        self.assertGreater(len(routed["leader"]), 2)
        for case, source in zip(result[4:], cases[4:]):
            label = case["layout"][0]
            dx, dy = source["labels"][0]["direction"]
            cx, cy = label["center"]
            self.assertAlmostEqual(cx*dx + cy*dy, 0)
            self.assertTrue(-90 <= label["angle"] <= 90)
            self.assertAlmostEqual(math.sin(math.radians(label["angle"]))*dx -
                                   math.cos(math.radians(label["angle"]))*dy, 0)
            self.assertIsNone(label["leader"])
            if dx == 0 or dy == 0:
                self.assertAlmostEqual(math.hypot(cx, cy), 11)
        for a, b in ((4, 5), (6, 7), (8, 9), (10, 11)):
            self.assertEqual(result[a]["layout"][0]["center"], result[b]["layout"][0]["center"])

    def test_every_label_clears_pads_and_full_dimension_segments(self):
        # Representative P0020 AE1 feed components and short dimensions, in both
        # orientations and both side-by-side views. Check rendered ink, not
        # the placement routine's own collision predicate.
        def crosses(a, b, box):
            low, high = 0, 1
            for axis, lower, size in ((0, box["x"], box["width"]), (1, box["y"], box["height"])):
                delta = b[axis]-a[axis]
                if abs(delta) < 1e-10:
                    if not lower <= a[axis] <= lower+size:
                        return False
                else:
                    ends = sorted(((lower-a[axis])/delta, (lower+size-a[axis])/delta))
                    low, high = max(low, ends[0]), min(high, ends[1])
                    if low > high:
                        return False
            return True
        for rotate in (False, True):
            def point(x, y):
                return ((y-114)*10, (162-x)*10) if rotate else ((x-142)*10, (y-114)*10)
            result = self.render(rotate)
            refs = [t for t in result["texts"] if t["text"] in ("AE1", "C61", "L3")]
            self.assertEqual({t["size"] for t in refs}, {12})
            self.assertEqual({t["angle"] for t in refs}, {0})
            for text in result["texts"]:
                b = text["box"]
                box = {**b, "x": b["x"]-4, "y": b["y"]-4, "width": b["width"]+8, "height": b["height"]+8}
                for x, y in ((148.7, 124), (149.2, 124.6), (149.6, 125)):
                    a, c = point(x, y), point(x+.6, y+.5)
                    pad = {"x": min(a[0], c[0]), "y": min(a[1], c[1]),
                           "width": abs(a[0]-c[0]), "height": abs(a[1]-c[1])}
                    self.assertFalse(box["x"] < pad["x"]+pad["width"] and pad["x"] < box["x"]+box["width"]
                                     and box["y"] < pad["y"]+pad["height"] and pad["y"] < box["y"]+box["height"], text)
                for line in result["lines"]:
                    if line.get("stroke-width") != .6:
                        # Include half of the dimension stroke in the clearance.
                        stroke_box = {**box, "x": box["x"]-.49, "y": box["y"]-.49,
                                      "width": box["width"]+.98, "height": box["height"]+.98}
                        self.assertFalse(crosses([line["x1"], line["y1"]], [line["x2"], line["y2"]], stroke_box), text)

    def test_only_one_board_annotation_path_exists(self):
        source = (ASSETS / "show.js").read_text()
        self.assertEqual(source.count("function placeLabels("), 1)
        self.assertEqual(source.count("const layout = placeLabels("), 1)
        self.assertNotIn("Math.cos(angle) * radius", source)


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
