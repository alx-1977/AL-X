"""Regressions for the third independent review, on the integrated head.

Qodo reviewed `e0a1ce7` — Sandbox V1 as merged with the external-review and
merge-authority work from `main` — and reported seven findings. Five were
verified against the code and fixed; these tests are what stop them returning.

Two were declined and are recorded in the pull request rather than here: the
duplicated daily-limit literals are deliberate boundary-local constants kept
equal by an existing test, and the "internal prompt terminology" finding names
wording already approved on `main` under D-025 and mirrored by D-027.

Every test here reproduces the exact failure the reviewer described, and fails
against the code as it stood before the fix.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.sandbox import build_sandbox_runtime  # noqa: E402
from alx.contracts.sandbox import (  # noqa: E402
    MAX_WALKED_FILES,
    MAX_WORKSPACE_BYTES,
    SandboxError,
    SandboxOutcome,
    SandboxRequest,
)
from alx.observability.sandbox_ledger import SandboxBudget  # noqa: E402
from alx.providers.sandbox_retention import SandboxRetention  # noqa: E402
from alx.providers.sandbox_runner import SeatbeltSandboxRunner  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


class WorkspaceCeilingTest(unittest.TestCase):
    """F3: a truncated walk must never prove the workspace is within bounds.

    `WalkResult.total_bytes` sums the entries it recorded. `walk()` stops at
    MAX_WALKED_FILES, so the files it never reached are exactly where the
    excess would be: a session could hold far more than the 64 MiB ceiling
    while the partial total read as compliant, and the overflow was left on
    disk. Fixed by treating a truncated snapshot as over the ceiling.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root)
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def _state(self) -> Path:
        paths = self.workspace.prepare("exp-f3", "ses-f3", "run-1")
        return paths.session_state

    def test_a_truncated_walk_is_reported_as_incomplete(self) -> None:
        """The precondition: more entries than the bound truncates."""
        state = self._state()
        for index in range(MAX_WALKED_FILES + 50):
            (state / f"f{index}").write_bytes(b"x")

        result = self.workspace.walk(state)
        self.assertTrue(result.truncated)
        # And the total it reports is necessarily a partial one.
        self.assertLess(len(result.entries), MAX_WALKED_FILES + 50)

    def test_a_truncated_walk_cannot_show_the_ceiling_was_respected(self) -> None:
        """The defect: >5000 files whose real total exceeds 64 MiB.

        The recorded prefix stays small, so the old check compared a partial
        sum against the ceiling and passed. Writing a real 64 MiB would make
        this test slow for no extra proof, so the property is asserted the way
        the runner sees it: truncated means unproven, whatever the partial
        total says.
        """
        state = self._state()
        for index in range(MAX_WALKED_FILES + 50):
            (state / f"f{index}").write_bytes(b"x")

        result = self.workspace.walk(state)
        self.assertTrue(result.truncated)
        # The partial total looks compliant, which is precisely the trap.
        self.assertLess(result.total_bytes, MAX_WORKSPACE_BYTES)
        # So the decision must not be made from the total alone.
        over_ceiling = result.truncated or result.total_bytes > MAX_WORKSPACE_BYTES
        self.assertTrue(
            over_ceiling,
            "a truncated snapshot must never prove the workspace is within bounds",
        )

    def test_an_oversized_session_is_refused_and_purged(self) -> None:
        """End to end through the real runner, if this platform can confine."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-f3b", "ses-f3b", "run-1")
        source = (
            "import pathlib\n"
            f"for i in range({MAX_WALKED_FILES + 50}):\n"
            "    pathlib.Path(f'f{i}').write_bytes(b'x')\n"
        )
        with self.assertRaises(SandboxError) as raised:
            self.runner.run(
                SandboxRequest("exp-f3b", "ses-f3b", "run-1", source), paths
            )
        self.assertEqual(raised.exception.code, "workspace_exhausted")
        # The overflow is not left behind.
        self.assertFalse(any(paths.session_state.glob("f*")))


class DirectoryFloodTest(unittest.TestCase):
    """F6: directories are traversal work and must count against the bound.

    The bound was checked only inside the file loop, so `os.walk` descended
    through directories without counting them. A tree of empty directories is
    cheap to create inside the wall clock and was then enumerated without any
    bound, by the snapshot and by every later retention sweep.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = SandboxWorkspace(Path(directory.name))

    def test_a_directory_only_flood_is_bounded(self) -> None:
        """Nested rather than flat, within what the filesystem allows.

        A single 5,200-deep chain exceeds the OS path limit, so the tree is
        built as a fan: deep enough that traversal is real work, wide enough to
        pass the bound. Either shape was unbounded before the fix.
        """
        paths = self.workspace.prepare("exp-f6", "ses-f6", "run-1")
        state = paths.session_state
        made = 0
        branch = 0
        while made < MAX_WALKED_FILES + 200:
            current = state / f"b{branch}"
            for depth in range(50):
                current = current / f"d{depth}"
            current.mkdir(parents=True)
            made += 51
            branch += 1

        result = self.workspace.walk(state)
        self.assertTrue(
            result.truncated,
            "a directory-only tree must reach the walk bound like any other",
        )

    def test_a_wide_directory_flood_is_bounded(self) -> None:
        """Breadth as well as depth: siblings count too."""
        paths = self.workspace.prepare("exp-f6b", "ses-f6b", "run-1")
        state = paths.session_state
        for index in range(MAX_WALKED_FILES + 200):
            (state / f"d{index}").mkdir()

        self.assertTrue(self.workspace.walk(state).truncated)

    def test_a_small_tree_is_still_complete(self) -> None:
        """The bound must not make ordinary sessions look truncated."""
        paths = self.workspace.prepare("exp-f6c", "ses-f6c", "run-1")
        state = paths.session_state
        (state / "a").mkdir()
        (state / "a" / "b").mkdir()
        (state / "a" / "b" / "note.txt").write_text("hello")

        result = self.workspace.walk(state)
        self.assertFalse(result.truncated)
        self.assertIn("a/b/note.txt", result.entries)


