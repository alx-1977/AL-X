"""D-027 resource bounds and the traversal defences, proved adversarially.

Two kinds of test live here. The bound tests run real programs that try to
exceed a limit and assert they were stopped. The traversal tests attack the
path and hash-walk boundaries, which are pure logic and need no sandbox.

One test asserts a *negative*: that no memory ceiling is claimed. D-027 records
that macOS has none, and a suite that quietly started asserting one would be
the first step towards believing it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.sandbox import (  # noqa: E402
    MAX_REPORTED_ARTIFACTS,
    MAX_STDOUT_CHARACTERS,
    SandboxError,
    SandboxRequest,
)
from alx.providers.sandbox_runner import SeatbeltSandboxRunner  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


class SandboxBoundsTest(unittest.TestCase):
    """Real programs, real limits."""

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

    def _run(self, source: str, wall_seconds: int = 30):
        self.counter += 1
        run_id = f"run-{self.counter}"
        paths = self.workspace.prepare("exp-b", "ses-b", run_id)
        return self.runner.run(
            SandboxRequest("exp-b", "ses-b", run_id, source, wall_seconds=wall_seconds),
            paths,
        )

    def test_an_endless_loop_is_stopped_and_reported_as_a_timeout(self) -> None:
        outcome = self._run("while True:\n    pass\n", wall_seconds=2)
        self.assertTrue(outcome.timed_out)
        self.assertTrue(outcome.signalled)
        self.assertLess(outcome.wall_seconds_used, 15)

    def test_a_timed_out_run_leaves_no_surviving_process(self) -> None:
        """A run that outlived its call would be work nobody accounts for.

        A thread rather than a subprocess: RLIMIT_NPROC refuses the fork on
        this host, so a subprocess variant would exercise the process limit and
        finish immediately, proving nothing about the timeout. The thread keeps
        the run genuinely alive until the wall clock stops it.
        """
        outcome = self._run(
            "import threading, time\n"
            "threading.Thread(target=lambda: time.sleep(120), daemon=False).start()\n"
            "time.sleep(120)\n",
            wall_seconds=2,
        )
        self.assertTrue(outcome.timed_out)
        self.assertTrue(outcome.signalled)
        self.assertLess(outcome.wall_seconds_used, 15)
        # The process group was killed; nothing from this run is still running.
        remaining = os.popen("pgrep -f 'time.sleep(120)' || true").read().strip()
        self.assertEqual(remaining, "")

    def test_the_process_limit_refuses_a_fork_bomb(self) -> None:
        """RLIMIT_NPROC, verified by a program that tries to spawn helpers.

        On this host the limit is already exhausted by the confined process
        itself, so the very first spawn is refused. That is the limit working:
        what matters is that an experiment cannot multiply itself.
        """
        outcome = self._run(
            "import subprocess, sys\n"
            "spawned = 0\n"
            "try:\n"
            "    for _ in range(64):\n"
            "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "        spawned += 1\n"
            "except Exception as error:\n"
            "    pass\n"
            "print('spawned', spawned)\n"
        )
        self.assertIn("spawned", outcome.stdout)
        count = int(outcome.stdout.strip().split()[-1])
        self.assertLess(count, 64)

    def test_an_oversized_file_is_refused_by_the_file_size_limit(self) -> None:
        outcome = self._run(
            "try:\n"
            "    open('big.bin', 'wb').write(b'x' * (64 * 1024 * 1024))\n"
            "    print('WROTE')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("WROTE", outcome.stdout)
        self.assertIn("blocked", outcome.stdout)

    def test_flooded_output_is_truncated_and_the_omission_reported(self) -> None:
        outcome = self._run("print('x' * 200000)\n")
        self.assertEqual(len(outcome.stdout), MAX_STDOUT_CHARACTERS)
        self.assertGreater(outcome.stdout_omitted_characters, 0)
        # The digest describes the whole stream, not the truncated view.
        self.assertGreater(outcome.stdout_byte_size, MAX_STDOUT_CHARACTERS)

    def test_many_artifacts_are_capped_and_the_omission_reported(self) -> None:
        outcome = self._run(
            f"for index in range({MAX_REPORTED_ARTIFACTS + 20}):\n"
            "    open(f'file-{index}.txt', 'w').write(str(index))\n"
        )
        self.assertEqual(len(outcome.artifacts), MAX_REPORTED_ARTIFACTS)
        self.assertEqual(outcome.artifacts_omitted, 20)

    def test_no_memory_ceiling_is_claimed(self) -> None:
        """D-027: macOS V1 has no hard RAM ceiling, and must not pretend to.

        RLIMIT_AS is ineffective on macOS arm64. Applying it would assert a
        bound the kernel ignores, so it is deliberately absent, and the
        manifest records the ceiling as null rather than omitting the field.
        """
        import resource

        applied: list[int] = []
        original = resource.setrlimit

        def record(which: int, limits: tuple[int, int]) -> None:
            # Recorded, never applied: applying RLIMIT_NPROC here would limit
            # the test runner itself and break every later test in the process.
            applied.append(which)

        resource.setrlimit = record  # type: ignore[assignment]
        try:
            SeatbeltSandboxRunner._limits()
        finally:
            resource.setrlimit = original  # type: ignore[assignment]

        self.assertIn(resource.RLIMIT_FSIZE, applied)
        self.assertIn(resource.RLIMIT_NPROC, applied)
        # The assertion that matters: no memory limit is set, because setting
        # one on this platform would assert a bound the kernel ignores.
        self.assertNotIn(resource.RLIMIT_AS, applied)

        # And the manifest records the ceiling as explicitly absent rather than
        # omitting the field, so no reader can infer one.
        paths = self.workspace.prepare("exp-b", "ses-mem", "run-mem")
        self.runner.run(
            SandboxRequest("exp-b", "ses-mem", "run-mem", "print(1)"), paths
        )
        import json

        manifest = json.loads(paths.manifest_path.read_text())
        self.assertIn("memory_ceiling", manifest["limits"])
        self.assertIsNone(manifest["limits"]["memory_ceiling"])

    def test_wall_seconds_beyond_the_maximum_are_refused_by_the_contract(self) -> None:
        with self.assertRaises(ValueError):
            SandboxRequest("exp-b", "ses-b", "run-x", "print(1)", wall_seconds=600)


class SandboxTraversalTest(unittest.TestCase):
    """Path and hash-walk attacks. No sandbox required."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = SandboxWorkspace(self.root)
        self.addCleanup(self.directory.cleanup)

    def test_traversal_identifiers_are_refused(self) -> None:
        for experiment, session in (
            ("../escape", "ses-a"),
            ("exp-a", "../../etc"),
            ("exp/a", "ses-a"),
            ("..", "ses-a"),
            ("", "ses-a"),
            ("exp-a", "ses a"),
            ("EXP", "ses-a"),
        ):
            with self.subTest(experiment=experiment, session=session):
                with self.assertRaises(SandboxError):
                    self.workspace.prepare(experiment, session, "run-1")

    def test_an_entry_filename_cannot_contain_a_directory(self) -> None:
        for name in ("../escape.py", "sub/dir.py", "/etc/passwd", "run.sh", ".py"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    SandboxRequest("exp-a", "ses-a", "run-1", "print(1)", entry_filename=name)

    def test_the_hash_walk_never_follows_a_symbolic_link(self) -> None:
        """A followed link would read what the sandbox itself cannot.

        The walk runs in the parent, with the parent's authority. If it
        followed links, an experiment could link to a secret and have its
        content hashed into the run's own evidence.
        """
        paths = self.workspace.prepare("exp-a", "ses-a", "run-1")
        secret = self.root / "outside-secret.txt"
        secret.write_text("a value the sandbox may not read")
        (paths.session_state / "link.txt").symlink_to(secret)

        walked = self.workspace.walk(paths.session_state)
        self.assertIn("link.txt", walked)
        digest, size = walked["link.txt"]
        self.assertEqual(digest, "")
        self.assertEqual(size, 0)

        import hashlib

        secret_digest = hashlib.sha256(secret.read_bytes()).hexdigest()
        self.assertNotIn(secret_digest, {value[0] for value in walked.values()})

    def test_a_linked_directory_is_not_descended_into(self) -> None:
        paths = self.workspace.prepare("exp-a", "ses-a", "run-1")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("private")
        (paths.session_state / "linked").symlink_to(outside, target_is_directory=True)

        walked = self.workspace.walk(paths.session_state)
        self.assertNotIn("linked/secret.txt", walked)

    def test_purging_refuses_a_path_outside_the_sandbox_root(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "keep.txt").write_text("must survive")
        with self.assertRaises(SandboxError):
            self.workspace.purge_transient(outside)
        self.assertTrue((outside / "keep.txt").exists())

    def test_purging_does_not_follow_a_link_out_of_the_workspace(self) -> None:
        """The classic cleanup incident: rmtree through a symlink."""
        paths = self.workspace.prepare("exp-a", "ses-a", "run-1")
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "precious.txt").write_text("must survive")
        (paths.session_state / "escape").symlink_to(outside, target_is_directory=True)

        self.workspace.purge_transient(self.root / "exp-a" / "ses-a")
        self.assertTrue((outside / "precious.txt").exists())


if __name__ == "__main__":
    unittest.main()
