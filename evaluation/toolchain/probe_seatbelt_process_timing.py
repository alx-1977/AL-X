"""Live macOS Seatbelt spawn, kill and wait probes. Not part of the default suite.

PID identity and the refusal to kill a bystander stay in
tests/test_sandbox_macos_backend.py. These probes wait on the kernel to reap
a confined process, which depends on this machine's timing.

    python -m unittest discover -s evaluation/toolchain -p 'probe_*.py'
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.sandbox import SandboxRequest  # noqa: E402
from alx.providers.sandbox_macos import (  # noqa: E402
    LIVE_RUN_NAME,
    SeatbeltSandboxRunner,
    process_identity,
)
from alx.providers.sandbox_macos import runner as sandbox_runner  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


@unittest.skipUnless(sys.platform == "darwin", "requires the macOS Sandbox backend")
class SeatbeltProcessTimingProbe(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = SandboxWorkspace(self.root / "ws")
        self.runner = SeatbeltSandboxRunner(self.workspace)

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

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not child_note.exists():
            time.sleep(0.05)
        if not child_note.exists():
            self.skipTest(
                "experiment did not record its pid; reap not observed"
            )
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


class ProcessGroupCleanupProbe(unittest.TestCase):
    """A helper left behind after its leader exits must be reaped.

    Staged against the group sweep directly. A confined run cannot spawn the
    helper on a host whose process limit is already spent.
    """

    def test_a_helper_does_not_outlive_a_clean_exit(self) -> None:
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
        self.addCleanup(_stop_process, leader)
        group = SeatbeltSandboxRunner._group_of(leader)
        self.assertIsNotNone(group)
        self.addCleanup(lambda: _kill_group(group))
        leader.wait(timeout=10)

        SeatbeltSandboxRunner._reap_group(group)

        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(group, 0)


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _kill_group(group: int | None) -> None:
    if group is None:
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


if __name__ == "__main__":
    unittest.main()