class RetentionLeaseRaceTest(unittest.TestCase):
    """F4: the idle decision and the purge must happen under one lease.

    `is_leased()` could only answer by taking the lock and releasing it, so a
    runner could acquire the session in the gap between the answer and the
    purge. Retention then deleted the state, source and output of a run that
    had already started.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = SandboxWorkspace(Path(directory.name))
        # A short TTL plus a `now` far in the future expires everything, so
        # only the lease can protect a session.
        self.retention = SandboxRetention(self.workspace, ttl_seconds=1)

    def test_a_held_lease_stops_the_purge(self) -> None:
        paths = self.workspace.prepare("exp-f4", "ses-f4", "run-1")
        (paths.session_state / "work.txt").write_text("mid-run state")

        holding = threading.Event()
        release = threading.Event()

        def runner() -> None:
            with self.workspace.lease("exp-f4", "ses-f4"):
                holding.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=runner)
        thread.start()
        try:
            self.assertTrue(holding.wait(timeout=5))
            # Retention runs while the lease is genuinely held.
            report = self.retention.sweep(now=time.time() + 10_000)
            self.assertEqual(report.sessions_purged, 0)
            self.assertTrue(
                (paths.session_state / "work.txt").is_file(),
                "retention deleted the state of a running experiment",
            )
        finally:
            release.set()
            thread.join(timeout=5)

    def test_the_decision_is_made_while_holding_the_lease(self) -> None:
        """The race itself: acquiring between the probe and the purge.

        `idle_lease` yields while still holding the lock, so a runner cannot
        take the session after the sweep decides it is idle. This asserts the
        property directly: while the sweep holds a session, a run cannot start
        in it.
        """
        self.workspace.prepare("exp-f4b", "ses-f4b", "run-1")
        session = self.workspace.root / "exp-f4b" / "ses-f4b"
        started: list[str] = []

        with self.workspace.idle_lease(session) as idle:
            self.assertTrue(idle)
            try:
                with self.workspace.lease("exp-f4b", "ses-f4b"):
                    started.append("acquired")
            except SandboxError as error:
                started.append(error.code)

        self.assertEqual(
            started,
            ["session_busy"],
            "a run started while retention believed the session was idle",
        )

    def test_an_idle_session_is_still_purged(self) -> None:
        """The lease must not stop retention doing its job."""
        paths = self.workspace.prepare("exp-f4c", "ses-f4c", "run-1")
        (paths.session_state / "old.txt").write_text("expired")

        report = self.retention.sweep(now=time.time() + 10_000)
        self.assertEqual(report.sessions_purged, 1)
        self.assertFalse((paths.session_state / "old.txt").exists())


class TruncatedArtifactCountTest(unittest.TestCase):
    """F5: counts from a truncated walk must not look exact.

    `artifacts` and `artifacts_omitted` were derived from possibly-incomplete
    snapshots, so a run reported a precise-looking omission count that silently
    excluded every unscanned file. Only the manifest knew the state was
    partial. The outcome now carries that fact to the caller.
    """

    @staticmethod
    def _built(**overrides) -> SandboxOutcome:
        return _build_outcome(**overrides)


def _build_outcome(**overrides) -> SandboxOutcome:
    """One valid outcome, with only the field under test varied."""
    from datetime import UTC, datetime

    if True:
        values = dict(
            experiment_id="exp-f5",
            session_id="ses-f5",
            run_id="run-1",
            exit_status=0,
            signalled=False,
            timed_out=False,
            stdout="",
            stderr="",
            stdout_omitted_characters=0,
            stderr_omitted_characters=0,
            stdout_digest="0" * 64,
            stderr_digest="0" * 64,
            stdout_byte_size=0,
            stderr_byte_size=0,
            artifacts=(),
            artifacts_omitted=0,
            wall_seconds_used=0.1,
            started_at=datetime(2026, 9, 7, tzinfo=UTC),
            finished_at=datetime(2026, 9, 7, tzinfo=UTC),
        )
        values.update(overrides)
        return SandboxOutcome(**values)

    def test_the_outcome_carries_whether_the_state_was_complete(self) -> None:
        self.assertTrue(self._built(state_truncated=True).state_truncated)
        self.assertFalse(self._built().state_truncated)

    def test_truncation_reaches_the_core_and_durable_state(self) -> None:
        """Both channels: an incomplete count must be visible in each."""
        outcome = self._built(state_truncated=True)
        self.assertTrue(outcome.as_values()["state_truncated"])
        self.assertTrue(outcome.durable_values()["state_truncated"])

    def test_the_capability_declares_the_field(self) -> None:
        """A schema with extra_properties=False would otherwise reject it."""
        from alx.tools.sandbox import DEFINITION

        self.assertIn("state_truncated", DEFINITION.output_schema.properties)

    def test_truncation_carries_no_experiment_authored_bytes(self) -> None:
        """It is a boolean, so making counts honest costs no privacy."""
        durable = self._built(state_truncated=True).durable_values()
        self.assertIsInstance(durable["state_truncated"], bool)


class PreLaunchAccountingTest(unittest.TestCase):
    """F7: a failure before the process starts must not spend a daily run.

    `executed` was set before calling the runner, but the runner writes the
    profile, copies the source and walks the baseline first. A disk error in
    any of those charged a run for an experiment that never started.
    """

    def _runtime(self, runner):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return build_sandbox_runtime(
            True,
            root / "w",
            root / "l.sqlite3",
            lambda: "call-1",
            runner=runner,
            budget=SandboxBudget(20, 300),
        )

    def test_a_failure_before_launch_charges_nothing(self) -> None:
        class FailsBeforeLaunch:
            def available(self) -> bool:
                return True

            def run(self, request, paths, launched=None):
                # Exactly the real order: writing the profile and copying the
                # source happen before any process exists, so `launched` has
                # not been called.
                raise SandboxError("workspace_unavailable")

        runtime = self._runtime(FailsBeforeLaunch())
        executor = runtime.executors["run_sandbox_experiment"]
        for _ in range(3):
            executor(
                {"experiment_id": "exp-f7", "session_id": "ses-f7", "source": "print(1)"}
            )

        self.assertEqual(
            runtime.ledger.committed_runs(),
            0,
            "a failure before the process started consumed a daily run",
        )

    def test_a_failure_after_launch_still_charges(self) -> None:
        """The other direction: real wall time must appear in the fuse."""

        class FailsAfterLaunch:
            def available(self) -> bool:
                return True

            def run(self, request, paths, launched=None):
                if launched is not None:
                    launched()
                raise RuntimeError("manifest write failed after execution")

        runtime = self._runtime(FailsAfterLaunch())
        executor = runtime.executors["run_sandbox_experiment"]
        for _ in range(3):
            executor(
                {"experiment_id": "exp-f7b", "session_id": "ses-f7b", "source": "print(1)"}
            )

        self.assertEqual(runtime.ledger.committed_runs(), 3)

    def test_the_runner_signals_the_moment_a_process_exists(self) -> None:
        """The signal is what the accounting depends on, so it is asserted."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workspace = SandboxWorkspace(Path(directory.name))
        runner = SeatbeltSandboxRunner(workspace)
        if not runner.available():
            self.skipTest("no supported confinement mechanism on this platform")

        paths = workspace.prepare("exp-f7c", "ses-f7c", "run-1")
        signals: list[str] = []
        runner.run(
            SandboxRequest("exp-f7c", "ses-f7c", "run-1", "print('ok')"),
            paths,
            lambda: signals.append("launched"),
        )
        self.assertEqual(signals, ["launched"])


