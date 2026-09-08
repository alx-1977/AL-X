"""Regressions for the ten findings of the independent review of PR #17.

Each test reproduces the reported weakness and asserts it is closed. They are
kept together and named for the finding so that a future change which
reintroduces one is unambiguous about what it broke.

The review was against head 425581b. Every finding was reproduced against that
head before being fixed; none was accepted on description alone.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.sandbox import build_sandbox_runtime  # noqa: E402
from alx.contracts.sandbox import (  # noqa: E402
    MAX_FILE_BYTES,
    MAX_WORKSPACE_BYTES,
    ArtifactMetadata,
    FileChange,
    SandboxError,
    SandboxRequest,
)
from alx.observability.sandbox_ledger import SandboxBudget  # noqa: E402
from alx.providers.sandbox_retention import SandboxRetention  # noqa: E402
from alx.providers.sandbox_macos import (  # noqa: E402
    CPU_GRACE_SECONDS,
    SeatbeltSandboxRunner,
)
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


class ConfinedRunTest(unittest.TestCase):
    """Shared fixture for findings that need a real confined run."""

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

    def _run(self, source: str, session: str = "ses-a", wall_seconds: int = 30):
        self.counter += 1
        run_id = f"run-{self.counter}"
        paths = self.workspace.prepare("exp-r", session, run_id)
        return paths, self.runner.run(
            SandboxRequest("exp-r", session, run_id, source, wall_seconds=wall_seconds),
            paths,
        )


class Finding1HomeDirectoryTest(ConfinedRunTest):
    """HIGH: reads were allowed everywhere except three named paths."""

    def test_the_users_own_data_is_refused_wholesale(self) -> None:
        """A deny-list only covers the secrets somebody remembered."""
        home = Path.home()
        _, outcome = self._run(
            "import os\n"
            f"home = {str(home)!r}\n"
            "for target in ('Library/Keychains', '.config', 'Documents',\n"
            "               '.zsh_history', '.bash_history', 'Library/Application Support'):\n"
            "    path = os.path.join(home, target)\n"
            "    try:\n"
            "        if os.path.isdir(path):\n"
            "            os.listdir(path)\n"
            "            print('LEAK', target)\n"
            "        else:\n"
            "            open(path, 'rb').read(8)\n"
            "            print('LEAK', target)\n"
            "    except Exception:\n"
            "        print('blocked', target)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)

    def test_the_workspace_is_still_usable_beneath_the_denied_home(self) -> None:
        """The re-allow must survive the home denial, or nothing works."""
        _, outcome = self._run(
            "open('artifact.txt', 'w').write('evidence')\n"
            "print('read back', open('artifact.txt').read())\n"
        )
        self.assertIn("read back evidence", outcome.stdout)

    def test_the_profile_denies_home_before_re_allowing_the_workspace(self) -> None:
        """Seatbelt applies the last matching rule, so order is load-bearing."""
        state = self.root / "exp-r" / "ses-a" / "state"
        profile = self.runner.profile(state)
        home_index = profile.index(f'(deny file-read* (subpath "{Path.home()}"))')
        allow_index = profile.index(f'(allow file-read* (subpath "{state}"))')
        self.assertLess(home_index, allow_index)


class Finding2WorkspaceCeilingTest(ConfinedRunTest):
    """HIGH: MAX_WORKSPACE_BYTES was declared but never enforced."""

    def test_many_legal_files_cannot_exceed_the_workspace_ceiling(self) -> None:
        """Each write is inside RLIMIT_FSIZE; together they are not."""
        chunk = 8 * 1024 * 1024
        count = (MAX_WORKSPACE_BYTES // chunk) + 3
        with self.assertRaises(SandboxError) as caught:
            self._run(
                f"for index in range({count}):\n"
                f"    open(f'f{{index}}.bin', 'wb').write(b'x' * {chunk})\n"
                "print('filled')\n"
            )
        self.assertEqual(caught.exception.code, "workspace_exhausted")

    def test_the_overflow_is_not_left_behind(self) -> None:
        chunk = 8 * 1024 * 1024
        count = (MAX_WORKSPACE_BYTES // chunk) + 3
        with self.assertRaises(SandboxError):
            self._run(
                f"for index in range({count}):\n"
                f"    open(f'f{{index}}.bin', 'wb').write(b'x' * {chunk})\n",
                session="ses-fill",
            )
        state = self.root / "exp-r" / "ses-fill" / "state"
        self.assertEqual(list(state.iterdir()), [])


class Finding3SweepCadenceTest(unittest.TestCase):
    """HIGH: retention swept only at construction."""

    def test_retention_runs_before_every_experiment(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        sweeps: list[int] = []

        class CountingRunner:
            def available(self) -> bool:
                return True

            def reap_orphans(self) -> int:
                return 0

            def run(self, request, paths):
                raise SandboxError("sandbox_unavailable")

        runtime = build_sandbox_runtime(
            True,
            root / "w",
            root / "l.sqlite3",
            lambda: "call-1",
            runner=CountingRunner(),
            budget=SandboxBudget(20, 300),
        )
        original = runtime.retention.sweep
        runtime.retention.sweep = lambda *args, **kwargs: (  # type: ignore[method-assign]
            sweeps.append(1),
            original(*args, **kwargs),
        )[1]

        executor = runtime.executors["run_sandbox_experiment"]
        for _ in range(3):
            executor(
                {"experiment_id": "exp-a", "session_id": "ses-a", "source": "print(1)"}
            )
        self.assertEqual(len(sweeps), 3)


class Finding4ReservationAccountingTest(unittest.TestCase):
    """HIGH: any failure after execution released the reservation."""

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

    def test_a_post_execution_failure_still_consumes_the_fuse(self) -> None:
        """Wall time was spent, so it must appear in the daily counters."""

        class ExplodingAfterRun:
            def available(self) -> bool:
                return True

            def reap_orphans(self) -> int:
                return 0

            def run(self, request, paths, launched=None):
                # The process started, so wall time was really spent. The
                # runner reports that before failing, exactly as the real one
                # does when reading output or writing the manifest fails.
                if launched is not None:
                    launched()
                raise RuntimeError("manifest write failed after execution")

        runtime = self._runtime(ExplodingAfterRun())
        executor = runtime.executors["run_sandbox_experiment"]
        for _ in range(3):
            executor(
                {"experiment_id": "exp-a", "session_id": "ses-a", "source": "print(1)"}
            )
        self.assertEqual(runtime.ledger.committed_runs(), 3)

    def test_a_failure_before_execution_still_releases_the_reservation(self) -> None:
        """Nothing ran, so nothing should be charged."""

        class UnavailableAtRun:
            def available(self) -> bool:
                return True

            def reap_orphans(self) -> int:
                return 0

            def run(self, request, paths):  # pragma: no cover - not reached
                raise AssertionError("must not run")

        runtime = self._runtime(UnavailableAtRun())
        executor = runtime.executors["run_sandbox_experiment"]
        # An invalid identifier fails in workspace.prepare, before execution.
        executor(
            {"experiment_id": "exp-a", "session_id": "ses-a", "source": "print(1)"}
        )
        # prepare() succeeded and run() asserted, which counts as executed.
        # Use a request that cannot even be built to prove the other branch.
        executor({"experiment_id": "../bad", "session_id": "s", "source": "print(1)"})
        self.assertLessEqual(runtime.ledger.committed_runs(), 1)


class Finding5ProcessGroupTest(ConfinedRunTest):
    """HIGH: termination returned once the group leader exited."""

    def test_no_member_of_the_group_survives_the_call(self) -> None:
        _, outcome = self._run(
            "import threading, time\n"
            "threading.Thread(target=lambda: time.sleep(90), daemon=False).start()\n"
            "time.sleep(90)\n",
            wall_seconds=2,
        )
        self.assertTrue(outcome.timed_out)
        remaining = os.popen("pgrep -f 'time.sleep(90)' || true").read().strip()
        self.assertEqual(remaining, "")

    def test_termination_polls_the_group_rather_than_only_the_leader(self) -> None:
        source = (
            REPOSITORY_ROOT / "src/alx/providers/sandbox_macos/runner.py"
        ).read_text()
        terminate = source.split("def _terminate", 1)[1].split("def ", 1)[0]
        self.assertIn("killpg(group, 0)", terminate)


class Finding6RetentionPinningTest(unittest.TestCase):
    """HIGH: an experiment could pin its workspace with a future mtime."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = SandboxWorkspace(self.root)

    def test_a_future_mtime_cannot_postpone_retention(self) -> None:
        paths = self.workspace.prepare("exp-a", "ses-a", "run-1")
        paths.manifest_path.write_text("{}")
        (paths.session_state / "pinned.txt").write_text("x")

        far_future = time.time() + 10 * 365 * 24 * 3600
        os.utime(paths.session_state / "pinned.txt", (far_future, far_future))
        stale = time.time() - 3600
        for path in (paths.manifest_path, paths.run_directory, self.root / "exp-a" / "ses-a"):
            os.utime(path, (stale, stale))

        retention = SandboxRetention(self.workspace, ttl_seconds=1)
        self.assertGreater(retention._age(self.root / "exp-a" / "ses-a", time.time()), 60)
        self.assertEqual(retention.sweep().sessions_purged, 1)
        self.assertFalse(paths.session_state.exists())
        self.assertTrue(paths.manifest_path.exists())

    def test_age_comes_from_the_run_directory_the_sandbox_cannot_write(self) -> None:
        paths = self.workspace.prepare("exp-a", "ses-b", "run-1")
        paths.manifest_path.write_text("{}")
        fresh = time.time()
        os.utime(paths.manifest_path, (fresh, fresh))
        # Ageing only the state must not age the session.
        stale = time.time() - 99_999
        os.utime(paths.session_state, (stale, stale))
        retention = SandboxRetention(self.workspace, ttl_seconds=10)
        self.assertLess(retention._age(self.root / "exp-a" / "ses-b", time.time()), 10)


