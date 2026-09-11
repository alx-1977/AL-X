"""The coding agent passes its own file set to the scoped diff evidence.

The mechanism these tests exercise -- a diff narrowed to named paths, bounded
once, reporting its own truncation -- lives in coding_process.py and is tested
in tests/test_coding_diff_scope.py.

This file covers the other half: that the agent actually uses it, and that both
the outcome Core reads and the diff the reviewer judges are narrowed the same
way. Those call sites sit inside the local-reviewer loop, which is not in HEAD,
so this file is deliberately kept separate and travels with that work rather
than with the mechanism.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.coding_agent import CodingAgent  # noqa: E402


class BothReadersSeeTheSameScopedEvidence(unittest.TestCase):
    """One helper, one file set, so neither can be shown more than the other."""

    def test_both_call_sites_pass_the_evolving_reviewed_file_set(self) -> None:
        source = inspect.getsource(CodingAgent)
        # The outcome Core reads, and the diff the reviewer judges. The set
        # starts with session files and gains any correction-only path.
        self.assertIn("self._git_evidence(workspace, reviewed_files)", source)
        self.assertIn("reviewed_files = next_files", source)

    def test_the_agent_labels_a_truncated_diff(self) -> None:
        source = inspect.getsource(CodingAgent._git_evidence)
        self.assertIn("diff truncated", source)
        self.assertIn("evidence.diff_truncated", source)


if __name__ == "__main__":
    unittest.main()
