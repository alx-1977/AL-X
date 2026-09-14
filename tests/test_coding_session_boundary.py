"""The coding-session boundary is provider-neutral below the contract.

The execution half was written against one CLI, and most of it turned out to
be about running *a subscription coding CLI under containment* rather than
about that CLI. `SubscriptionCodingSession` is that part; `GrokCodingSession`
is its first adapter.

These tests exist because the extraction is only worth anything if two things
stay true: the shared half genuinely cannot know which CLI it is running, and
an adapter genuinely cannot reach around it to launch a process its own way.
The second is the Law 0 property — one execution route, not one per provider —
and `tests/test_sandbox_capability.py` enforces it structurally by naming the
base as the single coding launch site.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers import coding_session, coding_subscription_session  # noqa: E402
from alx.providers.coding_session import GrokCodingSession  # noqa: E402
from alx.providers.coding_subscription_session import (  # noqa: E402
    METERED_ENVIRONMENT_KEYS,
    SubscriptionCodingSession,
    classify_failure,
)


class TheSharedHalfKnowsNoProvider(unittest.TestCase):
    """If the base names a CLI, the extraction did not happen."""

    def test_the_base_names_no_provider_vocabulary(self) -> None:
        """The METERED_ENVIRONMENT_KEYS block is the one deliberate exception.

        It names every provider's credential on purpose: stripping all of them
        regardless of which CLI is running *is* the neutral rule, so it is
        excluded from this scan rather than weakening it.
        """
        import re

        source = inspect.getsource(coding_subscription_session)
        body = re.sub(
            r"METERED_ENVIRONMENT_KEYS = frozenset\(\{.*?\}\)",
            "", source, flags=re.S,
        )
        for vocabulary in ("grok", "--sandbox", "claude", "qwen", "xai"):
            self.assertNotIn(
                vocabulary.lower(),
                body.lower(),
                f"the shared base must not know about {vocabulary}",
            )

    def test_every_provider_decision_arrives_through_a_named_hook(self) -> None:
        for hook in (
            "command", "provider_environment", "prepare_home",
            "read_result", "default_auth_home",
        ):
            self.assertTrue(
                hasattr(SubscriptionCodingSession, hook), hook
            )
            with self.assertRaises(NotImplementedError, msg=hook):
                getattr(SubscriptionCodingSession, hook)(
                    SubscriptionCodingSession.__new__(SubscriptionCodingSession),
                    *([Path("/tmp"), Path("/tmp")] if hook == "command" else
                      [Path("/tmp"), Path("/tmp"), None] if hook == "prepare_home" else
                      [Path("/tmp")] if hook == "provider_environment" else
                      [""] if hook == "read_result" else []),
                )


class AnAdapterCannotLaunchItsOwnProcess(unittest.TestCase):
    """One execution route, whichever CLI is behind it."""

    def test_the_adapter_does_not_start_a_process(self) -> None:
        source = inspect.getsource(coding_session)
        for launcher in ("subprocess.run", "subprocess.Popen", "os.exec"):
            self.assertNotIn(launcher, source)

    def test_the_adapter_does_not_reimplement_run_session(self) -> None:
        self.assertNotIn("run_session", GrokCodingSession.__dict__)
        self.assertIs(
            GrokCodingSession.run_session, SubscriptionCodingSession.run_session
        )

    def test_the_adapter_does_not_reimplement_the_environment_allowlist(self) -> None:
        self.assertNotIn("child_environment", GrokCodingSession.__dict__)


class ContainmentStaysWithTheAdapter(unittest.TestCase):
    """Containment is a per-CLI decision and may not hide behind the base.

    Each CLI contains its sessions its own way, and a base that pretended
    otherwise would put a containment decision behind an abstraction. The base
    calls `prepare_home` before the process starts and an adapter that cannot
    install its containment must raise rather than continue.
    """

    def test_the_grok_adapter_installs_its_own_sandbox_profile(self) -> None:
        self.assertIn("prepare_home", GrokCodingSession.__dict__)
        source = inspect.getsource(GrokCodingSession.prepare_home)
        self.assertIn("write_profile", source)

    def test_the_base_installs_no_containment_of_its_own(self) -> None:
        source = inspect.getsource(coding_subscription_session)
        self.assertNotIn("write_profile", source)

    def test_containment_is_installed_before_the_process_starts(self) -> None:
        source = inspect.getsource(SubscriptionCodingSession.run_session)
        self.assertLess(
            source.index("prepare_home"), source.index("self._runner("),
            "containment must be installed before the CLI is launched",
        )


class SharedFailureClassification(unittest.TestCase):
    """Subscription-CLI facts, not Grok facts."""

    def test_containment_refusing_to_start_is_checked_first(self) -> None:
        # A message carrying both markers must classify as the containment
        # failure: running without protections is the worse outcome.
        self.assertEqual(
            classify_failure("sandbox profile invalid; usage limit reached", ""),
            ("sandbox_unusable", "sandbox_not_applied"),
        )

    def test_each_family_keeps_its_declared_code(self) -> None:
        for text, expected in (
            ("sandbox could not be applied", ("sandbox_unusable", "sandbox_not_applied")),
            ("usage limit reached", ("session_failed", "subscription_usage_exhausted")),
            ("not logged in", ("session_failed", "subscription_unauthenticated")),
            ("something else", ("session_failed", "cli_failed")),
        ):
            self.assertEqual(classify_failure(text, ""), expected)

    def test_the_adapter_delegates_rather_than_reclassifying(self) -> None:
        self.assertEqual(
            GrokCodingSession._failure("usage limit", ""),
            classify_failure("usage limit", ""),
        )


class MeteredCredentialsNeverReachASession(unittest.TestCase):
    """Every provider's key is stripped, whichever CLI is running."""

    def test_no_metered_key_survives_the_allowlist(self) -> None:
        session = GrokCodingSession(
            "grok-4.6", 60,
            environment={key: "secret" for key in METERED_ENVIRONMENT_KEYS}
            | {"PATH": "/bin", "HOME": "/h"},
        )
        environment = session.child_environment(Path("/tmp/home"))
        for key in METERED_ENVIRONMENT_KEYS:
            self.assertNotIn(key, environment, key)
        self.assertEqual(environment["PATH"], "/bin")

    def test_another_provider_key_is_stripped_too(self) -> None:
        """Not only the running CLI's own: no metered key reaches any session."""
        self.assertIn("ANTHROPIC_API_KEY", METERED_ENVIRONMENT_KEYS)
        self.assertIn("OPENAI_API_KEY", METERED_ENVIRONMENT_KEYS)
        self.assertIn("XAI_API_KEY", METERED_ENVIRONMENT_KEYS)


if __name__ == "__main__":
    unittest.main()
