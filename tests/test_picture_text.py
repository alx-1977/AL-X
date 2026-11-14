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
        self.assertEqual(clean("AREA"), "")


class PictureWordsTests(unittest.TestCase):
    def test_pixels_come_back_as_points_of_the_page(self) -> None:
        def execute(program, arguments, folder):
            if program.name == "pdftoppm":
                (Path(folder) / "section-1.png").write_bytes(b"png")
                return subprocess.CompletedProcess(arguments, 0, "", "")
            # 600 dpi: 25/3 pixels per point.
            return subprocess.CompletedProcess(arguments, 0, "250.0\t125.0\t50.0\t25.0\t.90\nbad row\n", "")
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "sheet.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            words = picture_words(pdf, 1, (300.0, 600.0, 450.0, 700.0), scratch_root=Path(folder) / "s",
                                  renderer=Path("/x/pdftoppm"), recognizer=Path("/x/picture-text"),
                                  execute=execute)
        self.assertEqual(words, [(330.0, 615.0, 336.0, 618.0, "0.90")])
        [number] = read_numbers(words, (300, 600, 450, 700))
        self.assertEqual(number["value"], 0.9)

    def test_a_failed_build_is_reported_and_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(LayoutCheckError) as raised:
                engine(Path(folder), lambda source, target: subprocess.CompletedProcess([], 1, "", "no swiftc"))
            self.assertEqual(raised.exception.code, "engine_unavailable")
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
