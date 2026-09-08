"""Regressions for the six findings the second independent review added.

Qodo reviewed the same commit (`425581b`) as the first reviewer, with no
repository-specific configuration and no steer toward what to look for. It
reproduced six of the first reviewer's findings independently and added six the
first review missed. Those six are what this file guards.

Each was reproduced before being fixed. The concurrency findings are the ones a
single-run test could never have caught, and they are the reason two reviews
were worth more than one.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.config import sandbox_settings  # noqa: E402
from alx.contracts.sandbox import (  # noqa: E402
    DAILY_RUNS,
    DAILY_WALL_SECONDS,
    MAX_WALKED_FILES,
    SandboxError,
    SandboxRequest,
)
from alx.observability.sandbox_ledger import (  # noqa: E402
    SQLiteSandboxLedger,
    SandboxBudget,
    SandboxBudgetExceeded,
)
from alx.providers.sandbox_retention import SandboxRetention  # noqa: E402
from alx.providers.sandbox_macos import SeatbeltSandboxRunner  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


class ConfinedRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = SandboxWorkspace(self.root)
        self.runner = SeatbeltSandboxRunner(
            self.workspace, denied_read_paths=(REPOSITORY_ROOT,)
        )
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        self.addCleanup(self.directory.cleanup)
        self.counter = 0

    def _run(self, source: str, session: str = "ses-a"):
        self.counter += 1
        run_id = f"run-{self.counter}"
        paths = self.workspace.prepare("exp-q", session, run_id)
        return paths, self.runner.run(
            SandboxRequest("exp-q", session, run_id, source), paths
        )


class Q10ConfiguredCeilingTest(unittest.TestCase):
    """A governed ceiling an operator could raise is not a ceiling."""

    def test_configuration_cannot_raise_the_approved_ceilings(self) -> None:
        settings = sandbox_settings(
            {
                "ALX_SANDBOX_ENABLED": "true",
                "ALX_SANDBOX_ROOT": "/tmp/sandbox",
                "ALX_SANDBOX_DAILY_RUNS": "100000",
                "ALX_SANDBOX_DAILY_WALL_SECONDS": "999999",
            }
        )
        self.assertEqual(settings.daily_runs, DAILY_RUNS)
        self.assertEqual(settings.daily_wall_seconds, DAILY_WALL_SECONDS)

    def test_configuration_may_still_lower_a_ceiling(self) -> None:
        settings = sandbox_settings(
            {
                "ALX_SANDBOX_ENABLED": "true",
                "ALX_SANDBOX_ROOT": "/tmp/sandbox",
                "ALX_SANDBOX_DAILY_RUNS": "5",
                "ALX_SANDBOX_DAILY_WALL_SECONDS": "60",
            }
        )
        self.assertEqual(settings.daily_runs, 5)
        self.assertEqual(settings.daily_wall_seconds, 60)


class Q13AuthoredFilenameTest(ConfinedRunTest):
    """Retention must not leave authored text behind in a filename."""

    def test_an_authored_filename_does_not_survive_retention(self) -> None:
        marker = "SECRET-sk-live-abcdef123456"
        paths, _ = self._run(f"open('{marker}.txt', 'w').write('x')\n")
        SandboxRetention(self.workspace, ttl_seconds=1).purge_session("exp-q", "ses-a")

        self.assertTrue(paths.manifest_path.exists())
        raw = paths.manifest_path.read_text()
        self.assertNotIn(marker, raw)

    def test_the_manifest_still_identifies_the_file_by_digest(self) -> None:
        paths, outcome = self._run("open('artifact.txt', 'w').write('x')\n")
        manifest = json.loads(paths.manifest_path.read_text())
        expected = hashlib.sha256(b"artifact.txt").hexdigest()
        self.assertIn(expected, [item["name_digest"] for item in manifest["artifacts"]])
        # The readable name still reaches the Core in the transient result.
        self.assertIn("artifact.txt", {item.name for item in outcome.artifacts})


class Q12WalkBoundTest(unittest.TestCase):
    """Links must count against the walk bound like everything else."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = SandboxWorkspace(Path(self.directory.name))

    def test_many_symlinks_cannot_exceed_the_walk_bound(self) -> None:
        paths = self.workspace.prepare("exp-q", "ses-links", "run-1")
        for index in range(MAX_WALKED_FILES + 1000):
            os.symlink("/etc/hosts", paths.session_state / f"link-{index}")
        walked = self.workspace.walk(paths.session_state)
        self.assertLessEqual(len(walked.entries), MAX_WALKED_FILES)
        self.assertTrue(walked.truncated)

    def test_a_complete_snapshot_is_not_marked_truncated(self) -> None:
        paths = self.workspace.prepare("exp-q", "ses-small", "run-1")
        (paths.session_state / "one.txt").write_text("x")
        walked = self.workspace.walk(paths.session_state)
        self.assertFalse(walked.truncated)


