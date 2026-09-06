"""D-027 session persistence and retention.

Two properties are proved here, and they pull in opposite directions.

A session must provide genuine iterative state: a later run reads what an
earlier run wrote, or `session_id` is decoration rather than a working context.

And retention must remove every experiment-authored byte while keeping the
bounded manifest. Deleting the run directory wholesale would destroy the audit;
keeping it would leave an indefinite archive of source and output. Both failure
modes are tested, because either would satisfy a careless implementation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.sandbox import SandboxRequest  # noqa: E402
from alx.providers.sandbox_retention import SandboxRetention  # noqa: E402
from alx.providers.sandbox_runner import SeatbeltSandboxRunner  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


class SessionPersistenceTest(unittest.TestCase):
    """A session is iterative state, not a naming convention."""

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

    def _run(self, session: str, run_id: str, source: str):
        paths = self.workspace.prepare("exp-c", session, run_id)
        return self.runner.run(
            SandboxRequest("exp-c", session, run_id, source), paths
        )

    def test_a_later_run_reads_what_an_earlier_run_wrote(self) -> None:
        first = self._run(
            "ses-iter", "run-1", "open('data.txt', 'w').write('42')\nprint('wrote')\n"
        )
        self.assertEqual(first.exit_status, 0)
        second = self._run(
            "ses-iter", "run-2", "print('read', open('data.txt').read())\n"
        )
        self.assertIn("read 42", second.stdout)

    def test_a_later_run_can_import_a_module_an_earlier_run_created(self) -> None:
        """The point of persistence: build a helper, then use it."""
        self._run(
            "ses-import", "run-1", "open('helper.py', 'w').write('VALUE = 7\\n')\n"
        )
        outcome = self._run(
            "ses-import", "run-2", "import helper\nprint('value', helper.VALUE)\n"
        )
        self.assertIn("value 7", outcome.stdout)

    def test_a_different_session_does_not_see_the_first_session_state(self) -> None:
        self._run("ses-one", "run-1", "open('private.txt', 'w').write('one')\n")
        outcome = self._run(
            "ses-two",
            "run-1",
            "import os\nprint('found' if os.path.exists('private.txt') else 'absent')\n",
        )
        self.assertIn("absent", outcome.stdout)

    def test_only_changed_files_are_reported_as_artifacts(self) -> None:
        self._run("ses-delta", "run-1", "open('kept.txt', 'w').write('same')\n")
        outcome = self._run(
            "ses-delta",
            "run-2",
            "open('fresh.txt', 'w').write('new')\n",
        )
        names = {item.name for item in outcome.artifacts}
        self.assertIn("fresh.txt", names)
        self.assertNotIn("kept.txt", names)


class RetentionTest(unittest.TestCase):
    """Every experiment-authored byte goes; the manifest stays."""

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
        self.retention = SandboxRetention(self.workspace)
        self.paths = self.workspace.prepare("exp-d", "ses-d", "run-1")
        self.outcome = self.runner.run(
            SandboxRequest(
                "exp-d",
                "ses-d",
                "run-1",
                "open('artifact.txt', 'w').write('secret experiment output')\n"
                "print('a distinctive line of program output')\n",
            ),
            self.paths,
        )
        self.session = self.root / "exp-d" / "ses-d"

    def test_before_retention_the_transient_bytes_exist(self) -> None:
        self.assertTrue((self.paths.source_directory / "experiment.py").exists())
        self.assertTrue((self.paths.run_directory / "stdout.log").exists())
        self.assertTrue((self.paths.session_state / "artifact.txt").exists())
        self.assertTrue(self.paths.manifest_path.exists())

    def test_retention_removes_every_experiment_authored_byte(self) -> None:
        self.retention.purge_session("exp-d", "ses-d")

        self.assertFalse((self.paths.source_directory / "experiment.py").exists())
        self.assertFalse(self.paths.source_directory.exists())
        self.assertFalse((self.paths.run_directory / "stdout.log").exists())
        self.assertFalse((self.paths.run_directory / "stderr.log").exists())
        self.assertFalse((self.paths.run_directory / "profile.sb").exists())
        self.assertFalse(self.paths.session_state.exists())

        # Nothing anywhere under the session still contains the program's words.
        for current, _, file_names in os.walk(self.session):
            for name in file_names:
                content = Path(current, name).read_text(errors="replace")
                self.assertNotIn("a distinctive line of program output", content)
                self.assertNotIn("secret experiment output", content)

    def test_retention_preserves_the_manifest(self) -> None:
        self.retention.purge_session("exp-d", "ses-d")
        self.assertTrue(self.paths.manifest_path.exists())

        manifest = json.loads(self.paths.manifest_path.read_text())
        # The audit still answers what ran, when, and what it produced.
        self.assertEqual(manifest["run_id"], "run-1")
        self.assertEqual(manifest["exit_status"], 0)
        self.assertEqual(manifest["stdout_digest"], self.outcome.stdout_digest)
        # Names are digested in the manifest: it outlives the files, and an
        # experiment-chosen filename kept here would survive retention as
        # authored text.
        import hashlib

        expected = hashlib.sha256(b"artifact.txt").hexdigest()
        self.assertIn(expected, [item["name_digest"] for item in manifest["artifacts"]])
        self.assertEqual(manifest["confinement"], "seatbelt")

    def test_the_manifest_holds_no_experiment_authored_free_text(self) -> None:
        """The audit describes the bytes; it never contains them."""
        raw = self.paths.manifest_path.read_text()
        self.assertNotIn("a distinctive line of program output", raw)
        self.assertNotIn("secret experiment output", raw)
        self.assertNotIn("open('artifact.txt'", raw)

    def test_runs_do_not_become_an_indefinite_archive(self) -> None:
        """The failure mode opposite to deleting too much."""
        self.retention.purge_session("exp-d", "ses-d")
        survivors = {
            path.name
            for path in self.paths.run_directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(survivors, {"manifest.json"})

    def test_a_sweep_purges_an_expired_session_and_spares_a_fresh_one(self) -> None:
        retention = SandboxRetention(self.workspace, ttl_seconds=1)
        fresh = self.workspace.prepare("exp-d", "ses-fresh", "run-1")
        self.runner.run(
            SandboxRequest("exp-d", "ses-fresh", "run-1", "print('fresh')\n"), fresh
        )
        # Age the first session past the TTL without waiting for it.
        old = time.time() - 3600
        for current, directory_names, file_names in os.walk(self.session):
            for name in (*directory_names, *file_names):
                os.utime(Path(current, name), (old, old))
        os.utime(self.session, (old, old))

        report = retention.sweep()

        self.assertGreaterEqual(report.sessions_purged, 1)
        self.assertFalse(self.paths.session_state.exists())
        self.assertTrue(self.paths.manifest_path.exists())
        self.assertTrue(fresh.session_state.exists())

    def test_surviving_manifests_remain_listable_after_retention(self) -> None:
        self.retention.purge_session("exp-d", "ses-d")
        manifests = self.retention.manifests("exp-d", "ses-d")
        self.assertEqual(len(manifests), 1)
        self.assertTrue(manifests[0].is_file())


if __name__ == "__main__":
    unittest.main()