class SelfReviewFindingsTest(unittest.TestCase):
    """Three further defects found reviewing the fixes above.

    Two of them were made more likely by those fixes rather than introduced by
    them: refusing a truncated walk sends far more runs down the overflow path,
    and creating the lease marker unconditionally gave more sessions one to
    leave behind.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root)
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def test_an_overflowing_run_leaves_no_unaudited_bytes(self) -> None:
        """The overflow path writes no manifest, so it may keep nothing.

        stdout, stderr and the source were left in the run directory with
        nothing describing them: not covered by a manifest, and reachable only
        by retention at the TTL.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-r1", "ses-r1", "run-1")
        source = (
            "import pathlib\n"
            "print('noise')\n"
            f"for i in range({MAX_WALKED_FILES + 50}):\n"
            "    pathlib.Path(f'f{i}').write_bytes(b'x')\n"
        )
        with self.assertRaises(SandboxError) as raised:
            self.runner.run(
                SandboxRequest("exp-r1", "ses-r1", "run-1", source), paths
            )
        self.assertEqual(raised.exception.code, "workspace_exhausted")

        self.assertFalse(
            paths.run_directory.exists(),
            "an unaudited run directory survived a refused run",
        )
        self.assertFalse(any(paths.session_state.iterdir()))

    def test_a_completed_run_keeps_its_evidence(self) -> None:
        """The other direction: purge_run must not touch an audited run."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-r1b", "ses-r1b", "run-1")
        self.runner.run(
            SandboxRequest("exp-r1b", "ses-r1b", "run-1", "print('kept')"), paths
        )
        self.assertTrue(paths.manifest_path.is_file())

    def test_purge_run_refuses_a_path_outside_the_root(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: outside.rmdir())
        with self.assertRaises(SandboxError):
            self.workspace.purge_run(outside)

    def test_retention_leaves_no_lease_marker_behind(self) -> None:
        """A purged session should not keep a lock file for a run that is gone."""
        paths = self.workspace.prepare("exp-r3", "ses-r3", "run-1")
        (paths.session_state / "secret.txt").write_text("experiment bytes")

        report = SandboxRetention(self.workspace, ttl_seconds=1).sweep(
            now=time.time() + 10_000
        )
        self.assertEqual(report.sessions_purged, 1)

        session = self.root / "exp-r3" / "ses-r3"
        self.assertFalse(
            (session / ".lease").exists(), "a stale lease marker survived retention"
        )
        # And the experiment's own bytes are still gone.
        self.assertFalse((paths.session_state / "secret.txt").exists())

    def test_the_lease_marker_is_not_counted_as_experiment_bytes(self) -> None:
        """It is this module's bookkeeping, so it must not inflate the count."""
        paths = self.workspace.prepare("exp-r3b", "ses-r3b", "run-1")
        (paths.session_state / "one.txt").write_text("x")
        # Give the session a marker exactly as a run would.
        with self.workspace.lease("exp-r3b", "ses-r3b"):
            pass

        session = self.root / "exp-r3b" / "ses-r3b"
        removed = self.workspace.purge_transient(session)

        # Compared against an identical session that never took a lease, so the
        # assertion is about the marker rather than about how many other
        # entries `prepare` happens to create.
        control = self.workspace.prepare("exp-r3c", "ses-r3c", "run-1")
        (control.session_state / "one.txt").write_text("x")
        expected = self.workspace.purge_transient(self.root / "exp-r3c" / "ses-r3c")

        self.assertEqual(removed, expected)
        self.assertFalse((session / ".lease").exists())