class Q9TruncationReportedTest(ConfinedRunTest):
    """A partial snapshot must say so rather than miscount artifacts."""

    def test_the_manifest_records_whether_the_state_snapshot_was_complete(self) -> None:
        paths, _ = self._run("open('a.txt', 'w').write('x')\n")
        manifest = json.loads(paths.manifest_path.read_text())
        self.assertIn("state_truncated", manifest)
        self.assertFalse(manifest["state_truncated"])


class Q8FullStreamDigestTest(ConfinedRunTest):
    """A digest over a prefix is not a digest of the output."""

    def test_the_digest_and_size_describe_the_whole_stream(self) -> None:
        paths, outcome = self._run("print('x' * 100000)\n")
        expected = (paths.run_directory / "stdout.log").read_bytes()
        self.assertEqual(outcome.stdout_byte_size, len(expected))
        self.assertEqual(
            outcome.stdout_digest, hashlib.sha256(expected).hexdigest()
        )
        self.assertFalse(outcome.stdout_capped)


class Q6LedgerAtomicityTest(unittest.TestCase):
    """A thread lock bounds nothing across processes."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runs.sqlite3"

    def test_two_ledger_handles_share_one_daily_ceiling(self) -> None:
        first = SQLiteSandboxLedger(self.path, SandboxBudget(2, 300))
        second = SQLiteSandboxLedger(self.path, SandboxBudget(2, 300))
        first.settle(first.reserve(1), 1.0)
        second.settle(second.reserve(1), 1.0)
        with self.assertRaises(SandboxBudgetExceeded):
            first.reserve(1)
        with self.assertRaises(SandboxBudgetExceeded):
            second.reserve(1)

    def test_the_reservation_is_one_immediate_transaction(self) -> None:
        source = (
            REPOSITORY_ROOT / "src/alx/observability/sandbox_ledger.py"
        ).read_text()
        reserve = source.split("def reserve", 1)[1].split("    def ", 1)[0]
        self.assertIn("BEGIN IMMEDIATE", reserve)


class Q7SessionLeaseTest(ConfinedRunTest):
    """Overlapping runs in one session would race over the working copy."""

    def test_a_second_run_in_a_leased_session_is_refused(self) -> None:
        paths = self.workspace.prepare("exp-q", "ses-lease", "run-1")
        with self.workspace.lease("exp-q", "ses-lease"):
            with self.assertRaises(SandboxError) as caught:
                self.runner.run(
                    SandboxRequest("exp-q", "ses-lease", "run-1", "print(1)\n"), paths
                )
        self.assertEqual(caught.exception.code, "session_busy")

    def test_the_lease_is_released_afterwards(self) -> None:
        with self.workspace.lease("exp-q", "ses-free"):
            pass
        _, outcome = self._run("print('ran')\n", session="ses-free")
        self.assertIn("ran", outcome.stdout)


class Q11RetentionSkipsActiveSessionsTest(ConfinedRunTest):
    """Retention must not purge a session another process is running in."""

    def test_a_leased_session_is_not_purged(self) -> None:
        paths, _ = self._run("open('live.txt', 'w').write('x')\n", session="ses-live")
        retention = SandboxRetention(self.workspace, ttl_seconds=0.0001)
        with self.workspace.lease("exp-q", "ses-live"):
            report = retention.sweep()
        self.assertEqual(report.sessions_purged, 0)
        self.assertTrue(paths.session_state.exists())

    def test_an_unleased_expired_session_is_still_purged(self) -> None:
        paths, _ = self._run("open('old.txt', 'w').write('x')\n", session="ses-old")
        stale = 1.0
        os.utime(paths.manifest_path, (stale, stale))
        os.utime(paths.run_directory, (stale, stale))
        os.utime(self.root / "exp-q" / "ses-old", (stale, stale))
        report = SandboxRetention(self.workspace, ttl_seconds=1).sweep()
        self.assertGreaterEqual(report.sessions_purged, 1)
        self.assertFalse(paths.session_state.exists())
        self.assertTrue(paths.manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
