"""3D and SPICE models of a new part (19 Nov 2026): Fusion needs a body for every part, and a
circuit can only be simulated with the maker's SPICE model."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.providers.kicad import _placed_extent  # noqa: E402
from alx.providers.kicad_models import (  # noqa: E402
    body_boxes, box_step, check_deck, model_placement, package_files, spice_models, step_extent, with_model,
)

NGSPICE = shutil.which("ngspice") or "/opt/homebrew/bin/ngspice"
LEGACY = '''(module "X" (layer F.Cu)
  (pad 1 smd rect (at 0 0 0) (size 0.1 0.1) (layers F.Cu))
  (model X.stp
    (at (xyz 0 0 0.0019685039663409))
    (scale (xyz 1 1 1))
    (rotate (xyz -90 0 0))
  )
)'''


class ModelTests(unittest.TestCase):
    def test_a_generated_body_has_the_datasheet_size(self) -> None:
        step = box_step("T", body_boxes({"body": [2.0, 3.0], "height": 0.75}, [(-1.2, -0.2, -0.8, 0.2)]))
        self.assertTrue(step.startswith(b"ISO-10303-21;"))
        self.assertEqual(step_extent(step), ((-1.2, -1.5, 0.0), (1.0, 1.5, 0.75)))
        self.assertEqual(step.count(b"MANIFOLD_SOLID_BREP"), 2)

    def test_a_samacsys_placement_stands_the_model_up_on_the_board(self) -> None:
        offset, rotate = model_placement(LEGACY)
        self.assertAlmostEqual(offset[2], 0.05, places=4)        # inches in the legacy format
        self.assertEqual(rotate, (-90.0, 0.0, 0.0))
        # Drawn y up (0..0.31 tall, 1.0 deep): turned by KiCad it lies on the board, z up.
        low, high = _placed_extent(((-0.6, -0.04, -0.5), (0.6, 0.31, 0.5)), offset, rotate)
        self.assertAlmostEqual(high[1] - low[1], 1.0)
        self.assertAlmostEqual(low[2], 0.01, places=3)
        self.assertAlmostEqual(high[2], 0.36, places=3)

    def test_the_footprint_names_its_model_file(self) -> None:
        text = with_model('(footprint "X"\n\t(layer "F.Cu")\n)\n', "${FIREFLI_KICAD_LIB}/3dmodels/A.3dshapes/X.step",
                          (0, 0, 0.05), (-90, 0, 0))
        self.assertIn('(model "${FIREFLI_KICAD_LIB}/3dmodels/A.3dshapes/X.step"', text)
        self.assertIn("(xyz -90 0 0)", text)
        self.assertEqual(text.count("(model "), 1)

    def test_a_download_gives_its_footprint_model_and_spice_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "part.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("X/KiCad/X.kicad_mod", LEGACY)
                archive.writestr("X/3D/X.stp", "ISO-10303-21;")
                archive.writestr("X/BAT54.lib", ".model BAT54 D")
                archive.writestr("X/license.txt", "no")
                archive.writestr("../escape.lib", "no")
            found = package_files(bundle)
            self.assertEqual(sorted(found["footprint"]), ["X.kicad_mod"])
            self.assertEqual(sorted(found["model"]), ["X.stp"])
            self.assertEqual(sorted(found["spice"]), ["BAT54.lib"])

    def test_spice_files_are_read_for_their_models_and_nodes(self) -> None:
        models = spice_models(".SUBCKT BSS84 D G S\n+ PARAMS: W=1\nM1 D G S S PM\n.model PM PMOS(VTO=-1)\n.ENDS BSS84\n")
        self.assertEqual(models["BSS84"], {"kind": "subckt", "nodes": ["D", "G", "S"]})
        self.assertEqual(models["PM"]["kind"], "pmos")

    @unittest.skipUnless(Path(NGSPICE).is_file(), "ngspice is not installed")
    def test_ngspice_loads_a_sound_model_and_refuses_a_missing_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "d.lib").write_text(".model BAT54 D(IS=2e-7 N=1.04)\n")
            (Path(folder) / "ok.cir").write_text(check_deck("d.lib", {"kind": "d", "nodes": []}, "BAT54"))
            (Path(folder) / "bad.cir").write_text(check_deck("d.lib", {"kind": "subckt", "nodes": ["a", "b"]}, "NOPE"))
            ok = subprocess.run([NGSPICE, "-b", "ok.cir"], capture_output=True, text=True, cwd=folder, timeout=60)
            bad = subprocess.run([NGSPICE, "-b", "bad.cir"], capture_output=True, text=True, cwd=folder, timeout=60)
            self.assertNotRegex(ok.stdout + ok.stderr, r"(?i)\berror\b")
            self.assertRegex(bad.stdout + bad.stderr, r"(?i)\berror\b|unknown subckt|could not find")

    def test_engine_output_that_is_not_text_is_still_read(self) -> None:
        # ngspice prints raw bytes in its device listing for some maker models (BSS84AK, 19 Nov 2026).
        from alx.providers.coding_process import run_kicad_engine
        with tempfile.TemporaryDirectory() as folder:
            done = run_kicad_engine("/usr/bin/printf", ["ok \\231\\231 end"], folder)
            self.assertIn("ok", done.stdout)
            self.assertIn("end", done.stdout)


if __name__ == "__main__":
    unittest.main()
