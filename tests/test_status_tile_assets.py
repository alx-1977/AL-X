"""The AL/X status tile is a static UI shell served beside the voice page.

One reusable card renders every state from data. These tests pin what the
shell may be: its files are served from the one explicit asset table, the BHL
healthy and issue fixtures exist, PSUs are shown as in development rather
than monitored, the card offers no navigation or drill-down control, and both
states go through the same renderer rather than per-state markup.
"""

from pathlib import Path
from types import SimpleNamespace
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.interfaces.server import LiveVoiceServer


ASSETS = ROOT / "src/alx/interfaces/assets"

TILE_ROUTES = {
    "/tiles": "text/html",
    "/tile.css": "text/css",
    "/tile.js": "javascript",
    "/tile-fixtures.js": "javascript",
    "/tile-bhl-venue.jpg": "image/jpeg",
}


def _read(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _fixture(state: str) -> str:
    """The source text of one fixture object in tile-fixtures.js."""
    text = _read("tile-fixtures.js")
    match = re.search(rf"\n  {state}: \{{\n(.*?)\n  \}},", text, re.S)
    if match is None:
        raise AssertionError(f"fixture {state!r} not found")
    return match.group(1)


class StatusTileAssetTests(unittest.TestCase):
    def test_every_tile_file_is_served_from_the_asset_table(self) -> None:
        server = LiveVoiceServer(
            session=None,
            host="127.0.0.1",
            port=0,
            sample_rate_hz=16_000,
            asset_root=ASSETS,
        )
        for path, media in TILE_ROUTES.items():
            with self.subTest(path=path):
                response = server._serve_asset(None, SimpleNamespace(path=path))
                self.assertEqual(response.status_code, 200)
                self.assertIn(media, response.headers["Content-Type"])

    def test_healthy_and_issue_fixtures_exist(self) -> None:
        healthy = _fixture("healthy")
        issue = _fixture("issue")
        self.assertIn("'All systems normal'", healthy)
        self.assertIn("'25/25'", healthy)
        self.assertIn("'1 issue detected'", issue)
        self.assertIn("'Room reader R07 offline'", issue)
        self.assertIn("'24/25'", issue)
        self.assertIn("tone: 'attention'", issue)

    def test_psus_are_in_development_and_not_monitored(self) -> None:
        text = _read("tile-fixtures.js")
        self.assertIn(
            "{ icon: 'plug', label: 'PSUs', tone: 'disabled', note: 'In development' }",
            text,
        )
        for state in ("healthy", "issue"):
            with self.subTest(state=state):
                self.assertIn("PSUS", _fixture(state))
        # A disabled fact renders no value and no status dot.
        renderer = _read("tile.js")
        self.assertIn("const monitored = fact.tone !== 'disabled';", renderer)
        self.assertIn("if (monitored) item.append(el('span', 'alx-tile__dot'));", renderer)

    def test_tile_offers_no_navigation_or_drill_down_control(self) -> None:
        for name in ("tiles.html", "tile.js", "tile-fixtures.js"):
            text = _read(name).lower()
            with self.subTest(file=name):
                self.assertNotIn("<button", text)
                self.assertNotIn("'button'", text)
                self.assertNotIn("<a ", text)
                self.assertNotIn("'a'", text)
                self.assertNotIn("<input", text)
                self.assertNotIn("<form", text)

    def test_one_renderer_draws_both_states(self) -> None:
        renderer = _read("tile.js")
        fixtures = _read("tile-fixtures.js")
        # The renderer knows no state or domain; it only reads data.
        for word in ("healthy", "issue", "BHL", "normal", "R07"):
            self.assertNotIn(word, renderer.split("const svg")[1])
        # The page has one entry into it, and no markup of its own per state.
        self.assertEqual(fixtures.count("surfaceTile("), 1)
        self.assertNotIn("alx-tile", fixtures)
        self.assertEqual(renderer.count("export function"), 1)


if __name__ == "__main__":
    unittest.main()
