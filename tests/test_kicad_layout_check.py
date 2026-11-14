"""A board measured against a manufacturer's drawing, on real KiCad (2026-11-13).

The drawing is a small PDF made here with its numbers as text, as Würth's
are. Needs KiCad and poppler; skipped where they are not installed (CI).
"""

from __future__ import annotations

import json
import subprocess
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
from alx.providers.layout_check import extract_words, read_numbers  # noqa: E402

KICAD = Path("/Applications/KiCad/KiCad.app")
POPPLER = Path("/opt/homebrew/bin/pdftotext")
PYTHON = KICAD / "Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
FIXTURES = REPOSITORY_ROOT / "tests/fixtures"


def drawing(texts: list[tuple[float, float, str]]) -> bytes:
    """A one-page PDF (A4 landscape) with each text at x, y in points from the top left."""
    stream = "BT /F1 10 Tf " + " ".join(
        f"1 0 0 1 {x} {595 - y} Tm ({text}) Tj" for x, y, text in texts) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n"
    return out.encode()


class NumberReadingTests(unittest.TestCase):
    def test_tolerances_counts_and_part_values_are_told_apart(self) -> None:
        words = [(10, 10, 20, 20, "0,51"), (22, 10, 32, 20, "±0,15"), (10, 30, 20, 40, "4x"),
                 (10, 50, 20, 60, "1.8"), (10, 61, 20, 70, "pF"), (40, 50, 50, 60, "2.7"),
                 (51, 50, 58, 60, "nH"), (900, 900, 910, 910, "3.3")]
        numbers = read_numbers(words, (0, 0, 800, 800))
        self.assertEqual([(n["text"], n["value"], n["unit"]) for n in numbers],
                         [("0,51", 0.51, ""), ("1.8", 1.8, "pF"), ("2.7", 2.7, "nH")])


@unittest.skipUnless(KICAD.is_dir() and POPPLER.is_file() and kicad_containment_available(),
                     "KiCad or poppler is not installed")
class WordReaderTests(unittest.TestCase):
    """Words are read from pdftotext's table output (-tsv): -bbox crashed on
    every page of an onsemi datasheet on 14 Nov."""

    def test_words_and_boxes_come_from_the_table_rows(self) -> None:
        table = ("level\tpage_num\tpar_num\tblock_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                 "1\t1\t0\t0\t0\t0\t0.00\t0.00\t842.00\t595.00\t-1\t###PAGE###\n"
                 "5\t1\t0\t0\t0\t0\t100.00\t50.00\t12.50\t10.00\t100\t2.54\n"
                 "5\t1\t0\t0\t0\t1\t120.00\t50.00\t8.00\t10.00\t100\tR&D\n")

        def execute(program, arguments, folder):
            self.assertIn("-tsv", arguments)
            (Path(folder) / arguments[-1]).write_text(table)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "sheet.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            program = Path(folder) / "pdftotext"
            program.write_text("")
            words = extract_words(pdf, 1, scratch_root=Path(folder) / "scratch", extractor=program,
                                  execute=execute)
        self.assertEqual(words, [(100.0, 50.0, 112.5, 60.0, "2.54"), (120.0, 50.0, 128.0, 60.0, "R&D")])