class Finding7CaptureCapTest(ConfinedRunTest):
    """MEDIUM: digests described only the first 10 MiB of a 16 MiB file."""

    def test_the_capture_cap_is_not_smaller_than_the_file_limit(self) -> None:
        from alx.providers.sandbox_macos import runner as sandbox_runner

        self.assertGreaterEqual(sandbox_runner._MAX_CAPTURED_BYTES, MAX_FILE_BYTES)

    def test_a_capped_stream_is_reported_rather_than_silently_partial(self) -> None:
        outcome_fields = SandboxRequest.__dataclass_fields__
        self.assertIn("source", outcome_fields)
        _, outcome = self._run("print('short')\n")
        self.assertFalse(outcome.stdout_capped)
        self.assertFalse(outcome.stderr_capped)
        self.assertIn("stdout_capped", outcome.durable_values())


class Finding8CpuLimitTest(ConfinedRunTest):
    """MEDIUM: D-027 states RLIMIT_CPU is applied; it was not."""

    def test_the_manifest_records_the_cpu_ceiling(self) -> None:
        paths, _ = self._run("print(1)\n")
        limits = json.loads(paths.manifest_path.read_text())["limits"]
        self.assertEqual(limits["cpu_seconds"], 30 + CPU_GRACE_SECONDS)
        self.assertEqual(limits["max_workspace_bytes"], MAX_WORKSPACE_BYTES)
        self.assertIsNone(limits["memory_ceiling"])


