"""Datasheets fetched by part number through Nexar and DigiKey (2026-11-13)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.datasheet_fetch import DatasheetError, fetch_datasheet  # noqa: E402

PDF = b"%PDF-1.7 a datasheet"


class SimpleError(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class Stand:
    """The internet, as far as these tests need it."""

    def __init__(self, nexar_answers, digikey=None, files=None):
        self.nexar_answers, self.digikey, self.files = list(nexar_answers), digikey, files or {}
        self.fetched = []

    def post(self, url, data=None, json=None, headers=None):
        if "token" in url:
            return SimpleNamespace(json=lambda: {"access_token": "t"})
        answer = self.nexar_answers.pop(0)
        return SimpleNamespace(json=lambda: answer)

    def get(self, url, headers=None):
        # Only the DigiKey API is read through the client.
        assert "productdetails" in url, url
        return SimpleNamespace(status_code=200, json=lambda: {"Product": {"DatasheetUrl": self.digikey}})

    def download(self, url):
        """The guarded web path: a document or a refusal."""
        self.fetched.append(url)
        if url not in self.files:
            raise SimpleError("status 403")
        return self.files[url]


def part(mpn, url):
    return {"data": {"supSearchMpn": {"results": [{"part": {
        "mpn": mpn, "manufacturer": {"name": "Wurth Elektronik"}, "bestDatasheet": {"url": url},
        "documentCollections": []}}]}}}


class FetchDatasheetTests(unittest.TestCase):
    def test_the_second_nexar_key_is_tried_and_octopart_copy_kept(self) -> None:
        stand = Stand([{"errors": [{"message": "You have exceeded your part limit of 0."}]},
                       part("691325110002", "https://datasheet.octopart.com/691325110002.pdf")],
                      files={"https://datasheet.octopart.com/691325110002.pdf": PDF})
        with tempfile.TemporaryDirectory() as folder:
            result = fetch_datasheet("691325110002", "Würth", Path(folder), client=stand,
                                     nexar=[("main", "s"), ("eval", "s")], digikey=None,
                                     download=stand.download)
            self.assertEqual(result["source"], "nexar")
            self.assertEqual(Path(result["path"]).read_bytes(), PDF)
            self.assertIn("part limit of 0", result["tried"][0]["result"])
            # Saved once, then found again without asking anyone.
            again = fetch_datasheet("691325110002", "", Path(folder), client=Stand([]), nexar=[], digikey=None,
                                    download=stand.download)
            self.assertEqual(again["source"], "already saved")

    def test_a_page_that_is_not_a_pdf_is_refused_and_digikey_is_next(self) -> None:
        stand = Stand([part("ABC", "https://example.com/abc.html")], digikey="//cdn.example.com/abc.pdf",
                      files={"https://example.com/abc.html": b"<html>", "https://cdn.example.com/abc.pdf": PDF})
        with tempfile.TemporaryDirectory() as folder:
            result = fetch_datasheet("ABC", "", Path(folder), client=stand, nexar=[("k", "s")], digikey=("id", "s"),
                                     download=stand.download)
            self.assertEqual((result["source"], result["url"]), ("digikey", "https://cdn.example.com/abc.pdf"))
            self.assertEqual(result["tried"][0]["result"], "not a PDF")

    def test_nothing_found_says_what_was_tried(self) -> None:
        stand = Stand([part("XYZ", "https://www.we-online.com/xyz.pdf")], digikey="https://www.we-online.com/xyz.pdf")
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(DatasheetError) as caught:
            fetch_datasheet("XYZ", "", Path(folder), client=stand, nexar=[("k", "s")], digikey=("id", "s"),
                                     download=stand.download)
        self.assertEqual(caught.exception.code, "not_found")
        self.assertIn("status 403", caught.exception.detail)

    def test_another_part_with_a_similar_number_is_not_taken(self) -> None:
        stand = Stand([part("691325110002B", "https://datasheet.octopart.com/other.pdf")],
                      files={"https://datasheet.octopart.com/other.pdf": PDF})
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(DatasheetError):
            fetch_datasheet("691325110002", "", Path(folder), client=stand, nexar=[("k", "s")], digikey=None, download=stand.download)


if __name__ == "__main__":
    unittest.main()
