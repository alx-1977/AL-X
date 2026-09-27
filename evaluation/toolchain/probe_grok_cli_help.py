"""Installed Grok CLI help wording. Not part of the default suite.

The adapter's argv is covered by tests/test_grok_subscription.py. This probe
reads the installed binary's help text.

    python -m unittest discover -s evaluation/toolchain -p 'probe_*.py'
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.providers.grok_subscription import (  # noqa: E402
    GrokSubscriptionReasoningModel,
)


class GrokCliHelpProbe(unittest.TestCase):
    def test_installed_cli_help_documents_the_headless_contract(self) -> None:
        executable = shutil.which("grok")
        if executable is None:
            self.skipTest("unverified on this CLI: grok is not installed")
        model = GrokSubscriptionReasoningModel("grok-4.6", 30)
        with tempfile.TemporaryDirectory(prefix="alx-grok-help-") as cwd:
            result = subprocess.run(
                [executable, "--help"],
                cwd=cwd,
                env=model.child_environment(),
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        help_text = result.stdout.lower()
        self.assertIn("--single", help_text)
        self.assertIn("--json-schema", help_text)
        self.assertIn("--prompt-file", help_text)
        self.assertIn("--tools", help_text)
        self.assertIn("--disallowed-tools", help_text)
        self.assertNotIn("XAI_API_KEY", result.stdout)


if __name__ == "__main__":
    unittest.main()