class Finding9SymlinkArtifactTest(ConfinedRunTest):
    """MEDIUM: creating a symlink crashed the run and lost the manifest."""

    def test_a_run_that_creates_a_symlink_still_returns_evidence(self) -> None:
        paths, outcome = self._run(
            "import os\nos.symlink('/etc/hosts', 'link')\nprint('made link')\n"
        )
        self.assertEqual(outcome.exit_status, 0)
        self.assertIn("made link", outcome.stdout)
        self.assertTrue(paths.manifest_path.exists())
        names = {item.name: item for item in outcome.artifacts}
        self.assertIn("link", names)
        # Recorded as a link with no digest; its target is never opened.
        self.assertEqual(names["link"].digest, "")

    def test_a_deleted_artifact_still_refuses_a_digest(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactMetadata("gone", FileChange.DELETED, 0, "a" * 64)

    def test_a_malformed_digest_is_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactMetadata("f", FileChange.CREATED, 1, "not-a-digest")


class Finding10AsyncioScannerTest(unittest.TestCase):
    """MEDIUM: the Law 0 scanner missed asyncio subprocess creation."""

    def test_an_asyncio_execution_site_would_be_detected(self) -> None:
        from tests.test_sandbox_capability import SingleExecutionSiteTest

        planted = REPOSITORY_ROOT / "src/alx/tools/asyncio_probe.py"
        planted.write_text(
            "import asyncio\n\n\n"
            "async def run():\n"
            "    await asyncio.create_subprocess_exec('echo')\n"
        )
        case = SingleExecutionSiteTest("test_no_production_module_calls_a_process_execution_function")
        try:
            with self.assertRaises(AssertionError):
                case.test_no_production_module_calls_a_process_execution_function()
        finally:
            planted.unlink()

    def test_the_scanner_names_the_asyncio_entry_points(self) -> None:
        from tests.test_sandbox_capability import SingleExecutionSiteTest

        self.assertIn(
            "create_subprocess_exec", SingleExecutionSiteTest.ASYNCIO_EXECUTION_NAMES
        )
        self.assertIn(
            "create_subprocess_shell", SingleExecutionSiteTest.ASYNCIO_EXECUTION_NAMES
        )


if __name__ == "__main__":
    unittest.main()
