"""What Friedl can see while AL/X works (2026-11-06).

A Claude limit wait of 54 minutes on 2026-11-05 showed nothing on screen, and
111 plan replies never had a delivery receipt with nothing logged about where
they stopped. Both are now visible: the pause is announced with its reset
time, and every hand-off of a background reply to the screen is logged.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.subscription_retry import (  # noqa: E402
    LONG_PAUSE_SECONDS, SubscriptionRetryPolicy,
)

ROOT = Path(__file__).resolve().parents[1]


class LimitPauseTests(unittest.TestCase):
    def test_a_stated_reset_is_announced_before_the_wait(self) -> None:
        waited, announced = [], []
        policy = SubscriptionRetryPolicy(
            wait=waited.append, now=lambda: datetime(2026, 11, 5, 12, 26))
        policy.pause("You've hit your limit · resets 1:20pm", 0, announced.append)
        self.assertEqual(len(announced), 1)
        self.assertAlmostEqual(announced[0], 54 * 60, delta=1)
        self.assertGreater(sum(waited), LONG_PAUSE_SECONDS)

    def test_a_short_backoff_is_not_announced(self) -> None:
        announced = []
        SubscriptionRetryPolicy(wait=lambda _s: None).pause("timeout", 0, announced.append)
        self.assertEqual(announced, [])

    def test_a_failing_announcement_never_stops_the_wait(self) -> None:
        waited = []

        def broken(_delay):
            raise RuntimeError("console gone")
        SubscriptionRetryPolicy(wait=waited.append, now=lambda: datetime(2026, 11, 5, 12, 26)
                                ).pause("resets 1:20pm", 0, broken)
        self.assertTrue(waited)

    def test_the_console_shows_the_pause_as_a_system_line(self) -> None:
        app = (ROOT / "src/alx/interfaces/assets/app.js").read_text(encoding="utf-8")
        self.assertIn('message.code === "reasoning.paused"', app)
        self.assertIn("Claude limit reached · paused until", app)


class DeliveryVisibilityTests(unittest.TestCase):
    def test_each_hand_off_and_text_send_is_logged(self) -> None:
        server = (ROOT / "src/alx/interfaces/server.py").read_text(encoding="utf-8")
        voice = (ROOT / "src/alx/interfaces/live_voice.py").read_text(encoding="utf-8")
        runner = (ROOT / "src/alx/bootstrap/autonomous.py").read_text(encoding="utf-8")
        self.assertIn('"Background reply %s for the screen', server)
        self.assertIn('"Reply text sent to the screen: turn=%s"', voice)
        self.assertIn('"Reply text not sent to the screen: connection closed', voice)
        # Queued is what is known; "delivered" claimed more than that.
        self.assertIn('"Background response queued for screen"', runner)
        self.assertNotIn('"Background response delivered"', runner)


if __name__ == "__main__":
    unittest.main()
