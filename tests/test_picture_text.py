"""A land-pattern drawing that is a picture, read by macOS text recognition (2026-11-14).

On 14 Nov two of five datasheets in a rehearsal (TL3340AF160QG, 830053099)
drew their land pattern as a picture: no text, so nothing was measured.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.providers.layout_check import LayoutCheckError, read_numbers  # noqa: E402
from alx.providers.picture_text import clean, engine, picture_words  # noqa: E402


class CleanTests(unittest.TestCase):
    def test_marks_go_but_brackets_and_words_stay(self) -> None:
        self.assertEqual(clean("Д.90"), "0.90")
        self.assertEqual(clean(".60"), "0.60")
        self.assertEqual(clean("4.25"), "4.25")
        # An inch value stays bracketed, so it is never taken for millimetres.
        self.assertEqual(clean("[.035"), "[.035")
        self.assertEqual(clean("(2)"), "(2)")
        # Review of D-081: words and units stay words, tolerances stay signed.
        for word in ("AREA", "Fig.3", "PIN1", "pF", "±0.1", "+0.1", "-0.2", "%"):
            self.assertEqual(clean(word), word)


class PictureWordsTests(unittest.TestCase):
    def test_pixels_come_back_as_points_of_the_page(self) -> None:
        calls = []

        def execute(program, arguments, folder):
            calls.append(program)
            if program.name == "pdftoppm":
                (Path(folder) / "section-1.png").write_bytes(b"png")
                return subprocess.CompletedProcess(arguments, 0, "", "")
            # The program runs from a copy inside its confined folder.
            self.assertEqual(program.parent, Path(folder))
            # 600 dpi: 25/3 pixels per point.
            return subprocess.CompletedProcess(arguments, 0, "250.0\t125.0\t50.0\t25.0\t.90\nbad row\n", "")
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "sheet.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            program = Path(folder) / "built"
            program.write_bytes(b"")
            cache = Path(folder) / "cache" / "words.json"
            ask = dict(scratch_root=Path(folder) / "s", renderer=Path("/x/pdftoppm"), recognizer=program,
                       execute=execute, cache=cache)
            words = picture_words(pdf, 1, (300.0, 600.0, 450.0, 700.0), **ask)
            # Asked again, the same words come from the cache, nothing is run.
            again = picture_words(pdf, 1, (300.0, 600.0, 450.0, 700.0), **ask)
        self.assertEqual(words, [(330.0, 615.0, 336.0, 618.0, "0.90")])
        self.assertEqual(again, words)
        self.assertEqual(len(calls), 2)
        [number] = read_numbers(words, (300, 600, 450, 700))
        self.assertEqual(number["value"], 0.9)

    def test_a_failed_build_is_reported_and_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(LayoutCheckError) as raised:
                engine(Path(folder), lambda source, target: subprocess.CompletedProcess([], 1, "", "no swiftc"))
            self.assertEqual(raised.exception.code, "engine_unavailable")
            # Remembered: a second check does not build again.
            [marker] = Path(folder).iterdir()
            self.assertTrue(marker.name.endswith(".failed"))
            with self.assertRaises(LayoutCheckError):
                engine(Path(folder), lambda source, target: self.fail("built again"))


if __name__ == "__main__":
    unittest.main()
