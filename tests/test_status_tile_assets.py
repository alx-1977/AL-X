"""The AL/X status tile is one compact card served beside the voice page.

One reusable card renders every state from data. These tests pin what it may
be: its files are served from the one explicit asset table, the three states
Friedl approved (green, yellow, red) exist as fixtures, device types are small
chips rather than blocks, the card offers no navigation or drill-down
control, and every state goes through the same renderer.
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

    def test_the_three_approved_states_exist(self) -> None:
        healthy, warning, fault = _fixture("healthy"), _fixture("warning"), _fixture("fault")
        self.assertIn("tone: 'ok'", healthy)
        self.assertIn("'All systems normal'", healthy)
        self.assertIn("tone: 'warn'", warning)
        self.assertIn("'not monitoring yet', idle: true", warning)
        self.assertIn("tone: 'bad'", fault)
        self.assertIn("'27/30'", fault)

    def test_device_types_are_chips_not_blocks(self) -> None:
        renderer = _read("tile.js")
        self.assertIn("data.chips.map(buildChip)", renderer)
        self.assertNotIn("facts", renderer)
        self.assertNotIn("visual", renderer)
        # Four device chips fit the same card that two do.
        self.assertEqual(_fixture("fault").count("icon:"), 3)
        self.assertIn("width: 25em;", _read("tile.css"))

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
        for word in ("healthy", "warning", "fault", "BHL", "normal", "offline"):
            self.assertNotIn(word, renderer.split("const svg")[1])
        # The page has one entry into it, and no markup of its own per state.
        self.assertEqual(fixtures.count("surfaceTile("), 1)
        self.assertNotIn("alx-tile", fixtures)
        self.assertEqual(renderer.count("export function"), 1)


if __name__ == "__main__":
    unittest.main()
