"""The coding agent passes its own file set to the scoped diff evidence.

The mechanism these tests exercise -- a diff narrowed to named paths, bounded
once, reporting its own truncation -- lives in coding_process.py and is tested
in tests/test_coding_diff_scope.py. This file covers the other half: that the
agent actually uses it for the outcome Core reads.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.coding_agent import CodingAgent  # noqa: E402


class BothReadersSeeTheSameScopedEvidence(unittest.TestCase):
    """One helper and the job's own file set."""

    def test_the_outcome_diff_is_scoped_to_the_job_files(self) -> None:
        source = inspect.getsource(CodingAgent)
        self.assertIn("self._git_evidence(workspace, job_files)", source)

    def test_the_agent_labels_a_truncated_diff(self) -> None:
        source = inspect.getsource(CodingAgent._git_evidence)
        self.assertIn("diff truncated", source)
        self.assertIn("evidence.diff_truncated", source)


if __name__ == "__main__":
    unittest.main()
