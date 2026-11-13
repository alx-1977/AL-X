"""A board measured against a manufacturer's drawing, on real KiCad (2026-11-13).

The drawing is a small PDF made here with its numbers as text, as Würth's
are. Needs KiCad and poppler; skipped where they are not installed (CI).
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
from alx.providers.layout_check import read_numbers  # noqa: E402

KICAD = Path("/Applications/KiCad/KiCad.app")
POPPLER = Path("/opt/homebrew/bin/pdftotext")
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
            # His confirmation is recorded; changing a reading needs it again.
            self.assertTrue(call("confirm_layout_readings", **where)["confirmed"])
            self.assertTrue(call("check_layout", **where)["confirmed"])
            changed = call("check_layout", **where, readings=[{"number": ids["7.5"], "not_applicable": "a note"}])
            self.assertFalse(changed["confirmed"])
            # An edit of the copy makes an earlier check stale for the screen.
            call("edit_kicad_work_copy", work_copy_id=copy, script="b = load()\nmove_part(b, 'U2', 16, 5)\nsave(b)\n")
            self.assertFalse(provider.layout_check_result(checked["check_id"])["current"])


if __name__ == "__main__":
    unittest.main()
