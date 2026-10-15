"""The voice page reconnects on its own (Friedl, 2026-10-15).

"Voice transport closed" at 08:00:01 left AL/X disconnected until the button
was clicked again: the browser had dropped the link, and nothing reopened it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src/alx/interfaces/assets/app.js"


class VoiceReconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = APP.read_text(encoding="utf-8")

    def handler(self, name: str) -> str:
        found = re.search(rf"socket\.{name} = \(\) => \{{(.*?)\n  \}};", self.text, re.S)
        self.assertIsNotNone(found, name)
        return found.group(1)

    def test_a_dropped_link_is_reopened_while_it_should_be_kept(self) -> None:
        closed = self.handler("onclose")
        self.assertIn("if (keepConnected)", closed)
        self.assertIn("scheduleReconnect()", closed)

    def test_only_a_link_that_held_resets_the_delays(self) -> None:
        self.assertNotIn("reconnectAttempt", self.handler("onopen"))
        self.assertIn("Date.now() - socketOpenedAt >= RECONNECT_HELD_MS) reconnectAttempt = 0;",
                      self.handler("onclose"))

    def test_a_microphone_that_needs_a_click_stops_the_retries(self) -> None:
        failure = self.text[self.text.index("Microphone access failed"):]
        self.assertLess(failure.index("keepConnected = false;"), failure.index("return;"))

    def test_the_delays_are_bounded(self) -> None:
        self.assertIn("const RECONNECT_DELAYS_MS = [2000, 5000, 10000, 30000];", self.text)


if __name__ == "__main__":
    unittest.main()
