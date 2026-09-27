"""Installed Claude CLI help wording. Not part of the default suite.

The adapter's argv is covered by tests/test_claude_subscription.py. This probe
asks the installed binary what those flags mean, which changes when the CLI's
help text changes.

    python -m unittest discover -s evaluation/toolchain -p 'probe_*.py'
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.providers.claude_subscription import (  # noqa: E402
    ClaudeSubscriptionReasoningModel,
)


class ClaudeCliHelpProbe(unittest.TestCase):
    def test_installed_cli_help_documents_isolation_contract(self) -> None:
        executable = shutil.which("claude")
        if executable is None:
            self.skipTest("unverified on this CLI: claude is not installed")
        model = ClaudeSubscriptionReasoningModel("opus", 60)
        with tempfile.TemporaryDirectory(prefix="alx-claude-help-") as cwd:
            result = subprocess.run(
                [executable, "--help"], cwd=cwd, env=model.child_environment(),
                capture_output=True, text=True, timeout=15, check=True,
            )

        def option(flag: str) -> tuple[str, set[str]]:
            lines = result.stdout.splitlines()
            start = next(
                (index for index, line in enumerate(lines)
                 if re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|$)", line)),
                None,
            )
            self.assertIsNotNone(start, f"installed Claude CLI lacks {flag}")
            block = [lines[start]]
            for line in lines[start + 1:]:
                if re.match(r"^  (?:-\w, )?--[a-z]", line):
                    break
                block.append(line)
            description = " ".join(" ".join(block).split()).lower()
            return description, set(re.findall(r"[a-z]+", description))

        tools, tool_words = option("--tools")
        self.assertIn('""', tools)
        self.assertTrue({"disable", "all", "tools"} <= tool_words)
        _strict_mcp, strict_mcp_words = option("--strict-mcp-config")
        self.assertTrue(
            {"only", "mcp", "servers", "ignoring", "other", "configurations"}
            <= strict_mcp_words
        )
        _mcp, mcp_words = option("--mcp-config")
        self.assertTrue({"load", "mcp", "servers", "json", "strings"} <= mcp_words)
        _sources, source_words = option("--setting-sources")
        self.assertTrue({"user", "project", "local"} <= source_words)
        _persistence, persistence_words = option("--no-session-persistence")
        self.assertTrue(
            {"disable", "persistence", "saved", "resumed", "print"}
            <= persistence_words
        )


if __name__ == "__main__":
    unittest.main()
