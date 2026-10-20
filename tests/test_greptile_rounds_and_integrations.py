"""Greptile's round on a later page of check runs, and her integrations facts.

Both from Greptile's review of PR #139: a round past the first hundred check
runs read as no round at all, and the integrations listed services a runtime
configured otherwise does not use.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.live_voice import _integrations  # noqa: E402
from alx.contracts.review_provider import ReviewProvider, profile_for  # noqa: E402
from alx.providers import github_review  # noqa: E402
from alx.providers.github_review import GitHubReviewProvider, _Unavailable  # noqa: E402
from tests.review_transcript import HEAD, status  # noqa: E402


class _Response:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class CheckRunPagingTests(unittest.TestCase):
    def provider(self, pages):
        def request(method, url, **keywords):
            page = int(parse_qs(urlparse(url).query)["page"][0])
            runs = pages[page - 1] if page <= len(pages) else []
            return _Response({"total_count": 0, "check_runs": runs})

        original = github_review.httpx.request
        github_review.httpx.request = request
        self.addCleanup(setattr, github_review.httpx, "request", original)
        return GitHubReviewProvider(
            "owner/repo", "token", profile_for(ReviewProvider.GREPTILE)
        )

    @staticmethod
    def other(index: int) -> dict:
        return {"name": f"ci-{index}", "app": {"slug": "github-actions"},
                "status": "completed", "conclusion": "success"}

    def test_a_round_on_the_second_page_is_found(self) -> None:
        first = [self.other(n) for n in range(100)]
        provider = self.provider([first, [status("success")]])
        self.assertEqual(provider._round(HEAD).state, "success")

    def test_a_full_page_at_the_cap_is_unreadable_not_absent(self) -> None:
        full = [self.other(n) for n in range(100)]
        provider = self.provider([full] * github_review.MAX_PAGES)
        with self.assertRaises(_Unavailable):
            provider._round(HEAD)


def _settings(reasoning="claude_subscription", tts="elevenlabs", stt="cartesia"):
    def model(provider, name):
        return SimpleNamespace(provider=provider, model=name)

    return SimpleNamespace(
        reasoning=model(reasoning, "the-model"),
        text_to_speech=model(tts, "voice-model"),
        speech_to_text=model(stt, "hearing-model"),
    )


class IntegrationsTests(unittest.TestCase):
    def names(self, settings, research=None, others=()):
        return [item["name"] for item in _integrations(settings, research, others)]

    def test_the_configured_providers_are_the_ones_listed(self) -> None:
        self.assertEqual(self.names(_settings()), ["Claude", "ElevenLabs", "Cartesia"])
        self.assertEqual(
            self.names(_settings(reasoning="openai", tts="none")),
            ["OpenAI", "Cartesia"],
        )

    def test_research_on_the_same_service_joins_its_entry(self) -> None:
        tier = SimpleNamespace(provider="claude_subscription", model="claude-haiku-5-5")
        research = SimpleNamespace(enabled_tiers=frozenset({"survey"}), survey=tier)
        listed = _integrations(_settings(), research, ())
        self.assertEqual(listed[0]["name"], "Claude")
        self.assertIn("src/alx/providers/claude_usage.py", listed[0]["code"])
        self.assertIn("survey research (claude-haiku-5-5)", listed[0]["for"])

    def test_only_composed_services_are_listed(self) -> None:
        others = (("Xero", "accounts", "src/x.py", False),
                  ("GitHub", "repository", "src/g.py", True))
        self.assertEqual(self.names(_settings(), None, others)[-1], "GitHub")
        self.assertNotIn("Xero", self.names(_settings(), None, others))


if __name__ == "__main__":
    unittest.main()
