"""macOS Seatbelt backend, launcher and orphan-recovery regressions."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import signal
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
from alx.providers.sandbox_macos import runner as sandbox_runner  # noqa: E402
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

    def test_the_launcher_ends_the_run_when_its_parent_disappears(self) -> None:
        """Use recorded kernel identity, never source text in a ps listing."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")

        parent, note = self._running_runtime(
            "import time; time.sleep(45)", "run-parent-loss"
        )
        record = json.loads(note.read_text(encoding="utf-8"))

        parent.kill()
        parent.wait(timeout=10)

        self._wait_for_process_to_end(record["pid"])

    def test_parent_loss_during_the_first_report_reaps_the_experiment(self) -> None:
        """A runtime can die after launch but before learning the child's pid."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        child_note = (
            self.root / "ws" / "exp-early" / "ses-early" / "state"
            / "experiment.pid"
        )
        source = (
            "import os, pathlib, time\n"
            f"pathlib.Path({str(child_note)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(45)\n"
        )
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(REPOSITORY_ROOT / 'src')!r})\n"
            "from pathlib import Path\n"
            "from alx.providers.sandbox_macos import SeatbeltSandboxRunner\n"
            "from alx.providers.sandbox_workspace import SandboxWorkspace\n"
            "from alx.contracts.sandbox import SandboxRequest\n"
            f"w = SandboxWorkspace(Path({str(self.root / 'ws')!r}))\n"
            "r = SeatbeltSandboxRunner(w)\n"
            "p = w.prepare('exp-early','ses-early','run-early')\n"
            "r.run(SandboxRequest('exp-early','ses-early','run-early',"
            f"{source!r},wall_seconds=45), p, lambda: os._exit(0))\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - a stand-in runtime
            [sys.executable, "-c", script], start_new_session=True
        )
        self.addCleanup(self._stop_process, parent)
        parent.wait(timeout=15)

        # The child may be killed before its first instruction. If it did run,
        # its exact kernel identity must disappear promptly despite there being
        # no live-run note for startup recovery to use.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not child_note.exists():
            time.sleep(0.05)
        if child_note.exists():
            self._wait_for_process_to_end(
                int(child_note.read_text(encoding="utf-8"))
            )

    def test_the_launcher_reaps_descendants_when_the_runtime_dies(self) -> None:
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        child_note = (
            self.root / "ws" / "exp-live" / "ses-live" / "state"
            / "descendant.pid"
        )
        source = (
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(45)'])\n"
            f"pathlib.Path({str(child_note)!r}).write_text(str(child.pid))\n"
            "time.sleep(45)\n"
        )
        parent, note = self._running_runtime(
            source, "run-descendants", process_limit=1_000
        )
        record = json.loads(note.read_text(encoding="utf-8"))
        self._wait_for_path(child_note)
        descendant_pid = int(child_note.read_text(encoding="utf-8"))

        parent.kill()
        parent.wait(timeout=10)

        self._wait_for_process_to_end(record["pid"])
        self._wait_for_process_to_end(descendant_pid)

    def test_an_unexpected_launcher_death_is_reaped_by_the_live_runtime(self) -> None:
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        parent, note = self._running_runtime(
            "import time; time.sleep(45)", "run-launcher-loss"
        )
        record = json.loads(note.read_text(encoding="utf-8"))

        os.kill(record["launcher_pid"], signal.SIGKILL)

        parent.wait(timeout=15)
        self._wait_for_process_to_end(record["pid"])

    def test_identity_is_durable_before_the_parent_reads_the_first_report(self) -> None:
        """Launcher death after spawn still leaves verified cleanup identity."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-gap", "ses-gap", "run-gap")
        note = paths.run_directory / LIVE_RUN_NAME
        recorded: dict[str, object] = {}

        def kill_launcher_after_note() -> None:
            self._wait_for_path(note)
            recorded.update(json.loads(note.read_text(encoding="utf-8")))
            os.kill(int(recorded["launcher_pid"]), signal.SIGKILL)

        self.runner.run(
            SandboxRequest(
                "exp-gap",
                "ses-gap",
                "run-gap",
                "import time; time.sleep(45)",
                wall_seconds=45,
            ),
            paths,
            kill_launcher_after_note,
        )

        self.assertIn("process_started_at", recorded)
        self._wait_for_process_to_end(int(recorded["pid"]))
        self.assertFalse(note.exists())

    def test_the_initial_launcher_report_has_a_wall_clock_backstop(self) -> None:
        """A stopped launcher cannot hold the Core turn indefinitely."""
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        stalled_launcher = self.root / "stalled_launcher.py"
        stalled_launcher.write_text(
            "import os, signal\nos.kill(os.getpid(), signal.SIGSTOP)\n",
            encoding="utf-8",
        )
        paths = self.workspace.prepare("exp-stop", "ses-stop", "run-stop")
        original_launcher = sandbox_runner._LAUNCHER
        original_launch_grace = sandbox_runner._LAUNCH_GRACE
        original_term_grace = sandbox_runner._TERM_GRACE_SECONDS
        sandbox_runner._LAUNCHER = stalled_launcher
        sandbox_runner._LAUNCH_GRACE = 0.2
        sandbox_runner._TERM_GRACE_SECONDS = 0.2
        started = time.monotonic()
        try:
            outcome = self.runner.run(
                SandboxRequest(
                    "exp-stop",
                    "ses-stop",
                    "run-stop",
                    "print('never reached')",
                    wall_seconds=1,
                ),
                paths,
            )
        finally:
            sandbox_runner._LAUNCHER = original_launcher
            sandbox_runner._LAUNCH_GRACE = original_launch_grace
            sandbox_runner._TERM_GRACE_SECONDS = original_term_grace

        self.assertTrue(outcome.timed_out)
        self.assertLess(time.monotonic() - started, 4)

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

    def test_a_real_timeout_is_distinct_from_exit_124(self) -> None:
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        paths = self.workspace.prepare("exp-to", "ses-to", "run-1")
        outcome = self.runner.run(
            SandboxRequest(
                "exp-to",
                "ses-to",
                "run-1",
                "import time; time.sleep(10)",
                wall_seconds=1,
            ),
            paths,
        )
        self.assertEqual(outcome.exit_status, -signal.SIGKILL)
        self.assertTrue(outcome.signalled)
        self.assertTrue(outcome.timed_out)

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

    def _running_runtime(
        self, source: str, run_id: str, process_limit: int | None = None
    ) -> tuple[subprocess.Popen, Path]:
        limit_override = (
            f"sandbox_runner.MAX_PROCESSES = {process_limit}\n"
            if process_limit is not None
            else ""
        )
        script = (
            "import asyncio, sys\n"
            f"sys.path.insert(0, {str(REPOSITORY_ROOT / 'src')!r})\n"
            "from pathlib import Path\n"
            "from alx.providers.sandbox_macos import runner as sandbox_runner\n"
            "from alx.providers.sandbox_macos import SeatbeltSandboxRunner\n"
            "from alx.providers.sandbox_workspace import SandboxWorkspace\n"
            "from alx.contracts.sandbox import SandboxRequest\n"
            f"{limit_override}"
            f"w = SandboxWorkspace(Path({str(self.root / 'ws')!r}))\n"
            "r = SeatbeltSandboxRunner(w)\n"
            f"p = w.prepare('exp-live','ses-live',{run_id!r})\n"
            "async def main():\n"
            "    await asyncio.to_thread(r.run, SandboxRequest("
            f"'exp-live','ses-live',{run_id!r},{source!r},wall_seconds=45), p)\n"
            "asyncio.run(main())\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - a stand-in runtime
            [sys.executable, "-c", script], start_new_session=True
        )
        self.addCleanup(self._stop_process, parent)
        note = (
            self.root
            / "ws"
            / "exp-live"
            / "ses-live"
            / "runs"
            / run_id
            / LIVE_RUN_NAME
        )
        self._wait_for_path(note)
        record = json.loads(note.read_text(encoding="utf-8"))
        self.addCleanup(
            lambda: self._kill_group_if_present(record.get("process_group"))
        )
        return parent, note

    def _wait_for_path(self, path: Path) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {path.name}")

    def _wait_for_process_to_end(self, pid: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process_identity(pid) is None:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} survived cleanup")

    @staticmethod
    def _kill_group_if_present(group: object) -> None:
        if not isinstance(group, int) or isinstance(group, bool):
            return
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
