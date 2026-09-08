"""D-027 confinement, proved by attacking it rather than by reading the profile.

Every test here asserts a *blocked* outcome against the real macOS sandbox. A
profile that merely contains the right rules proves nothing; these run code
that genuinely tries to escape and assert that it could not.

The tests skip on platforms without a supported confinement mechanism, and skip
explicitly rather than silently, so a green suite on a machine that cannot
confine anything is never mistaken for evidence that confinement works.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.sandbox import SandboxRequest  # noqa: E402
from alx.providers.sandbox_macos import SeatbeltSandboxRunner  # noqa: E402
from alx.providers.sandbox_workspace import SandboxWorkspace  # noqa: E402


def _runner(root: Path) -> SeatbeltSandboxRunner:
    workspace = SandboxWorkspace(root)
    return SeatbeltSandboxRunner(
        workspace,
        denied_read_paths=(REPOSITORY_ROOT, Path.home() / ".ssh"),
    )


class SandboxConfinementTest(unittest.TestCase):
    """Adversarial probes against the real kernel sandbox."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = SandboxWorkspace(self.root)
        self.runner = _runner(self.root)
        if not self.runner.available():
            self.skipTest("no supported confinement mechanism on this platform")
        self.addCleanup(self.directory.cleanup)
        self.counter = 0

    def _run(self, source: str, wall_seconds: int = 30):
        self.counter += 1
        run_id = f"run-{self.counter}"
        paths = self.workspace.prepare("exp-a", "ses-a", run_id)
        request = SandboxRequest("exp-a", "ses-a", run_id, source, wall_seconds=wall_seconds)
        return self.runner.run(request, paths)

    def test_outbound_network_is_refused(self) -> None:
        outcome = self._run(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
            "    print('LEAK')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)
        self.assertIn("blocked", outcome.stdout)

    def test_dns_resolution_is_refused(self) -> None:
        outcome = self._run(
            "import socket\n"
            "try:\n"
            "    socket.gethostbyname('example.com')\n"
            "    print('LEAK')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)

    def test_repository_source_cannot_be_read(self) -> None:
        outcome = self._run(
            "try:\n"
            f"    open({str(REPOSITORY_ROOT / 'LAWS_OF_ALX.md')!r}).read()\n"
            "    print('LEAK')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)
        self.assertIn("blocked", outcome.stdout)

    def test_environment_file_cannot_be_read(self) -> None:
        outcome = self._run(
            "try:\n"
            f"    open({str(REPOSITORY_ROOT / '.env')!r}).read()\n"
            "    print('LEAK')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)

    def test_private_keys_cannot_be_read(self) -> None:
        outcome = self._run(
            "import glob\n"
            f"found = glob.glob({str(Path.home() / '.ssh' / '*')!r})\n"
            "print('FOUND' if found else 'blocked')\n"
        )
        self.assertNotIn("FOUND", outcome.stdout)

    def test_repository_cannot_be_written(self) -> None:
        outcome = self._run(
            "try:\n"
            f"    open({str(REPOSITORY_ROOT / 'LAWS_OF_ALX.md')!r}, 'a').write('x')\n"
            "    print('LEAK')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)
        self.assertEqual(
            (REPOSITORY_ROOT / "LAWS_OF_ALX.md").read_text().count("Law 0"), 2
        )

    def test_writes_outside_the_workspace_are_refused(self) -> None:
        target = Path(tempfile.gettempdir()) / "alx-sandbox-escape-probe"
        if target.exists():
            target.unlink()
        outcome = self._run(
            "try:\n"
            f"    open({str(target)!r}, 'w').write('escaped')\n"
            "    print('LEAK')\n"
            "except Exception as error:\n"
            "    print('blocked', type(error).__name__)\n"
        )
        self.assertNotIn("LEAK", outcome.stdout)
        self.assertFalse(target.exists())

    def test_a_child_process_inherits_the_confinement(self) -> None:
        """The escape most likely to be forgotten: spawn a helper and retry."""
        outcome = self._run(
            "import subprocess, sys\n"
            "result = subprocess.run(\n"
            "    [sys.executable, '-c', "
            "'import socket; socket.create_connection((\"1.1.1.1\", 443), timeout=5); print(\"CHILD LEAK\")'],\n"
            "    capture_output=True)\n"
            "print('child rc', result.returncode)\n"
            "print(result.stdout.decode())\n"
        )
        self.assertNotIn("CHILD LEAK", outcome.stdout)

    def test_no_production_credential_is_inherited(self) -> None:
        """Asserted against the real .env names, not a sample."""
        names = [
            line.split("=", 1)[0].strip()
            for line in (REPOSITORY_ROOT / ".env").read_text().splitlines()
            if "=" in line and not line.strip().startswith("#")
        ]
        secret_names = [
            name for name in names
            if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        ]
        self.assertTrue(secret_names, "expected the .env to contain credentials")
        outcome = self._run("import os\nprint(sorted(os.environ))\n")
        for name in secret_names:
            self.assertNotIn(name, outcome.stdout)

    def test_the_workspace_itself_remains_writable(self) -> None:
        outcome = self._run(
            "open('artifact.txt', 'w').write('evidence')\nprint('wrote')\n"
        )
        self.assertIn("wrote", outcome.stdout)
        self.assertEqual(outcome.exit_status, 0)
        self.assertEqual(
            [item.name for item in outcome.artifacts], ["artifact.txt"]
        )

    def test_the_probe_reaches_the_network_when_the_profile_permits_it(self) -> None:
        """The mutation these tests exist to catch.

        Without this, every blocked result above could be a probe that never
        worked rather than a profile that stopped it. A profile that grants
        network access must let the same probe through.

        The mutation grants access rather than deleting the denial: under
        `(deny default)` removing `(deny network*)` changes nothing, because
        the default already refuses. Asserting that deletion blocks traffic
        would have proved the default and said nothing about the rule.
        """
        weakened = _runner(self.root)
        original = weakened.profile

        def permissive(state: Path) -> str:
            return original(state).replace("(deny network*)", "(allow network*)")

        weakened.profile = permissive  # type: ignore[method-assign]
        paths = self.workspace.prepare("exp-a", "ses-weak", "run-weak")
        outcome = weakened.run(
            SandboxRequest(
                "exp-a",
                "ses-weak",
                "run-weak",
                "import socket\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
                "    print('REACHED')\n"
                "except Exception as error:\n"
                "    print('blocked', type(error).__name__)\n",
            ),
            paths,
        )
        self.assertIn("REACHED", outcome.stdout)


if __name__ == "__main__":
    unittest.main()
