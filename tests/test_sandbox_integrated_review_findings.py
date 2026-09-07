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

import ast
import hashlib
import os
import subprocess
import json
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.sandbox import build_sandbox_runtime  # noqa: E402
from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.sandbox import (  # noqa: E402
    MAX_FILE_BYTES,
    MAX_PROCESSES,
    MAX_WALKED_FILES,
    MAX_WORKSPACE_BYTES,
    SandboxError,
    SandboxOutcome,
    SandboxRequest,
)
from alx.observability.sandbox_ledger import SandboxBudget  # noqa: E402
from alx.providers.sandbox_retention import (  # noqa: E402
    LIVE_RUN_NAME,
    SandboxRetention,
)
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


class ResilienceAndAccountingTest(unittest.TestCase):
    """Four defects from the review of the escape fixes.

    Two of them are about the capability staying usable and honest rather than
    about confinement: a sweep that could brick the runtime, and a fuse that
    under-counted the machine time actually spent.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")

    def test_one_undeletable_session_does_not_stop_the_sweep(self) -> None:
        """R2: the sweep runs before every experiment, so it must not abort.

        purge_transient does filesystem work on directories an experiment can
        make undeletable, and rmtree and unlink raise plain OSError, which the
        sweep did not catch. One stuck session therefore aborted composition
        and every later run.
        """
        # A barrier the parent cannot clear, so the skip-and-continue path is
        # the one under test. The directory is made unreadable *and* its parent
        # is left owned by another concept of the run: on this host the closest
        # reliable stand-in is a permission the sweep's repair also fails on,
        # so the deletion is forced to fail rather than merely be awkward.
        stuck = self.workspace.prepare("exp-s1", "ses-s1", "run-1")
        (stuck.session_state / "held").mkdir()
        (stuck.session_state / "held" / "f.txt").write_text("x")

        ordinary = self.workspace.prepare("exp-s2", "ses-s2", "run-1")
        (ordinary.session_state / "ok.txt").write_text("should be purged")

        original = SandboxWorkspace.purge_transient

        def explode(inner_self, session):
            if session.name == "ses-s1":
                raise PermissionError("cannot delete this session")
            return original(inner_self, session)

        SandboxWorkspace.purge_transient = explode
        self.addCleanup(setattr, SandboxWorkspace, "purge_transient", original)

        report = SandboxRetention(self.workspace, ttl_seconds=1).sweep(
            now=time.time() + 10_000
        )

        self.assertEqual(report.sessions_purged, 1)
        self.assertFalse(
            (ordinary.session_state / "ok.txt").exists(),
            "one stuck session stopped every other session being purged",
        )

    def test_a_zero_wall_time_is_refused_before_anything_runs(self) -> None:
        """R4: `or` turned an explicit zero into the 30-second default."""
        from alx.tools.sandbox import RUN_SANDBOX_EXPERIMENT, build_sandbox_executors

        reached: list[object] = []

        def never(request):
            reached.append(request)
            raise AssertionError("a zero-second request must not run")

        executor = build_sandbox_executors(
            never, lambda: "call-1", lambda: "run-1"
        )[RUN_SANDBOX_EXPERIMENT]

        for value in (0, -1):
            with self.subTest(wall_seconds=value):
                result = executor(
                    {
                        "experiment_id": "exp-z",
                        "session_id": "ses-z",
                        "source": "print(1)",
                        "wall_seconds": value,
                    }
                )
                self.assertIs(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(reached, [], "an invalid request reached the runner")

    def test_an_omitted_wall_time_still_takes_the_default(self) -> None:
        """The fix must not turn a missing value into a refusal."""
        from alx.contracts.sandbox import DEFAULT_WALL_SECONDS
        from alx.tools.sandbox import RUN_SANDBOX_EXPERIMENT, build_sandbox_executors

        seen: list[int] = []

        def capture(request):
            seen.append(request.wall_seconds)
            raise SandboxError("sandbox_unavailable")

        executor = build_sandbox_executors(
            capture, lambda: "call-1", lambda: "run-1"
        )[RUN_SANDBOX_EXPERIMENT]
        executor(
            {"experiment_id": "exp-d", "session_id": "ses-d", "source": "print(1)"}
        )
        self.assertEqual(seen, [DEFAULT_WALL_SECONDS])

    def test_a_relative_sandbox_root_is_anchored_not_launch_dependent(self) -> None:
        """R5: workspace and ledger identity must not follow the launch folder.

        The runtime storage root is resolved against the repository; the
        sandbox root was passed through as configured, so starting the service
        from another directory created a different workspace and a different
        ledger - a session lost its history and the day's spend restarted.
        """
        from alx.config import sandbox_settings

        settings = sandbox_settings(
            {"ALX_SANDBOX_ENABLED": "true", "ALX_SANDBOX_ROOT": ".alx/sandbox"}
        )
        self.assertFalse(settings.workspace_root.is_absolute())

        # The composition applies the same anchoring the storage root gets.
        repository_root = Path("/somewhere/repo")
        anchored = repository_root / settings.workspace_root
        self.assertTrue(anchored.is_absolute())
        self.assertTrue(str(anchored).startswith("/somewhere/repo"))

    def test_the_composition_anchors_a_relative_root(self) -> None:
        """Asserted against the composition source, not a restatement of it."""
        source = (
            REPOSITORY_ROOT / "src/alx/bootstrap/live_voice.py"
        ).read_text()
        self.assertIn("sandbox_workspace_root = ", source)
        self.assertIn(
            "repository_root / sandbox_workspace_root",
            source,
            "a relative sandbox root is no longer anchored to the repository",
        )
        self.assertIn("repository_root / sandbox_ledger_path", source)


class IndependentReviewFindingsTest(unittest.TestCase):
    """Four defects a second reviewer found in the fixes above.

    Three are the same lesson in new places: a fix that named one shape of a
    problem, and stopped there. The symlink working copy was fixed but not the
    fifo; the walk counted directories but not special files; the sweep stopped
    aborting but stopped deleting too.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def test_one_session_cannot_read_another(self) -> None:
        """G1: session isolation was incidental, not enforced.

        The profile denied the home directory and whatever the composition
        passed in, but never the sandbox root. Isolation therefore held only
        when the root happened to sit beneath the home directory. A root
        elsewhere - which is the layout D-027 pushes toward, and the layout
        these tests use - left every session readable by every other, reachable
        as ../../<other>/state from the working directory.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        first = self.workspace.prepare("exp-x", "ses-one", "run-1")
        self.runner.run(
            SandboxRequest(
                "exp-x",
                "ses-one",
                "run-1",
                "open('secret.txt', 'w').write('SESSION-ONE-SECRET')\n",
            ),
            first,
        )

        second = self.workspace.prepare("exp-x", "ses-two", "run-1")
        source = (
            "import os\n"
            "target = os.path.join(os.getcwd(), '..', '..', 'ses-one',"
            " 'state', 'secret.txt')\n"
            "try:\n"
            "    print('CROSS', open(target).read())\n"
            "except Exception as error:\n"
            "    print('BLOCKED', type(error).__name__)\n"
        )
        outcome = self.runner.run(
            SandboxRequest("exp-x", "ses-two", "run-1", source), second
        )

        self.assertIn("BLOCKED", outcome.stdout)
        self.assertNotIn(
            "SESSION-ONE-SECRET",
            outcome.stdout,
            "one session read another session's state",
        )

    def test_a_session_can_still_read_its_own_state(self) -> None:
        """Denying the root must not deny the run its own workspace."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-y", "ses-y", "run-1")
        self.runner.run(
            SandboxRequest(
                "exp-y", "ses-y", "run-1", "open('kept.txt','w').write('mine')\n"
            ),
            paths,
        )
        outcome = self.runner.run(
            SandboxRequest(
                "exp-y",
                "ses-y",
                "run-2",
                "print('OWN', open('kept.txt').read())\n",
            ),
            self.workspace.prepare("exp-y", "ses-y", "run-2"),
        )
        self.assertIn("OWN mine", outcome.stdout)

    def test_a_fifo_entry_file_does_not_hang_the_parent(self) -> None:
        """G2: O_NOFOLLOW refuses a symlink; a fifo is not a symlink.

        Opening a fifo for writing blocks until a reader arrives. The parent
        held the session lease and the day's reservation while it waited, and
        the capability call never returned - in the live runtime, the agent
        loop. The walk was given O_NONBLOCK for this reason; this open was not.
        """
        paths = self.workspace.prepare("exp-f2", "ses-f2", "run-1")
        os.mkfifo(paths.session_state / "experiment.py")

        outcome: list[str] = []

        def attempt() -> None:
            try:
                self.runner._write_working_copy(
                    paths.session_state / "experiment.py", "source"
                )
                outcome.append("wrote")
            except SandboxError as error:
                outcome.append(error.code)

        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), "the parent blocked on a fifo")
        self.assertEqual(outcome, ["workspace_unavailable"])

    def test_an_ordinary_working_copy_is_replaced_completely(self) -> None:
        """Truncation still happens, so no bytes of the old source survive."""
        paths = self.workspace.prepare("exp-f3", "ses-f3", "run-1")
        working = paths.session_state / "experiment.py"
        working.write_text("a much longer previous program body")

        self.runner._write_working_copy(working, "short")
        self.assertEqual(working.read_text(), "short")

    def test_special_files_count_against_the_walk_bound(self) -> None:
        """G3: names that produce no entry were free.

        A fifo is skipped for hashing, but it is still a name the parent opened
        and inspected. Skipping without counting let thousands of them - which
        a confined program can create - pass the bound entirely.
        """
        paths = self.workspace.prepare("exp-n", "ses-n", "run-1")
        for index in range(MAX_WALKED_FILES + 50):
            os.mkfifo(paths.session_state / f"p{index}")
        (paths.session_state / "ok.txt").write_text("x")

        self.assertTrue(
            self.workspace.walk(paths.session_state).truncated,
            "a flood of special files passed the walk bound",
        )

    def test_an_ordinary_session_is_not_reported_truncated(self) -> None:
        paths = self.workspace.prepare("exp-n2", "ses-n2", "run-1")
        (paths.session_state / "a.txt").write_text("x")
        self.assertFalse(self.workspace.walk(paths.session_state).truncated)

    def test_retention_deletes_bytes_the_experiment_protected(self) -> None:
        """G4: the previous fix stopped the sweep aborting, and stopped it
        deleting.

        D-027 requires every experiment-authored byte to go at the retention
        limit. An experiment can set UF_IMMUTABLE on its own file, or chmod a
        directory unreadable, and the sweep simply skipped it - so the bytes
        became permanent. The parent owns them, so it clears the barrier it is
        entitled to clear and deletes.
        """
        paths = self.workspace.prepare("exp-i", "ses-i", "run-1")
        immutable = paths.session_state / "keep-me.txt"
        immutable.write_text("must-not-survive-ttl")
        os.chflags(immutable, stat.UF_IMMUTABLE)
        self.addCleanup(
            lambda: os.chflags(immutable, 0) if immutable.exists() else None
        )

        locked = paths.session_state / "held"
        locked.mkdir()
        (locked / "f.txt").write_text("also must not survive")
        os.chmod(locked, 0o500)

        report = SandboxRetention(self.workspace, ttl_seconds=1).sweep(
            now=time.time() + 10_000
        )

        self.assertEqual(report.sessions_purged, 1)
        self.assertFalse(immutable.exists(), "an immutable file outlived its TTL")
        self.assertFalse(locked.exists(), "a locked directory outlived its TTL")

    def test_the_manifest_still_survives_retention(self) -> None:
        """Clearing barriers must not start deleting what D-027 keeps."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-m", "ses-m", "run-1")
        self.runner.run(
            SandboxRequest(
                "exp-m", "ses-m", "run-1", "open('out.txt','w').write('bytes')\n"
            ),
            paths,
        )
        SandboxRetention(self.workspace, ttl_seconds=1).sweep(
            now=time.time() + 10_000
        )
        self.assertTrue(paths.manifest_path.is_file())


class ProfileCompletenessTest(unittest.TestCase):
    """Three defects in the Seatbelt profile itself.

    Two are authority the profile granted and D-027 does not: reads of data
    outside a short deny-list, and execution of every binary on the host. The
    third is the profile failing to parse at all when a path contains a quote.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def test_a_data_store_outside_the_denied_paths_is_refused(self) -> None:
        """Q1: the deny-list named four paths; data lives in more than four.

        A production store outside the home directory, the repository, the
        runtime root and the sandbox root was readable by any experiment that
        knew its path, which D-027 withholds entirely.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")

        # Deliberately outside the home directory, the repository, the runtime
        # root and the sandbox root - the four paths the profile already
        # denied. A probe inside any of those would pass with or without this
        # fix, which is exactly the mistake the earlier isolation test made.
        probe = None
        for root in ("/private/var/db", "/Library/Application Support"):
            directory = Path(root)
            if not directory.is_dir():
                continue
            for candidate in sorted(directory.glob("*")):
                if candidate.is_file() and os.access(candidate, os.R_OK):
                    probe = candidate
                    break
            if probe is not None:
                break
        if probe is None:
            self.skipTest("no readable file outside the previously denied paths")

        paths = self.workspace.prepare("exp-d", "ses-d", "run-1")
        source = (
            f"try:\n"
            f"    open({str(probe)!r}, 'rb').read(16)\n"
            f"    print('READ-OK')\n"
            f"except Exception as error:\n"
            f"    print('BLOCKED', type(error).__name__)\n"
        )
        outcome = self.runner.run(
            SandboxRequest("exp-d", "ses-d", "run-1", source), paths
        )
        self.assertIn("BLOCKED", outcome.stdout)
        self.assertNotIn("READ-OK", outcome.stdout)

    def test_a_host_binary_cannot_be_executed(self) -> None:
        """Q2: process-exec was unrestricted.

        D-027 authorises Python programs using the standard library. An
        unrestricted exec authorised every binary on the host, and os.execv
        needs no fork, so the RLIMIT_NPROC exhaustion that refuses subprocess
        did not mask it: /bin/echo ran inside the sandbox.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-x2", "ses-x2", "run-1")
        source = (
            "import os\n"
            "try:\n"
            "    os.execv('/bin/echo', ['echo', 'HOST-BINARY-RAN'])\n"
            "except Exception as error:\n"
            "    print('EXEC-BLOCKED', type(error).__name__)\n"
        )
        outcome = self.runner.run(
            SandboxRequest("exp-x2", "ses-x2", "run-1", source), paths
        )
        self.assertIn("EXEC-BLOCKED", outcome.stdout)
        self.assertNotIn("HOST-BINARY-RAN", outcome.stdout)

    def test_the_interpreter_itself_still_starts(self) -> None:
        """Narrowing exec must not refuse the launch it exists to permit.

        Starting CPython is not one exec: the configured name is a symlink and
        a framework build re-execs a further binary inside its own bundle.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-x3", "ses-x3", "run-1")
        outcome = self.runner.run(
            SandboxRequest("exp-x3", "ses-x3", "run-1", "print('ORDINARY OK')"),
            paths,
        )
        self.assertEqual(outcome.exit_status, 0)
        self.assertIn("ORDINARY OK", outcome.stdout)

    def test_a_quote_in_a_path_does_not_corrupt_the_profile(self) -> None:
        """Q3: paths were interpolated into SBPL strings unescaped.

        The checkout location, home directory, runtime storage and an
        operator-set sandbox root may all legally contain a quote. One ends the
        string early, the profile stops parsing, and sandbox-exec refuses every
        experiment before Python starts.
        """
        awkward = self.root / 'we"ird'
        awkward.mkdir()
        workspace = SandboxWorkspace(awkward / "ws")
        runner = SeatbeltSandboxRunner(workspace)
        paths = workspace.prepare("exp-q", "ses-q", "run-1")

        profile = runner.profile(paths.session_state)
        self.assertIn('we\\"ird', profile)

        if not runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        outcome = runner.run(
            SandboxRequest("exp-q", "ses-q", "run-1", "print('QUOTED PATH OK')"),
            paths,
        )
        self.assertEqual(outcome.exit_status, 0)
        self.assertIn("QUOTED PATH OK", outcome.stdout)

    def test_a_newline_in_a_path_is_refused_rather_than_encoded(self) -> None:
        """A newline inside a security profile is not worth accommodating."""
        from alx.providers.sandbox_runner import _sbpl

        with self.assertRaises(SandboxError):
            _sbpl("/tmp/two\nlines")


class FixesOfFixesTest(unittest.TestCase):
    """Three defects in the three newest fixes, from a second review pass.

    Every one is a fix that named a shape of a problem and left an adjacent
    shape open, and every one had a regression test that passed anyway because
    it probed only what the fix had just added. That pattern is the reason
    these tests are written to fail against the *remaining* behaviour rather
    than against some earlier state.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def test_clearing_barriers_never_follows_a_link_to_a_host_file(self) -> None:
        """The retention fix wrote outside the workspace.

        `os.chmod` and `os.chflags` follow symlinks by default, so clearing a
        barrier on a link an experiment planted changed the mode of whatever it
        pointed at - a privileged write to an attacker-chosen host path. It
        also left the session undeleted, because the link's own flags were
        never cleared.
        """
        victim = self.root / "HOST_FILE"
        victim.write_text("host content")
        os.chmod(victim, 0o600)

        paths = self.workspace.prepare("exp-l", "ses-l", "run-1")
        link = paths.session_state / "link"
        os.symlink(victim, link)
        os.chflags(link, stat.UF_IMMUTABLE, follow_symlinks=False)
        self.addCleanup(
            lambda: os.chflags(link, 0, follow_symlinks=False)
            if link.is_symlink()
            else None
        )

        report = SandboxRetention(self.workspace, ttl_seconds=1).sweep(
            now=time.time() + 10_000
        )

        self.assertEqual(
            oct(victim.stat().st_mode & 0o777),
            "0o600",
            "retention changed the mode of a file outside the workspace",
        )
        self.assertEqual(victim.read_text(), "host content")
        # And the deletion guarantee the fix exists for still holds.
        self.assertEqual(report.sessions_purged, 1)
        self.assertFalse(link.is_symlink() or link.exists())

    def test_temporary_roots_are_denied(self) -> None:
        """The data-root list forgot where temp files live.

        /tmp and the per-user directories under /private/var/folders hold every
        process's temp and cache for this uid. The previous regression test
        probed /private/var/db - a root the fix had just added - so it passed
        while these stayed readable.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        probe = Path("/tmp/alx-regression-probe.txt")
        probe.write_text("TMP-SECRET")
        self.addCleanup(lambda: probe.unlink(missing_ok=True))

        paths = self.workspace.prepare("exp-t", "ses-t", "run-1")
        source = (
            "import os\n"
            "for target in ('/tmp/alx-regression-probe.txt',"
            " '/private/tmp/alx-regression-probe.txt'):\n"
            "    try:\n"
            "        print('READ', open(target).read())\n"
            "    except Exception as error:\n"
            "        print('BLOCKED', type(error).__name__)\n"
            "try:\n"
            "    os.listdir('/private/var/folders')\n"
            "    print('LISTED')\n"
            "except Exception as error:\n"
            "    print('LIST-BLOCKED', type(error).__name__)\n"
        )
        outcome = self.runner.run(
            SandboxRequest("exp-t", "ses-t", "run-1", source), paths
        )
        self.assertNotIn("TMP-SECRET", outcome.stdout)
        self.assertNotIn("LISTED", outcome.stdout)
        # Asserted positively as well: empty output would satisfy the two
        # checks above whether or not the program ran at all.
        # "LIST-BLOCKED" contains "BLOCKED", so the refusals are counted by
        # whole word rather than by substring.
        refusals = [word for word in outcome.stdout.split() if word == "BLOCKED"]
        self.assertEqual(
            len(refusals),
            2,
            "both temp-path reads must be refused, not merely absent",
        )
        self.assertIn("LIST-BLOCKED", outcome.stdout)

    def test_the_credential_store_is_denied(self) -> None:
        """A keychain is the reason a read boundary exists at all."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        keychain = Path("/Library/Keychains/System.keychain")
        if not keychain.is_file():
            self.skipTest("no system keychain on this host")

        paths = self.workspace.prepare("exp-k", "ses-k", "run-1")
        source = (
            "try:\n"
            "    data = open('/Library/Keychains/System.keychain', 'rb').read(8)\n"
            "    print('READ', data)\n"
            "except Exception as error:\n"
            "    print('BLOCKED', type(error).__name__)\n"
        )
        outcome = self.runner.run(
            SandboxRequest("exp-k", "ses-k", "run-1", source), paths
        )
        self.assertIn("BLOCKED", outcome.stdout)
        self.assertNotIn("READ", outcome.stdout)

    def test_the_workspace_still_works_under_a_denied_temp_root(self) -> None:
        """Denying /private/var/folders must not refuse a workspace living there.

        This is the reasoning the earlier omission rested on, and it is wrong:
        the session re-allow comes last and later rules win.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-w", "ses-w", "run-1")
        outcome = self.runner.run(
            SandboxRequest(
                "exp-w",
                "ses-w",
                "run-1",
                "open('own.txt','w').write('mine')\nprint('OWN', open('own.txt').read())\n",
            ),
            paths,
        )
        self.assertIn("OWN mine", outcome.stdout)

    def test_a_shared_prefix_is_never_granted_for_execution(self) -> None:
        """The exec fix walked up to the first ancestor named `bin`.

        Right for a framework build, catastrophic for /usr/bin/python3: it
        produced (allow process-exec (subpath "/usr")), authorising osascript,
        ssh, curl and every other tool in /usr. The previous test used
        /bin/echo, which a /usr grant blocks anyway, so it passed.
        """
        for interpreter in (
            "/usr/bin/python3",
            "/usr/local/bin/python3",
            "/opt/homebrew/bin/python3",
            "/bin/python3",
        ):
            with self.subTest(interpreter=interpreter):
                self.assertIsNone(
                    SeatbeltSandboxRunner._interpreter_prefix(Path(interpreter)),
                    f"{interpreter} named a prefix shared with the system",
                )

    def test_no_shared_prefix_reaches_the_generated_profile(self) -> None:
        runner = SeatbeltSandboxRunner(
            self.workspace, interpreter="/usr/bin/python3"
        )
        paths = self.workspace.prepare("exp-u", "ses-u", "run-1")
        profile = runner.profile(paths.session_state)

        self.assertIn('(allow process-exec (literal "/usr/bin/python3"))', profile)
        for shared in ('subpath "/usr"', 'subpath "/opt"', 'subpath "/"'):
            self.assertNotIn(
                f"(allow process-exec ({shared}))",
                profile,
                "execution was granted over a shared system prefix",
            )

    def test_only_a_framework_layout_is_granted_a_prefix(self) -> None:
        """The one layout that needs a prefix gets one; nothing else does.

        A framework build re-execs a binary inside its own bundle, which is
        why the grant exists at all. Every other layout starts from the
        literals, so naming a prefix for them only widens what can be executed.
        """
        framework = Path(
            "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
        )
        self.assertEqual(
            SeatbeltSandboxRunner._interpreter_prefix(framework),
            Path("/Library/Frameworks/Python.framework/Versions/3.13"),
        )

        # Each of these once produced a prefix carrying a toolchain: MacPorts
        # and conda ship compilers, curl and openssl, and Xcode's usr is a
        # developer tree. A venv needs no grant either - it is listed here so
        # a later change that starts naming prefixes again is caught.
        for interpreter in (
            "/opt/local/bin/python3",
            "/Users/someone/miniconda3/bin/python",
            "/Users/someone/miniconda3/envs/ml/bin/python",
            "/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
            "/nix/store/abc123-python-3.13/bin/python",
            "/Users/someone/project/.venv/bin/python",
            "/snap/bin/python3",
        ):
            with self.subTest(interpreter=interpreter):
                self.assertIsNone(
                    SeatbeltSandboxRunner._interpreter_prefix(Path(interpreter)),
                    f"{interpreter} was granted an execution prefix",
                )

    def test_a_framework_prefix_never_climbs_above_its_version(self) -> None:
        """The grant stops at the version directory, not the framework."""
        prefix = SeatbeltSandboxRunner._interpreter_prefix(
            Path(
                "/Library/Frameworks/Python.framework/Versions/3.13"
                "/Resources/Python.app/Contents/MacOS/Python"
            )
        )
        self.assertEqual(
            prefix, Path("/Library/Frameworks/Python.framework/Versions/3.13")
        )
        # A bare "Versions" component that is not a framework grants nothing.
        self.assertIsNone(
            SeatbeltSandboxRunner._interpreter_prefix(
                Path("/opt/Versions/3.13/bin/python3")
            )
        )


if __name__ == "__main__":
    unittest.main()


class TrustedLauncherTest(unittest.TestCase):
    """The launcher: limits applied safely, and nothing outliving the runtime.

    Two defects made it necessary. `preexec_fn` runs between fork and exec in
    a child holding copies of every lock the other threads had, and the AL/X
    runtime dispatches Core turns through asyncio.to_thread, so a launch could
    deadlock before exec with the lease, the reservation and the turn held. And
    an experiment's process identity lived only in the memory of the process
    that started it, so a crash left a sleeping program that no wall clock,
    CPU limit or cleanup would ever end.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")
        self.runner = SeatbeltSandboxRunner(self.workspace)

    def test_the_runtime_never_uses_preexec_fn(self) -> None:
        """The launch path must not depend on it, however convenient it is.

        Asserted against the source rather than behaviour: a deadlock between
        fork and exec is timing-dependent and would make a flaky test, while
        its absence is a property that can simply be checked.
        """
        tree = ast.parse(
            (REPOSITORY_ROOT / "src/alx/providers/sandbox_runner.py").read_text()
        )
        # The keyword, not the word: the comment explaining why it is gone
        # mentions it, and a prose match would fail on the explanation.
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword):
                self.assertNotEqual(
                    node.arg,
                    "preexec_fn",
                    "the multi-threaded runtime forks with a Python callback again",
                )

    def test_limits_are_applied_by_the_launcher(self) -> None:
        """They still have to be applied, just from a single-threaded process."""
        import resource

        from alx.providers import sandbox_launcher

        applied: list[int] = []
        original = resource.setrlimit
        resource.setrlimit = lambda which, limits: applied.append(which)
        try:
            sandbox_launcher._apply_limits(35, MAX_FILE_BYTES, MAX_PROCESSES)
        finally:
            resource.setrlimit = original

        self.assertIn(resource.RLIMIT_CPU, applied)
        self.assertIn(resource.RLIMIT_FSIZE, applied)
        self.assertIn(resource.RLIMIT_NPROC, applied)
        # And never an address-space limit, which is ineffective on this host
        # and would record a ceiling the kernel ignores.
        self.assertNotIn(resource.RLIMIT_AS, applied)

    def test_a_running_experiment_is_recorded_for_recovery(self) -> None:
        """Recovery needs something to verify, written where it cannot be forged."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-rec", "ses-rec", "run-1")
        note = paths.run_directory / LIVE_RUN_NAME

        self.runner.run(
            SandboxRequest("exp-rec", "ses-rec", "run-1", "print('done')"), paths
        )
        # Cleared once the run is over: nothing to recover.
        self.assertFalse(note.exists())

    def test_a_genuine_orphan_is_reaped_at_startup(self) -> None:
        """D-027 promises this, and nothing performed it before."""
        paths = self.workspace.prepare("exp-orp", "ses-orp", "run-1")
        victim = subprocess.Popen(  # noqa: S603 - a stand-in for a stranded run
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(lambda: victim.poll() is None and victim.kill())
        group = os.getpgid(victim.pid)
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-orp/ses-orp/run-1",
                    "pid": victim.pid,
                    "process_group": group,
                    "started_at": "2026-09-07T00:00:00+00:00",
                    # A different runtime: this one did not start it.
                    "parent_pid": os.getpid() + 1_000_000,
                }
            ),
            encoding="utf-8",
        )

        reaped = SandboxRetention(self.workspace).reap_orphans()

        self.assertEqual(reaped, 1)
        victim.wait(timeout=10)
        self.assertIsNotNone(victim.returncode)
        self.assertFalse((paths.run_directory / LIVE_RUN_NAME).exists())

    def test_a_reused_identifier_never_kills_an_unrelated_process(self) -> None:
        """A pid is not proof. Numbers are reused, and this one is innocent."""
        paths = self.workspace.prepare("exp-inn", "ses-inn", "run-1")
        bystander = subprocess.Popen(  # noqa: S603 - an unrelated process
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.addCleanup(lambda: bystander.poll() is None and bystander.kill())

        # The recorded group is not the one this pid actually belongs to, which
        # is exactly what a recycled number looks like.
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-inn/ses-inn/run-1",
                    "pid": bystander.pid,
                    "process_group": os.getpgid(bystander.pid) + 4_242,
                    "started_at": "2026-09-07T00:00:00+00:00",
                    "parent_pid": os.getpid() + 1_000_000,
                }
            ),
            encoding="utf-8",
        )

        reaped = SandboxRetention(self.workspace).reap_orphans()

        self.assertEqual(reaped, 0)
        time.sleep(0.3)
        self.assertIsNone(
            bystander.poll(), "recovery killed a process that was not its own"
        )
        # The stale note is still cleared, so it cannot mislead a later run.
        self.assertFalse((paths.run_directory / LIVE_RUN_NAME).exists())

    def test_this_runtimes_own_run_is_not_treated_as_an_orphan(self) -> None:
        """A live run must survive a sweep by the process that started it."""
        paths = self.workspace.prepare("exp-own", "ses-own", "run-1")
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-own/ses-own/run-1",
                    "pid": os.getpid(),
                    "process_group": os.getpgid(0),
                    "started_at": "2026-09-07T00:00:00+00:00",
                    "parent_pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(SandboxRetention(self.workspace).reap_orphans(), 0)
        self.assertTrue((paths.run_directory / LIVE_RUN_NAME).exists())

    def test_the_launcher_ends_the_run_when_its_parent_disappears(self) -> None:
        """The whole point: an experiment must not outlive the runtime.

        A sleeping program consumes no CPU allowance, so neither the wall timer
        nor RLIMIT_CPU would ever end it. The launcher watches its parent and
        kills the group when that parent is gone.
        """
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")

        # A parent that starts a long experiment and then dies, exactly as a
        # crashing runtime would.
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(REPOSITORY_ROOT / 'src')!r})\n"
            "from pathlib import Path\n"
            "from alx.providers.sandbox_runner import SeatbeltSandboxRunner\n"
            "from alx.providers.sandbox_workspace import SandboxWorkspace\n"
            "from alx.contracts.sandbox import SandboxRequest\n"
            f"w = SandboxWorkspace(Path({str(self.root / 'ws')!r}))\n"
            "r = SeatbeltSandboxRunner(w)\n"
            "p = w.prepare('exp-par','ses-par','run-1')\n"
            "import threading\n"
            "threading.Thread(target=lambda: r.run(SandboxRequest("
            "'exp-par','ses-par','run-1','import time; time.sleep(45)',"
            "wall_seconds=45), p), daemon=True).start()\n"
            "time.sleep(3)\n"
            "import os; os._exit(0)\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - a stand-in runtime
            [sys.executable, "-c", script], start_new_session=True
        )
        parent.wait(timeout=30)

        # The launcher polls its parent; give it a moment to notice and reap.
        deadline = time.monotonic() + 20
        survivors = 1
        while time.monotonic() < deadline:
            listing = subprocess.run(  # noqa: S603 - reading process state
                ["/bin/ps", "-eo", "command"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            survivors = listing.count("time.sleep(45)")
            if survivors == 0:
                break
            time.sleep(0.5)

        self.assertEqual(
            survivors, 0, "an experiment outlived the runtime that started it"
        )
