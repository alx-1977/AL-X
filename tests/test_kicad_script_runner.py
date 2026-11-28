"""A failed edit script shows its error, not its own source (28 Nov 2026 replay)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.providers.kicad_helpers import script_runner  # noqa: E402


class ScriptRunnerTests(unittest.TestCase):
    def test_the_traceback_names_the_error_without_quoting_the_script(self) -> None:
        script = "\n".join("x%d = %d  # a long line of her drawing code" % (i, i) for i in range(300))
        script += "\nraise ValueError('U9 pin 2 would land on what is already there')\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edit.py"
            path.write_text("import sys as _sys\n_pcbnew = None\n" + script_runner(script))
            ran = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
        self.assertNotEqual(ran.returncode, 0)
        self.assertIn("U9 pin 2 would land", ran.stderr)
        self.assertNotIn("a long line of her drawing code", ran.stderr)
        self.assertLess(len(ran.stderr), 600)


if __name__ == "__main__":
    unittest.main()
