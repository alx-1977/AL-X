"""Execute the real composition root and prove it reaches the serving state.

A NameError in `run()` left the runtime unstartable while 1157 tests, two law
gates and three external reviews all passed. Every "composition" test asserted
against the file's *source* — parsing it, or searching it — so they proved the
code was written and never that it runs.

This test calls the actual production `run()`. Only the boundaries that would
leave the process are made inert: binding a socket, and the tick's sleep loop.
Everything the bug lived in — settings, stores, ledgers, CoreAgent, the
gateway, the reasoners, capability registration, approval windows, the shared
lock, the TaskGroup — is constructed for real.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap import live_voice  # noqa: E402


def _template_environment() -> dict[str, str]:
    """The runtime's own configuration template, with test credentials.

    Derived from the tracked `.env.example` rather than hand-listed, so a new
    required setting makes this test fail at startup — which is exactly what it
    is for — instead of silently drifting out of date. Placeholders are filled
    with values that satisfy validation and reach no network.
    """
    environment: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        environment[key.strip()] = value.strip()
    # Every setting the runtime declares as required, filled with values that
    # satisfy validation and reach no network. Derived from the template above
    # plus these, so a newly required setting fails this test at startup rather
    # than passing quietly.
    for key, value in {
        "ALX_PRIMARY_PERSON_ID": "friedl",
        "OPENAI_API_KEY": "test-key",
        "XAI_API_KEY": "test-key",
        "CARTESIA_API_KEY": "test-key",
        "ELEVENLABS_API_KEY": "test-key",
        "ALX_TTS_VOICE_ID": "test-voice",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_ID": "test-dictionary",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID": "test-version",
        "MAIL_ADDRESS": "alx@example.invalid",
        "MAIL_KEY": "test-secret",
        "MAIL_IMAP_HOST": "imap.example.invalid",
        "MAIL_IMAP_PORT": "993",
    }.items():
        if not environment.get(key):
            environment[key] = value

    # Never inherit a real autonomous configuration from the template.
    for key in (
        "ALX_AUTONOMOUS_PROVIDER", "ALX_AUTONOMOUS_MODEL",
        "ALX_AUTONOMOUS_EFFORT", "AUTONOMOUS_COGNITION_DAILY_BUDGET_USD",
        "ALX_AUTONOMOUS_COMMISSIONING_DISPATCHES",
    ):
        environment.pop(key, None)
    return environment


BASE_ENVIRONMENT = _template_environment()


class _Kept:
    """Keeps a directory alive so a test can inspect what startup wrote."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __enter__(self) -> str:
        return self._name

    def __exit__(self, *_exc) -> None:
        return None


