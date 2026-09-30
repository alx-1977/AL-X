"""The one coding subprocess path observes activity, then checkpoints on bounds."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import CodingError
from alx.providers.coding_process import CodingCancellation


class CodingWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_child(self, script: str, *, stall: float, ceiling: float,
                  blocked: tuple[str, ...] = ()):
        return CodingCancellation().run(
            subprocess.run, [sys.executable, "-c", script, str(self.root)],
            capture_output=True, text=True, check=False,
            inactivity_timeout=stall, activity_root=self.root, timeout=ceiling,
            activity_blocked_paths=blocked,
        )

    def test_output_activity_keeps_session_alive_beyond_old_wall_clock_equivalent(self) -> None:
        started = time.monotonic()
        result = self.run_child(
            "import time; [(print('alive', flush=True), time.sleep(.09)) for _ in range(9)]",
            stall=.3, ceiling=2,
        )
        self.assertEqual(result.returncode, 0)
        self.assertGreater(time.monotonic() - started, .6)
        self.assertEqual(result.stdout.count("alive"), 9)

    def test_checkout_changes_reset_inactivity_timer_without_cli_output(self) -> None:
        result = self.run_child(
            "import pathlib, sys, time; p=pathlib.Path(sys.argv[1])/'work.txt'; "
            "[(p.write_text(str(i)), time.sleep(.09)) for i in range(9)]",
            stall=.3, ceiling=2,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.root / "work.txt").read_text(), "8")

    def test_inactivity_interrupts_child(self) -> None:
        started = time.monotonic()
        with self.assertRaises(CodingError) as raised:
            self.run_child("import time; time.sleep(5)", stall=.3, ceiling=2)
        self.assertEqual(raised.exception.code, "session_interrupted")
        self.assertEqual(raised.exception.details["reason_code"], "session_stalled")
        self.assertLess(time.monotonic() - started, 2)

    def test_runtime_log_churn_is_not_coding_activity(self) -> None:
        runtime = self.root / ".alx"
        runtime.mkdir()
        with self.assertRaises(CodingError) as raised:
            self.run_child(
                "import pathlib, sys, time; p=pathlib.Path(sys.argv[1])/'.alx'/'log'; "
                "[(p.write_text(str(i)), time.sleep(.08)) for i in range(20)]",
                stall=.3, ceiling=2,
            )
        self.assertEqual(raised.exception.details["reason_code"], "session_stalled")

    def test_blocked_path_churn_is_not_observed_as_activity(self) -> None:
        with self.assertRaises(CodingError) as raised:
            self.run_child(
                "import pathlib, sys, time; p=pathlib.Path(sys.argv[1])/'blocked.txt'; "
                "[(p.write_text(str(i)), time.sleep(.08)) for i in range(20)]",
                stall=.3, ceiling=2, blocked=("blocked.txt",),
            )
        self.assertEqual(raised.exception.details["reason_code"], "session_stalled")

    def test_emergency_ceiling_interrupts_even_active_child(self) -> None:
        with self.assertRaises(CodingError) as raised:
            self.run_child(
                "import time; [(print('alive', flush=True), time.sleep(.09)) for _ in range(30)]",
                stall=.3, ceiling=.8,
            )
        self.assertEqual(raised.exception.code, "session_interrupted")
        self.assertEqual(raised.exception.details["reason_code"], "session_emergency_ceiling")


if __name__ == "__main__":
    unittest.main()
