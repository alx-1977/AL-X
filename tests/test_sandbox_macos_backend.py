"""macOS Seatbelt backend, launcher and orphan-recovery regressions."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.sandbox import MAX_FILE_BYTES, MAX_PROCESSES, SandboxRequest  # noqa: E402
from alx.providers.sandbox_macos import (  # noqa: E402
    LIVE_RUN_NAME,
    SeatbeltSandboxRunner,
    process_identity,
)
from alx.providers.sandbox_macos import launcher as sandbox_launcher  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402
from alx.tools.sandbox import RUN_SANDBOX_EXPERIMENT, build_sandbox_executors  # noqa: E402


@unittest.skipUnless(sys.platform == "darwin", "requires the macOS Sandbox backend")
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
            (
                REPOSITORY_ROOT / "src/alx/providers/sandbox_macos/runner.py"
            ).read_text()
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
        self.addCleanup(self._stop_process, victim)
        group = os.getpgid(victim.pid)
        identity = process_identity(victim.pid)
        self.assertIsNotNone(identity)
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-orp/ses-orp/run-1",
                    "pid": victim.pid,
                    "process_group": group,
                    "process_started_at": list(identity.started_at),
                    "started_at": "2026-09-07T00:00:00+00:00",
                    # A different runtime: this one did not start it.
                    "parent_pid": os.getpid() + 1_000_000,
                    "parent_process_group": os.getpgid(0),
                    "parent_started_at": [0, 0],
                }
            ),
            encoding="utf-8",
        )

        reaped = self.runner.reap_orphans()

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
        self.addCleanup(self._stop_process, bystander)

        identity = process_identity(bystander.pid)
        self.assertIsNotNone(identity)
        # A recycled PID and group can numerically match. Its kernel start time
        # cannot match the process that originally held them.
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-inn/ses-inn/run-1",
                    "pid": bystander.pid,
                    "process_group": os.getpgid(bystander.pid),
                    "process_started_at": [identity.started_at[0] - 1, 0],
                    "started_at": "2026-09-07T00:00:00+00:00",
                    "parent_pid": os.getpid() + 1_000_000,
                    "parent_process_group": os.getpgid(0),
                    "parent_started_at": [0, 0],
                }
            ),
            encoding="utf-8",
        )

        reaped = self.runner.reap_orphans()

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
        identity = process_identity(os.getpid())
        self.assertIsNotNone(identity)
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-own/ses-own/run-1",
                    "pid": os.getpid(),
                    "process_group": os.getpgid(0),
                    "process_started_at": list(identity.started_at),
                    "started_at": "2026-09-07T00:00:00+00:00",
                    "parent_pid": os.getpid(),
                    "parent_process_group": os.getpgid(0),
                    "parent_started_at": list(identity.started_at),
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.runner.reap_orphans(), 0)
        self.assertTrue((paths.run_directory / LIVE_RUN_NAME).exists())

    def test_a_live_parent_need_not_lead_its_process_group(self) -> None:
        paths = self.workspace.prepare("exp-parent", "ses-parent", "run-1")
        parent = subprocess.Popen(  # noqa: S603 - a stand-in live runtime
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        experiment = subprocess.Popen(  # noqa: S603 - its active experiment
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.addCleanup(self._stop_process, parent)
        self.addCleanup(self._stop_process, experiment)
        parent_identity = process_identity(parent.pid)
        experiment_identity = process_identity(experiment.pid)
        self.assertIsNotNone(parent_identity)
        self.assertIsNotNone(experiment_identity)
        self.assertNotEqual(parent.pid, parent_identity.process_group)
        (paths.run_directory / LIVE_RUN_NAME).write_text(
            json.dumps(
                {
                    "identity": "exp-parent/ses-parent/run-1",
                    "pid": experiment.pid,
                    "process_group": experiment_identity.process_group,
                    "process_started_at": list(experiment_identity.started_at),
                    "started_at": "2026-09-07T00:00:00+00:00",
                    "parent_pid": parent.pid,
                    "parent_process_group": parent_identity.process_group,
                    "parent_started_at": list(parent_identity.started_at),
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.runner.reap_orphans(), 0)
        self.assertIsNone(experiment.poll())
        self.assertTrue((paths.run_directory / LIVE_RUN_NAME).exists())

    def test_a_failed_inner_launch_is_a_capability_failure_not_an_outcome(self) -> None:
        """A program that never started has no exit status to report."""
        unavailable = self.root / "not-executable"
        unavailable.write_text("not an executable", encoding="utf-8")
        runner = SeatbeltSandboxRunner(
            self.workspace,
            sandbox_exec=str(unavailable),
        )

        def run(request: SandboxRequest):
            paths = self.workspace.prepare(
                request.experiment_id, request.session_id, request.run_id
            )
            return runner.run(request, paths)

        executor = build_sandbox_executors(
            run, lambda: "call-launch-failed", lambda: "run-launch-failed"
        )[RUN_SANDBOX_EXPERIMENT]
        result = executor(
            {
                "experiment_id": "exp-launch-failed",
                "session_id": "ses-launch-failed",
                "source": "print('never ran')",
            }
        )

        self.assertIs(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "sandbox_unavailable")
        self.assertNotIn("exit_status", result.values)

    def test_a_program_exit_of_124_is_not_a_timeout(self) -> None:
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-124", "ses-124", "run-1")
        outcome = self.runner.run(
            SandboxRequest(
                "exp-124",
                "ses-124",
                "run-1",
                "raise SystemExit(124)",
                wall_seconds=5,
            ),
            paths,
        )
        self.assertEqual(outcome.exit_status, 124)
        self.assertFalse(outcome.signalled)
        self.assertFalse(outcome.timed_out)

    def test_the_real_asyncio_to_thread_path_completes(self) -> None:
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")

        async def run() -> object:
            paths = self.workspace.prepare("exp-async", "ses-async", "run-1")
            return await asyncio.to_thread(
                self.runner.run,
                SandboxRequest(
                    "exp-async",
                    "ses-async",
                    "run-1",
                    "print('threaded')",
                    wall_seconds=5,
                ),
                paths,
            )

        outcome = asyncio.run(run())
        self.assertEqual(outcome.exit_status, 0)
        self.assertIn("threaded", outcome.stdout)

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