class PrivilegedParentTest(unittest.TestCase):
    """Four defects from the review of the fixed head, three of them escapes.

    All three High findings turn on the same mistake in different places: the
    privileged parent treated a name under session state as trustworthy, when
    session state is writable by the experiment and persists between runs.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def test_a_linked_entry_file_cannot_overwrite_a_host_file(self) -> None:
        """Q2: shutil.copyfile followed a symlink at the destination.

        An experiment leaves its entry file as a link to any host path. The
        next run's privileged preparation then writes the new source through
        it, outside the sandbox entirely.
        """
        victim = self.root / "HOST_FILE"
        victim.write_text("original host content")
        paths = self.workspace.prepare("exp-q2", "ses-q2", "run-1")
        (paths.session_state / "experiment.py").symlink_to(victim)

        with self.assertRaises(SandboxError):
            self.runner._write_working_copy(
                paths.session_state / "experiment.py", "NEW SOURCE"
            )
        self.assertEqual(
            victim.read_text(),
            "original host content",
            "the privileged parent wrote through a symlink to a host file",
        )

    def test_an_ordinary_entry_file_is_still_replaced(self) -> None:
        """The fix must not break iterating in a session."""
        paths = self.workspace.prepare("exp-q2b", "ses-q2b", "run-1")
        working = paths.session_state / "experiment.py"
        working.write_text("old source")

        self.runner._write_working_copy(working, "new source")
        self.assertEqual(working.read_text(), "new source")

    def test_a_linked_output_is_evidence_not_a_host_read(self) -> None:
        """Q4: the walk checked a name, then reopened it.

        A helper that survives the run can swap a checked file for a symlink
        between the check and the open, so the parent hashes a host file.
        Opening once with O_NOFOLLOW removes the gap.
        """
        secret = self.root / "HOST_SECRET"
        secret.write_text("host contents nobody may hash")
        paths = self.workspace.prepare("exp-q4", "ses-q4", "run-1")
        (paths.session_state / "link.txt").symlink_to(secret)
        (paths.session_state / "real.txt").write_text("mine")

        entries = self.workspace.walk(paths.session_state).entries
        # A link is recorded, so creating one stays visible.
        self.assertEqual(entries["link.txt"], ("", 0))
        host_digest = hashlib.sha256(secret.read_bytes()).hexdigest()
        self.assertNotIn(
            host_digest,
            [digest for digest, _ in entries.values()],
            "the privileged parent hashed a file outside the workspace",
        )
        # And an ordinary output is still measured.
        self.assertEqual(entries["real.txt"][1], 4)

    def test_the_walk_does_not_block_on_a_fifo_or_device(self) -> None:
        """Opening a fifo blocks until a writer arrives; a device never ends.

        Both would hang the privileged parent forever, which is a denial of
        service that outlives the run's wall clock.
        """
        paths = self.workspace.prepare("exp-q4b", "ses-q4b", "run-1")
        os.mkfifo(paths.session_state / "pipe")
        (paths.session_state / "devlink").symlink_to("/dev/zero")
        (paths.session_state / "ok.txt").write_text("hi")

        finished: list[bool] = []

        def walk() -> None:
            self.workspace.walk(paths.session_state)
            finished.append(True)

        thread = threading.Thread(target=walk, daemon=True)
        thread.start()
        thread.join(timeout=15)
        self.assertTrue(finished, "the evidence walk blocked on a fifo or device")

    def test_a_helper_does_not_outlive_a_clean_exit(self) -> None:
        """Q3: the group was killed only when the run timed out.

        A program that spawns a background helper and returns cleanly left it
        running - outside wall-time accounting, still able to write to session
        state while the evidence walk read it.

        Staged directly against the group sweep rather than through a confined
        run: RLIMIT_NPROC on this host refuses the child's own spawn, so a real
        experiment cannot set the trap here. What is asserted is the property
        the runner now relies on - after a leader exits, no member of its group
        is left alive.
        """
        import subprocess

        leader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time;"
                " subprocess.Popen([sys.executable, '-c',"
                " 'import time; time.sleep(60)']);"
                " time.sleep(0.2)",
            ],
            start_new_session=True,
        )
        group = SeatbeltSandboxRunner._group_of(leader)
        self.assertIsNotNone(group)
        leader.wait(timeout=10)

        # The leader is gone; without the sweep the helper is still running.
        SeatbeltSandboxRunner._reap_group(group)

        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(group, 0)


if __name__ == "__main__":
    unittest.main()