class RealLayoutCheckTests(unittest.TestCase):
    def test_kicad_measures_and_an_edit_makes_the_check_stale(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            project = root / "designs" / "board"
            project.mkdir(parents=True)
            (project / "board.kicad_pcb").write_bytes((FIXTURES / "copper_board.kicad_pcb").read_bytes())
            (project / "board.kicad_pro").write_text(json.dumps({"meta": {"filename": "board.kicad_pro"}}))
            # U1: a 0.5 mm pad; the STEP copper is 1.2 mm; U1 to U2 is 10 mm; 3.0 is wrong
            # on purpose; 1.8 pF is a part value; 7.5 is not accounted for.
            (project / "sheet.pdf").write_bytes(drawing([
                (100, 100, "0.5"), (200, 100, "1.2"), (300, 100, "10"), (400, 100, "3.0"),
                (100, 200, "1.8"), (116, 200, "pF"), (200, 200, "7.5")]))
            runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                          root / "storage")

            def call(action, **arguments):
                result = kicad_call(runtime.executors, action, arguments)
                self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
                return result.values

            copy = call("check_kicad_board", project_dir=str(project))["work_copy_id"]
            where = {"work_copy_id": copy, "document": str(project / "sheet.pdf"), "page": 1,
                     "section": [50, 50, 500, 300], "part": "U1"}
            first = call("check_layout", **where)
            ids = {n["text"]: n["id"] for n in first["numbers"]}
            self.assertEqual(sorted(ids), ["0.5", "1.2", "1.8", "10", "3.0", "7.5"])
            readings = [
                {"number": ids["0.5"], "measures": {"pad_size": "U1.1", "axis": "x"}},
                {"number": ids["1.2"], "measures": {"copper_width": "STEP", "at": [0, 2], "axis": "x"}},
                {"number": ids["10"], "measures": {"distance": ["U1.1:centre", "U2.1:centre"], "axis": "x"}},
                {"number": ids["3.0"], "measures": {"distance": ["U1.1:centre", "U2.1:centre"], "axis": "x"}}]
            checked = call("check_layout", **where, readings=readings, rf=True)
            state = {n["text"]: (n["status"], n.get("board_mm")) for n in checked["numbers"]}
            self.assertEqual(state, {"0.5": ("matches", 0.5), "1.2": ("matches", 1.2), "10": ("matches", 10.0),
                                     "3.0": ("differs", 10.0), "1.8": ("not_applicable", None),
                                     "7.5": ("unaccounted", None)})
            self.assertFalse(checked["passed"])
            provider = runtime.provider
            self.assertTrue(provider.layout_check_result(checked["check_id"])["current"])
            # Every number counts, also beyond those listed (review of #239).
            import alx.providers.layout_check as layout_check
            original, layout_check.MAX_NUMBERS = layout_check.MAX_NUMBERS, 2
            try:
                few = call("check_layout", **where)
            finally:
                layout_check.MAX_NUMBERS = original
            self.assertEqual((len(few["numbers"]), few["numbers_omitted"]), (2, 4))
            self.assertEqual(dict(few["counts"]), dict(checked["counts"]))
            # His confirmation names the check he saw; changing a reading needs it again.
            self.assertTrue(call("confirm_layout_readings", check_id=few["check_id"])["confirmed"])
            confirmed = call("check_layout", **where)
            self.assertTrue(confirmed["confirmed"])
            self.assertFalse(provider.layout_check_result(checked["check_id"])["current"])
            changed = call("check_layout", **where, readings=[{"number": ids["7.5"], "not_applicable": "a note"}])
            self.assertFalse(changed["confirmed"])
            # A check made before the readings changed is no longer current, and
            # confirming it confirms nothing (review of #239).
            self.assertFalse(provider.layout_check_result(confirmed["check_id"])["current"])
            refused = kicad_call(runtime.executors, "confirm_layout_readings", {"check_id": confirmed["check_id"]})
            self.assertEqual(refused.state, CapabilityResultState.FAILED)
            # A board that changes while it is measured is never recorded (review of #239).
            from unittest.mock import patch
            with patch.object(type(provider), "_board_state", side_effect=["before", "after"]):
                moving = kicad_call(runtime.executors, "check_layout", where)
            self.assertEqual(moving.failure["code"], "work_copy_changed")
            # A section with no readable number is never a pass.
            empty = call("check_layout", **dict(where, section=[600, 400, 800, 500], part="U2"))
            self.assertFalse(empty["passed"])
            self.assertIn("no number could be read", empty["reason"])
            # An edit of the copy makes an earlier check stale for the screen.
            call("edit_kicad_work_copy", work_copy_id=copy, script="b = load()\nmove_part(b, 'U2', 16, 5)\nsave(b)\n")
            self.assertFalse(provider.layout_check_result(checked["check_id"])["current"])



@unittest.skipUnless(KICAD.is_dir() and POPPLER.is_file() and kicad_containment_available(),
                     "KiCad or poppler is not installed")