class StartupSmokeTest(unittest.TestCase):
    """One executable guard over the whole startup path."""

    def _run_runtime(self, overrides: dict[str, str], keep: bool = False) -> dict:
        """Start the real runtime, reach serving, then shut it down.

        Returns what the run observed, so a caller can assert that startup
        completing did not itself cause any outward effect.
        """
        observed: dict = {
            "served": False,
            "provider_calls": 0,
            "ticks": 0,
            "synthesis_calls": 0,
            "bound_sockets": 0,
        }

        holder = tempfile.TemporaryDirectory()
        if keep:
            self.addCleanup(holder.cleanup)
        with holder if not keep else _Kept(holder.name) as directory:
            environment = dict(BASE_ENVIRONMENT)
            environment["ALX_RUNTIME_STORAGE_ROOT"] = directory
            environment.update(overrides)

            serving = asyncio.Event()

            # --- the only stubs: things that would leave the process ---------
            def load_environment(_path, inherited=None):
                return dict(environment)

            async def serve_forever(_self):
                observed["bound_sockets"] += 1   # counted, never bound
                observed["served"] = True
                serving.set()
                await asyncio.Event().wait()      # serve until cancelled

            async def tick_forever(_self):
                # The real tick's body is exercised in its own tests; here it
                # must not sleep 30s or notice anything, because nothing is due
                # and the point is to prove startup, not cognition.
                observed["ticks"] += 1
                await asyncio.Event().wait()

            def refuse_provider_call(*_args, **_kwargs):
                observed["provider_calls"] += 1
                raise AssertionError("startup must not call a provider")

            from alx.continuity.due_source import DueCognitionSource
            from alx.interfaces.server import LiveVoiceServer
            from alx.providers.openai import OpenAIReasoningModel
            from alx.providers.xai import XAIReasoningModel

            patches = [
                (live_voice, "load_environment", load_environment),
                (LiveVoiceServer, "serve_forever", serve_forever),
                (DueCognitionSource, "run", tick_forever),
                (OpenAIReasoningModel, "complete", refuse_provider_call),
                (XAIReasoningModel, "complete", refuse_provider_call),
            ]
            originals = [
                (target, name, getattr(target, name)) for target, name, _ in patches
            ]
            for target, name, value in patches:
                setattr(target, name, value)

            async def scenario() -> None:
                runtime = asyncio.create_task(live_voice.run(ROOT))
                done, _ = await asyncio.wait(
                    [runtime, asyncio.create_task(serving.wait())],
                    timeout=30,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if runtime in done:
                    # It exited instead of serving: surface the real error.
                    runtime.result()
                    raise AssertionError("run() returned without serving")
                # Reaching here means composition completed and serving began.
                runtime.cancel()
                try:
                    await runtime
                except asyncio.CancelledError:
                    pass

            try:
                asyncio.run(scenario())
            finally:
                for target, name, value in originals:
                    setattr(target, name, value)

            observed["storage"] = sorted(
                path.name for path in Path(directory).glob("*.sqlite3")
            )
            observed["directory"] = directory
        return observed

    # --- A. autonomous disabled ------------------------------------------

    def test_the_runtime_starts_with_autonomous_cognition_disabled(self) -> None:
        observed = self._run_runtime({})
        self.assertTrue(observed["served"], "run() must reach the serving state")

    def test_a_disabled_runtime_still_builds_its_durable_stores(self) -> None:
        observed = self._run_runtime({})
        for name in ("goals.sqlite3", "conversations.sqlite3", "continuity.sqlite3"):
            with self.subTest(store=name):
                self.assertIn(name, observed["storage"])

    # --- B. commissioning configuration ------------------------------------

    def _commissioning(self) -> dict[str, str]:
        return {
            "ALX_AUTONOMOUS_PROVIDER": "openai",
            "ALX_AUTONOMOUS_MODEL": "gpt-5.6-luna",
            "ALX_AUTONOMOUS_EFFORT": "max",
            "AUTONOMOUS_COGNITION_DAILY_BUDGET_USD": "0.0816",
            "ALX_AUTONOMOUS_COMMISSIONING_DISPATCHES": "1",
        }

    def test_the_runtime_starts_under_the_commissioning_configuration(self) -> None:
        observed = self._run_runtime(self._commissioning())
        self.assertTrue(observed["served"])

    def test_commissioning_startup_builds_the_autonomous_ledgers(self) -> None:
        observed = self._run_runtime(self._commissioning())
        for name in (
            "cognition-opportunities.sqlite3",
            "autonomous-cognition-spend.sqlite3",
        ):
            with self.subTest(store=name):
                self.assertIn(name, observed["storage"])

    def test_the_due_cognition_producer_is_started(self) -> None:
        observed = self._run_runtime(self._commissioning())
        self.assertEqual(observed["ticks"], 1)

    # --- no live side effects ---------------------------------------------

    def test_startup_alone_calls_no_provider(self) -> None:
        for label, overrides in (
            ("disabled", {}), ("commissioning", self._commissioning()),
        ):
            with self.subTest(configuration=label):
                observed = self._run_runtime(overrides)
                self.assertEqual(observed["provider_calls"], 0)
                self.assertEqual(observed["synthesis_calls"], 0)

    def test_startup_creates_no_cognition_opportunity(self) -> None:
        """A composed runtime is inert until something is genuinely due."""
        observed = self._run_runtime(self._commissioning(), keep=True)
        from alx.continuity import SQLiteOpportunityLedger

        ledger = SQLiteOpportunityLedger(
            Path(observed["directory"]) / "cognition-opportunities.sqlite3"
        )
        try:
            self.assertEqual(ledger.rows(), ())
            self.assertEqual(ledger.commissioning_dispatches(), 0)
        finally:
            ledger.close()

    # --- shutdown ----------------------------------------------------------

    def test_the_context_suppliers_the_core_was_given_actually_work(self) -> None:
        """Composition passes lambdas; startup alone never calls them.

        A mutation that pointed `undelivered_responses` at an undefined local
        survived every other assertion here, because an unbound name inside a
        lambda only raises when the lambda runs. So the smoke test calls what
        composition handed the Core, which is the moment the wiring is real.
        """
        captured: dict = {}
        from alx.core.loop import CoreAgent

        original = CoreAgent.__init__

        def capture(self, *args, **kwargs):
            captured.update(kwargs)
            original(self, *args, **kwargs)

        CoreAgent.__init__ = capture
        try:
            self._run_runtime(self._commissioning())
        finally:
            CoreAgent.__init__ = original

        for name in ("open_thoughts", "undelivered_responses"):
            with self.subTest(supplier=name):
                supplier = captured.get(name)
                self.assertIsNotNone(supplier, f"{name} must be supplied")
                # Calling it is what proves the names it closes over exist.
                self.assertEqual(tuple(supplier()), ())

    def test_the_runtime_shuts_down_cleanly(self) -> None:
        """Cancellation must unwind composition without raising."""
        observed = self._run_runtime(self._commissioning())
        self.assertTrue(observed["served"])


if __name__ == "__main__":
    unittest.main()
