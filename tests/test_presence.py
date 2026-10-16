"""Whether Friedl is here or away, as he tells AL/X (2026-10-15).

AL/X runs on another machine, so his keyboard and screen say nothing about
him; he tells her when he steps out or is back, she records it, and a quiet
icon on his screen shows it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.continuity.store import SQLiteContinuityStore  # noqa: E402
from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.tools import SET_PRESENCE  # noqa: E402
from alx.tools.continuity import DEFINITIONS, build_continuity_executors  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "src/alx/interfaces/assets"


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 15, hour, minute, tzinfo=UTC)


class PresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "continuity.sqlite3"
        self.store = SQLiteContinuityStore(self.path)
        self.now = [at(9)]
        self.set = build_continuity_executors(
            self.store, 30, lambda: "call-1", clock=lambda: self.now[0]
        )[SET_PRESENCE]

    def test_he_is_here_until_he_says_otherwise(self) -> None:
        self.assertEqual(self.store.presence(), ("here", None))

    def test_she_records_him_away_and_back(self) -> None:
        result = self.set({"state": "away"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values, {"state": "away", "since": at(9).isoformat()})
        self.now[0] = at(11)
        self.set({"state": "here"})
        self.assertEqual(SQLiteContinuityStore(self.path).presence(), ("here", at(11)))

    def test_saying_it_twice_keeps_the_first_time(self) -> None:
        self.set({"state": "away"})
        self.now[0] = at(10)
        self.assertEqual(self.set({"state": "away"}).values["since"], at(9).isoformat())

    def test_only_here_or_away(self) -> None:
        for value in ("Away", "busy", "", None):
            with self.subTest(value=value):
                result = self.set({"state": value})
                self.assertEqual(result.failure["code"], "arguments_unusable")

    def test_she_is_told_what_it_is_for(self) -> None:
        self.assertIn(SET_PRESENCE, [definition.capability_id for definition in DEFINITIONS])
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("runtime.presence says whether Friedl is here or away", text)
        # 2026-10-16: "Good night" did not set him away, because she had been
        # told to record it when he announced it. It is her judgement, and
        # nothing lists what counts.
        self.assertIn("your judgement from anything he says or does", text)
        self.assertNotIn("tells you he is stepping out", text)


class PresenceIconTests(unittest.TestCase):
    def get(self, presence):
        server = LiveVoiceServer(Mock(), "127.0.0.1", 8765, 16000, ASSETS, presence=presence)
        return server._serve_asset(Mock(), Mock(path="/presence.json"))

    def test_the_page_gets_here_or_away(self) -> None:
        response = self.get(lambda: ("away", at(9)))
        self.assertEqual(json.loads(response.body),
                         {"presence": {"state": "away", "since": at(9).isoformat()}})
        self.assertEqual(json.loads(self.get(None).body), {"presence": None})

    def test_a_failing_source_is_an_error(self) -> None:
        def broken():
            raise RuntimeError("store")
        self.assertEqual(self.get(broken).status_code, 503)

    def test_the_icon_sits_in_the_menu_bar_like_the_bhl_item(self) -> None:
        script = (ASSETS / "presence.js").read_text()
        self.assertIn('document.getElementById("menubar")', script)
        # The light first, then the figure, and no words.
        self.assertIn('light.className = "rt-light";', script)
        self.assertIn("root.append(light, glyph());", script)
        self.assertNotIn("textContent", script)  # only the hover tip says "Away since …"
        page = (ASSETS / "index.html").read_text()
        self.assertIn('id="menubar"', page)
        self.assertIn('src="/menubar.js"', page)

    def test_the_bar_text_follows_what_is_behind_it(self) -> None:
        script = (ASSETS / "menubar.js").read_text()
        self.assertIn('item.dataset.ink = "dark"', script)
        self.assertIn('item.dataset.ink = "light"', script)

    def test_the_icon_is_on_the_page(self) -> None:
        page = (ASSETS / "index.html").read_text()
        self.assertIn('id="presence"', page)
        self.assertIn('src="/presence.js"', page)


if __name__ == "__main__":
    unittest.main()