class LibraryWorkTests(unittest.TestCase):
    def test_a_library_footprint_is_copied_edited_measured_and_written_back(self) -> None:
        """13 Nov: library parts through the tool; land patterns checked once per part."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            library = root / "designs" / "FireFli-KiCad-Lib"
            pretty = library / "footprints" / "Test.pretty"
            pretty.mkdir(parents=True)
            (library / ".git").mkdir()
            (library / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (library / "symbols").mkdir()
            (library / "symbols" / "Test.kicad_sym").write_text("(kicad_symbol_lib (version 20241209))\n")
            # A footprint with a 0.5 mm square pad, in KiCad's own file format.
            (pretty / "PAD1.kicad_mod").write_text(
                '(footprint "PAD1"\n\t(version 20241229)\n\t(generator "pcbnew")\n\t(layer "F.Cu")\n'
                '\t(attr smd)\n\t(pad "1" smd rect\n\t\t(at 0 0)\n\t\t(size 0.5 0.5)\n'
                '\t\t(layers "F.Cu" "F.Paste" "F.Mask")\n\t)\n)\n')
            self.assertTrue((pretty / "PAD1.kicad_mod").is_file())
            (library / "sheet.pdf").write_bytes(drawing([(100, 100, "0.6")]))
            runtime = build_kicad_runtime(lambda: "call", {"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")},
                                          root / "storage")

            def call(action, **arguments):
                result = kicad_call(runtime.executors, action, arguments)
                self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
                return result.values

            # A link inside the library is never followed (review of #240).
            elsewhere = root / "elsewhere" / "Big.pretty"
            elsewhere.mkdir(parents=True)
            (elsewhere / "BIG.kicad_mod").write_text("(footprint \"BIG\")\n")
            (library / "footprints" / "Big.pretty").symlink_to(elsewhere, target_is_directory=True)
            # A .pretty folder itself is not a library folder (review of #240).
            refused = kicad_call(runtime.executors, "copy_library", {"library_dir": str(pretty)})
            self.assertEqual(refused.state, CapabilityResultState.FAILED)
            copy = call("copy_library", library_dir=str(library))
            self.assertFalse(list((root / "storage").rglob("BIG.kicad_mod")))
            self.assertEqual([lib["name"] for lib in copy["footprint_libraries"]], ["Test"])
            self.assertFalse(list((root / "storage").rglob(".git")))
            where = {"work_copy_id": copy["work_copy_id"], "footprint": "Test/PAD1", "part": "FP",
                     "document": str(library / "sheet.pdf"), "page": 1, "section": [50, 50, 300, 200]}
            # A wrong footprint name is a wrong name, not a broken tool (review of #240).
            for wrong in ("Test/Missing", "Test"):
                bad = kicad_call(runtime.executors, "check_layout", dict(where, footprint=wrong))
                self.assertEqual(bad.failure["code"], "arguments_unusable", bad.failure)
            # The screen draws the footprint as the check measures it (14 Nov).
            drawn = runtime.provider.section({"work_copy_id": copy["work_copy_id"], "footprint": "Test/PAD1",
                                              "refs": ["FP"]})
            self.assertEqual([part["ref"] for part in drawn["parts"]], ["FP"])
            [number] = call("check_layout", **where)["numbers"]
            reading = [{"number": number["id"], "measures": {"pad_size": "FP.1", "axis": "x"}}]
            first = call("check_layout", **where, readings=reading)
            self.assertEqual((first["numbers"][0]["status"], first["numbers"][0]["board_mm"]), ("differs", 0.5))
            edited = call("edit_kicad_work_copy", work_copy_id=copy["work_copy_id"], script=(
                "fp = footprint('Test', 'PAD1')\n"
                "fp.Pads()[0].SetSize(_pcbnew.PADSTACK.ALL_LAYERS, point(0.6, 0.6))\n"
                "save_footprint('Test', fp)\n"
                "print(footprints('Test'))\n"))
            self.assertIn("['PAD1']", edited["stdout"])
            self.assertEqual(list(edited["change_report"]["files_changed"]), ["footprints/Test.pretty/PAD1.kicad_mod"])
            # The earlier check is of the old footprint; a new one matches.
            self.assertFalse(runtime.provider.layout_check_result(first["check_id"])["current"])
            self.assertEqual(call("check_layout", **where)["numbers"][0]["status"], "matches")
            # A commit in the library meanwhile never reads as a changed original.
            (library / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
            call("write_back_kicad_board", work_copy_id=edited["work_copy_id"],
                 work_copy_revision=edited["work_copy_revision"], project_dir=str(library))
            self.assertIn("0.6", (pretty / "PAD1.kicad_mod").read_text())

if __name__ == "__main__":
    unittest.main()
