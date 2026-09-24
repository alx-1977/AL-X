"""AL/X starts from committed main, whatever the Coding Agent left in the checkout.

The Coding Agent edits the canonical checkout on a feature branch (D-033). If
the runtime imported that checkout's `src/`, a failed edit to startup code
would leave AL/X unable to start after a restart, and so unable to use the
repository authority that recovers the checkout. `scripts/alx` therefore runs
code extracted from local `main` with git alone, importing nothing first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap import live_voice  # noqa: E402


def git(repository: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


class LauncherRunsCommittedMain(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.test")
        git(self.root, "config", "user.name", "test")
        package = self.root / "src/alx"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "probe.py").write_text("VALUE = 'main'\n", encoding="utf-8")
        (package / "startup.py").write_text("READY = True\n", encoding="utf-8")
        (self.root / "LAWS_OF_ALX.md").write_text("main laws\n", encoding="utf-8")
        (self.root / "IDENTITY_AND_MEMORY.md").write_text("main identity\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        self.main = git(self.root, "rev-parse", "main")
        # The launcher derives the checkout from its own location.
        (self.root / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/alx", self.root / "scripts/alx")

        # A failed Coding Agent job: a committed broken edit on its feature
        # branch, an uncommitted one beside it, and a rewritten law.
        git(self.root, "switch", "-q", "-c", "feat/broken")
        (package / "probe.py").write_text("VALUE = (\n", encoding="utf-8")
        git(self.root, "commit", "-qam", "broken")
        (package / "startup.py").write_text("def broken(:\n", encoding="utf-8")
        (self.root / "LAWS_OF_ALX.md").write_text("edited laws\n", encoding="utf-8")

    def runtime_code(self) -> Path:
        completed = subprocess.run(
            ["bash", "-c", 'source scripts/alx && runtime_code'],
            cwd=self.root, capture_output=True, text=True, check=True,
        )
        return Path(completed.stdout.strip())

    def importable(self, source: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c",
             "import alx.probe, alx.startup; print(alx.probe.VALUE)"],
            env={**os.environ, "PYTHONPATH": str(source)},
            capture_output=True, text=True,
        )

    def test_broken_feature_branch_source_cannot_stop_startup(self) -> None:
        self.assertNotEqual(self.importable(self.root / "src").returncode, 0)

        code = self.runtime_code()

        started = self.importable(code / "src")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(started.stdout.strip(), "main")
        self.assertEqual(code.name, self.main)
        self.assertEqual(
            (code / "LAWS_OF_ALX.md").read_text(encoding="utf-8"), "main laws\n"
        )
        self.assertTrue((code / "IDENTITY_AND_MEMORY.md").is_file())

    def test_the_checkout_is_left_exactly_as_the_job_left_it(self) -> None:
        status = git(self.root, "status", "--porcelain")
        self.runtime_code()
        self.assertEqual(git(self.root, "branch", "--show-current"), "feat/broken")
        self.assertEqual(git(self.root, "status", "--porcelain"), status)
        self.assertEqual(
            (self.root / "src/alx/startup.py").read_text(encoding="utf-8"),
            "def broken(:\n",
        )
        self.assertEqual(git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_snapshot_lives_in_git_metadata_and_follows_main(self) -> None:
        first = self.runtime_code()
        common = Path(git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual(first.parent, common / "alx-runtime")
        self.assertEqual(self.runtime_code(), first)

        # main moves on (a merge AL/X synchronised); the next start follows it
        # and the superseded snapshot does not linger.
        advanced = git(
            self.root, "commit-tree", f"{self.main}^{{tree}}", "-p", self.main,
            "-m", "merged",
        )
        git(self.root, "update-ref", "refs/heads/main", advanced)
        second = self.runtime_code()
        self.assertEqual(second.name, advanced)
        self.assertFalse(first.exists())


class RuntimeEntryNamesItsCheckout(unittest.TestCase):
    def test_the_checkout_is_required_and_passed_through(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            live_voice.main([])

        received: list[Path] = []

        async def run(repository_root: Path) -> None:
            received.append(repository_root)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(live_voice, "run", run):
            live_voice.main(["--checkout", directory])
            self.assertEqual(received, [Path(directory).resolve()])

    def test_laws_and_assets_come_from_the_code_that_is_running(self) -> None:
        self.assertEqual(live_voice.CODE_ROOT, Path(live_voice.__file__).resolve().parents[3])
        self.assertTrue((live_voice.CODE_ROOT / "LAWS_OF_ALX.md").is_file())

    def test_the_launcher_never_imports_the_working_files(self) -> None:
        launcher = (ROOT / "scripts/alx").read_text(encoding="utf-8")
        self.assertNotIn("PYTHONPATH=src", launcher)
        self.assertIn('PYTHONPATH="$code/src"', launcher)


if __name__ == "__main__":
    unittest.main()
