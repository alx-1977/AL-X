from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.elevenlabs_pronunciation import (  # noqa: E402
    ElevenLabsDictionaryManager,
    load_vocabulary,
)


ROOT = Path(__file__).resolve().parents[1]
VOCABULARY = ROOT / "config/pronunciation/alx-vocabulary.v1.json"
ACCEPTANCE = ROOT / "tests/fixtures/pronunciation_acceptance.json"
DEPLOYMENT = ROOT / "config/pronunciation/elevenlabs-deployment.v1.json"


class PronunciationVocabularyTests(unittest.TestCase):
    def test_canonical_vocabulary_is_versioned_unique_and_alias_only(self) -> None:
        vocabulary = load_vocabulary(VOCABULARY)
        self.assertEqual(vocabulary.schema_version, 1)
        self.assertEqual(vocabulary.vocabulary_version, "1.0.0")
        rules = [rule.as_elevenlabs_rule() for rule in vocabulary.rules]
        self.assertTrue(all(rule["type"] == "alias" for rule in rules))
        self.assertEqual(len(rules), len({rule["string_to_replace"] for rule in rules}))

    def test_acceptance_fixture_covers_required_categories(self) -> None:
        fixture = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
        categories = {case["category"] for case in fixture["cases"]}
        self.assertTrue(
            {"resistance", "voltage", "current", "power", "frequency", "capacitance", "temperature", "component", "currency", "names"}
            <= categories
        )

    def test_deployment_manifest_matches_canonical_vocabulary(self) -> None:
        vocabulary = load_vocabulary(VOCABULARY)
        deployment = json.loads(DEPLOYMENT.read_text(encoding="utf-8"))
        self.assertEqual(deployment["schema_version"], 1)
        self.assertEqual(deployment["provider"], "elevenlabs")
        self.assertEqual(
            deployment["vocabulary_version"], vocabulary.vocabulary_version
        )
        self.assertEqual(deployment["rules_count"], len(vocabulary.rules))
        self.assertTrue(deployment["dictionary_id"])
        self.assertTrue(deployment["version_id"])

    def test_remote_dictionary_is_created_from_local_rules_and_returns_locator(self) -> None:
        captured = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"id": "dictionary-id", "version_id": "version-id"})

        manager = ElevenLabsDictionaryManager(
            "secret",
            "https://speech.example",
            httpx.Client(transport=httpx.MockTransport(respond)),
        )
        locator = manager.deploy(load_vocabulary(VOCABULARY))
        sent = json.loads(captured["request"].content)
        self.assertEqual(captured["request"].url.path, "/v1/pronunciation-dictionaries/add-from-rules")
        self.assertEqual(sent["name"], "ALX Canonical Vocabulary")
        self.assertTrue(all(rule["type"] == "alias" for rule in sent["rules"]))
        self.assertEqual(locator.dictionary_id, "dictionary-id")
        self.assertEqual(locator.version_id, "version-id")

    def test_existing_dictionary_update_creates_a_new_version(self) -> None:
        captured = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"version_id": "new-version-id"})

        manager = ElevenLabsDictionaryManager(
            "secret",
            "https://speech.example",
            httpx.Client(transport=httpx.MockTransport(respond)),
        )
        locator = manager.deploy(load_vocabulary(VOCABULARY), "dictionary-id")
        self.assertEqual(
            captured["request"].url.path,
            "/v1/pronunciation-dictionaries/dictionary-id/set-rules",
        )
        self.assertEqual(locator.version_id, "new-version-id")


if __name__ == "__main__":
    unittest.main()
