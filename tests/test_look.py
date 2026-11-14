"""D-075: AL/X looks at a picture or a PDF page herself."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.look import build_look_runtime  # noqa: E402
from alx.contracts import CapabilityResultState  # noqa: E402
from alx.providers.look_image import (  # noqa: E402
    LEFT_MARGIN, TOP_MARGIN, LookError, grid_step, look, png, png_size, ppm, stored_image, with_grid,
)
from alx.tools.look import DEFINITIONS, LOOK, build_look_executors  # noqa: E402


def blank_pdf(path: Path, width: float = 400, height: float = 300, pages: int = 2) -> None:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    with path.open("wb") as handle:
        writer.write(handle)


class LookProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pdf = self.root / "designs" / "sheet.pdf"
        self.pdf.parent.mkdir()
        blank_pdf(self.pdf)
        self.picture = self.root / "designs" / "concept.jpg"
        self.picture.write_bytes(b"jpeg bytes")
        self.store = self.root / "looks"
        self.scratch = self.root / "scratch"
        self.runs = []
        for tool in ("pdftoppm", "ffmpeg"):
            (self.root / tool).write_text("")

    def execute(self, tool, arguments, directory):
        arguments = [str(item) for item in arguments]
        self.runs.append((Path(tool).name, arguments, Path(directory)))
        directory = Path(directory)
        if Path(tool).name == "pdftoppm":
            width, height = int(arguments[arguments.index("-W") + 1]), int(arguments[arguments.index("-H") + 1])
            (directory / "section.ppm").write_bytes(b"P6\n%d %d\n255\n" % (width, height)
                                                    + b"\xff" * (width * height * 3))
        else:
            (directory / "look.png").write_bytes(png(30, 20, b"\x00" * (30 * 20 * 3)))
        return SimpleNamespace(returncode=0, stderr="")

    def look(self, path, arguments):
        return look(path, arguments, store=self.store, scratch_root=self.scratch,
                    renderer=self.root / "pdftoppm", picture_converter=self.root / "ffmpeg",
                    execute=self.execute)

    def test_a_whole_page_is_rendered_with_a_labelled_grid_and_kept_by_digest(self) -> None:
        facts = self.look(self.pdf, {"page": 2})
        self.assertEqual((facts["kind"], facts["page"], facts["page_count"]), ("pdf_page", 2, 2))
        self.assertEqual(facts["section"], [0, 0, 400, 300])
        self.assertEqual(facts["grid"]["step_points"], 20)
        name, arguments, directory = self.runs[0]
        self.assertEqual((name, arguments[:4]), ("pdftoppm", ["-f", "2", "-l", "2"]))
        self.assertTrue(directory.is_relative_to(self.scratch))
        self.assertEqual(list(self.scratch.iterdir()), [])
        image = (self.store / (facts["image_sha256"] + ".png")).read_bytes()
        self.assertEqual(hashlib.sha256(image).hexdigest(), facts["image_sha256"])
        self.assertEqual(png_size(image), tuple(facts["pixels"]))
        self.assertEqual(stored_image(self.store, facts["image_sha256"]), image)

    def test_the_grid_is_drawn_where_its_points_are(self) -> None:
        width, height, pixels = with_grid(100, 50, b"\xff" * (100 * 50 * 3), (10, 20, 110, 70), 10)
        self.assertEqual((width, height), (100 + LEFT_MARGIN, 50 + TOP_MARGIN))

        def pixel(x, y):
            at = (y * width + x) * 3
            return tuple(pixels[at:at + 3])
        # x = 20 points is 10 pixels into the section; y = 30 points is 10 down.
        self.assertNotEqual(pixel(LEFT_MARGIN + 10, TOP_MARGIN + 25), (255, 255, 255))
        self.assertNotEqual(pixel(LEFT_MARGIN + 25, TOP_MARGIN + 10), (255, 255, 255))
        self.assertEqual(pixel(LEFT_MARGIN + 15, TOP_MARGIN + 15), (255, 255, 255))
        self.assertEqual(grid_step(400, 300), 20)
        self.assertEqual(grid_step(30, 20), 2)

    def test_a_section_is_rendered_from_its_points(self) -> None:
        facts = self.look(self.pdf, {"page": 1, "section": [50, 40, 150, 90]})
        self.assertEqual(facts["section"], [50, 40, 150, 90])
        arguments = self.runs[0][1]
        self.assertEqual(arguments[arguments.index("-r") + 1], "600")

    def test_a_picture_is_scaled_never_up_and_kept(self) -> None:
        facts = self.look(self.picture, {})
        self.assertEqual((facts["kind"], facts["pixels"]), ("picture", [30, 20]))
        name, arguments, _ = self.runs[0]
        self.assertEqual(name, "ffmpeg")
        self.assertIn("scale=w='min(1568,iw)':h='min(1568,ih)':force_original_aspect_ratio=decrease",
                      arguments)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_what_cannot_be_looked_at_is_refused_before_anything_runs(self) -> None:
        other = self.root / "designs" / "notes.txt"
        other.write_text("x")
        for path, arguments, code in ((other, {}, "arguments_unusable"),
                                      (self.root / "missing.pdf", {}, "document_unavailable"),
                                      (self.pdf, {"page": 3}, "arguments_unusable"),
                                      (self.pdf, {"page": True}, "arguments_unusable")):
            with self.subTest(path=path.name, arguments=arguments):
                with self.assertRaises(LookError) as caught:
                    self.look(path, arguments)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.runs, [])

    def test_a_huge_pdf_is_refused_before_it_is_opened(self) -> None:
        # Review of 908947d: the size bound came after pypdf had read it.
        from unittest.mock import patch
        with patch("alx.providers.look_image.MAX_DOCUMENT_BYTES", 10), \
                patch("alx.providers.look_image.page_box") as opened:
            with self.assertRaises(LookError) as caught:
                self.look(self.pdf, {"page": 1})
        self.assertEqual(caught.exception.code, "document_unavailable")
        opened.assert_not_called()

    def test_a_changed_or_unnamed_image_is_not_handed_back(self) -> None:
        facts = self.look(self.picture, {})
        digest = facts["image_sha256"]
        (self.store / (digest + ".png")).write_bytes(b"changed")
        self.assertIsNone(stored_image(self.store, digest))
        for name in (None, "../x", "A" * 64, "a" * 63):
            self.assertIsNone(stored_image(self.store, name))

    def test_a_ppm_must_be_whole(self) -> None:
        self.assertEqual(ppm(b"P6\n# c\n2 1\n255\n" + b"\x01" * 6), (2, 1, b"\x01" * 6))
        with self.assertRaises(LookError):
            ppm(b"P6\n2 1\n255\n" + b"\x01" * 3)


class LookCapabilityTests(unittest.TestCase):
    def test_it_reads_only_and_reports_failures_as_her_evidence(self) -> None:
        self.assertEqual(DEFINITIONS[0].side_effect.name, "NONE")

        def broken(_):
            raise LookError("page_failed", "renderer stopped")
        run = build_look_executors(broken, lambda: "c1")[LOOK]
        result = run({"path": "/x.pdf"})
        self.assertEqual((result.state, result.failure["code"]), (CapabilityResultState.FAILED, "page_failed"))
        self.assertEqual(build_look_executors(None, lambda: "c1")[LOOK]({"path": "x"}).failure["code"],
                         "unavailable")
        self.assertEqual(build_look_executors(lambda _: {}, lambda: "c1")[LOOK](
            {"path": "x", "section": [1, 2]}).failure["code"], "arguments_unusable")

    def test_only_his_design_folders_and_alx_storage_are_looked_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            designs = root / "designs"
            designs.mkdir()
            runtime = build_look_runtime(lambda: "c1", {}, root / "storage", (designs,))
            run = runtime.executors[LOOK]
            outside = root / "elsewhere.png"
            outside.write_bytes(b"x")
            self.assertEqual(run({"path": str(outside)}).failure["code"], "path_denied")
            secret = designs / ".ssh" / "key.png"
            secret.parent.mkdir()
            secret.write_bytes(b"x")
            self.assertEqual(run({"path": str(secret)}).failure["code"], "path_denied")
            self.assertEqual(run({"path": ""}).failure["code"], "arguments_unusable")
            self.assertEqual(runtime.store, root / "storage" / "looks")
            self.assertEqual(runtime.permissions, frozenset({"files.look"}))

    def test_every_core_reads_the_look_store(self) -> None:
        wiring = (REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertIn("look_images = partial(stored_image, look_runtime.store)", wiring)
        self.assertEqual(wiring.count("image_source=look_images"), 3)


if __name__ == "__main__":
    unittest.main()
