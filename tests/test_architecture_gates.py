from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_architecture import Rules, check_source, load_rules
from scripts.check_governance import (
    _check_greptile,
    _check_identity_checksum,
    _check_law_checksum,
    check_repository,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ArchitectureGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(REPOSITORY_ROOT)

    def inspect(self, relative_path: str, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / self.rules.source_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            return [violation.message for violation in check_source(root, self.rules)]

    def assert_rejected(self, relative_path: str, source: str, expected: str) -> None:
        messages = self.inspect(relative_path, source)
        self.assertTrue(
            any(expected in message for message in messages),
            f"expected {expected!r} in {messages!r}",
        )

    def test_approved_dependency_is_accepted(self) -> None:
        messages = self.inspect(
            "core/agent_loop.py", "from alx.contracts.goal import Goal\n"
        )
        self.assertEqual([], messages)

    def test_tool_cannot_import_core(self) -> None:
        self.assert_rejected(
            "tools/mail.py",
            "from alx.core.agent_loop import AgentLoop\n",
            "tools may not import core",
        )

    def test_model_sdk_is_provider_only(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            "from xai_sdk import Client\n",
            "allowed only in providers",
        )

    def test_provider_http_client_is_provider_only(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            "import httpx\n",
            "allowed only in providers",
        )

    def test_certificate_bundle_is_provider_only(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            "import certifi\n",
            "allowed only in providers",
        )

    def test_websocket_transport_is_limited_to_providers_and_interfaces(self) -> None:
        self.assertEqual(
            [],
            self.inspect("interfaces/live_voice.py", "import websockets\n"),
        )
        self.assert_rejected(
            "core/agent_loop.py",
            "import websockets\n",
            "allowed only in interfaces, providers",
        )

    def test_raw_language_is_rejected_at_tool_boundary(self) -> None:
        self.assert_rejected(
            "tools/mail.py",
            "def search(user_text: str) -> list[str]:\n    return []\n",
            "raw-language parameter",
        )

    def test_phrase_comparison_is_rejected(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            'def choose(message: str) -> str:\n    if message.lower() == "send email":\n        return "mail"\n    return ""\n',
            "raw-language comparison",
        )

    def test_fixed_text_action_map_is_rejected(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            'actions = {"send email": "mail", "check diary": "calendar"}\n',
            "fixed text-to-action mapping",
        )

    def test_relative_import_is_rejected(self) -> None:
        self.assert_rejected(
            "core/agent_loop.py",
            "from ..contracts import Goal\n",
            "relative imports are prohibited",
        )

    def test_workflow_source_name_is_rejected(self) -> None:
        self.assert_rejected(
            "tools/invoice_workflow.py",
            "VALUE = 1\n",
            "forbidden routing/workflow source name",
        )

    def test_wake_word_activation_is_rejected(self) -> None:
        self.assert_rejected(
            "interfaces/audio.py",
            "def wake_word_detected(samples: bytes) -> bool:\n    return True\n",
            "forbidden routing/workflow identifier",
        )


class GovernanceGateTests(unittest.TestCase):
    def test_repository_governance_passes(self) -> None:
        self.assertEqual([], check_repository(REPOSITORY_ROOT))

    def test_greptile_constitutional_config_passes(self) -> None:
        violations: list[str] = []
        _check_greptile(REPOSITORY_ROOT, violations)
        self.assertEqual([], violations)

    def test_missing_greptile_rule_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPOSITORY_ROOT / ".greptile", root / ".greptile")
            config_path = root / ".greptile/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["rules"] = [
                rule
                for rule in config["rules"]
                if rule["id"] != "alx-dynamic-reasoning"
            ]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            violations: list[str] = []
            _check_greptile(root, violations)
            self.assertTrue(
                any("alx-dynamic-reasoning" in violation for violation in violations),
                violations,
            )

    def test_automatic_greptile_reviews_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPOSITORY_ROOT / ".greptile", root / ".greptile")
            (root / "governance").mkdir()
            shutil.copy(
                REPOSITORY_ROOT / "governance/GREPTILE.sha256",
                root / "governance/GREPTILE.sha256",
            )
            config_path = root / ".greptile/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["triggerOnUpdates"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            violations: list[str] = []
            _check_greptile(root, violations)
            self.assertTrue(
                any(
                    "automatic commit re-reviews must remain disabled" in violation
                    for violation in violations
                ),
                violations,
            )

    def test_silent_identity_rewrite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "governance").mkdir()
            shutil.copy(
                REPOSITORY_ROOT / "IDENTITY_AND_MEMORY.md",
                root / "IDENTITY_AND_MEMORY.md",
            )
            shutil.copy(
                REPOSITORY_ROOT / "governance/IDENTITY_AND_MEMORY.sha256",
                root / "governance/IDENTITY_AND_MEMORY.sha256",
            )
            identity = root / "IDENTITY_AND_MEMORY.md"
            identity.write_text(
                identity.read_text(encoding="utf-8") + "\nUnapproved change.\n",
                encoding="utf-8",
            )
            violations: list[str] = []
            _check_identity_checksum(root, violations)
            self.assertTrue(
                any("explicit owner approval" in violation for violation in violations),
                violations,
            )

    def test_silent_law_rewrite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "governance").mkdir()
            shutil.copy(REPOSITORY_ROOT / "LAWS_OF_ALX.md", root / "LAWS_OF_ALX.md")
            shutil.copy(
                REPOSITORY_ROOT / "governance/LAWS_OF_ALX.sha256",
                root / "governance/LAWS_OF_ALX.sha256",
            )
            laws = root / "LAWS_OF_ALX.md"
            laws.write_text(laws.read_text(encoding="utf-8") + "\nUnapproved change.\n", encoding="utf-8")
            violations: list[str] = []
            _check_law_checksum(root, violations)
            self.assertTrue(any("owner-approved amendment" in item for item in violations), violations)


if __name__ == "__main__":
    unittest.main()


class AlxAuthorshipBoundaryTests(ArchitectureGateTests):
    """Only the authoritative reasoning path may author AL/X's words.

    Law 1 forbids a second assistant voice. A recovery handler nevertheless
    added a fixed sentence to the voice error event — "That turn failed before
    I could answer. Nothing was changed. Please say that again." — which reads
    to Friedl as AL/X speaking when she never reasoned at all.

    Each test is a mutation: it reintroduces one way for a non-reasoning
    component to speak and asserts the gate rejects it. Technical codes,
    diagnostics and structural phase labels stay allowed and are asserted too.
    """

    def test_a_composed_field_on_a_voice_event_is_rejected(self) -> None:
        self.assert_rejected(
            "interfaces/server.py",
            "def send(message: dict) -> None:\n"
            "    message['notice'] = 'That turn failed. Please say it again.'\n",
            "voice event may not carry composed wording",
        )

    def test_composed_wording_in_an_event_literal_is_rejected(self) -> None:
        self.assert_rejected(
            "interfaces/server.py",
            "def build() -> dict:\n"
            "    return {'type': 'phase', 'value': 'error',\n"
            "            'message': 'Something went wrong, sorry.'}\n",
            "voice event may not carry composed wording",
        )

    def test_every_conversational_field_name_is_covered(self) -> None:
        """Renaming the field must not evade the rule."""
        for field in ("notice", "message", "text", "content", "speech", "say"):
            with self.subTest(field=field):
                self.assert_rejected(
                    "interfaces/server.py",
                    "def send(event: dict) -> None:\n"
                    f"    event[{field!r}] = 'Please try that again.'\n",
                    "voice event may not carry composed wording",
                )

    def test_a_literal_sentence_reaching_speech_is_rejected(self) -> None:
        self.assert_rejected(
            "interfaces/live_voice.py",
            "async def speak(synthesizer, conversation_id) -> None:\n"
            "    async for chunk in synthesizer.synthesize(\n"
            "        'Sorry, something went wrong.', conversation_id\n"
            "    ):\n"
            "        pass\n",
            "only the authoritative Core response may reach speech synthesis",
        )

    def test_a_capability_result_reaching_speech_is_rejected(self) -> None:
        """A tool result is evidence for AL/X, never her words."""
        self.assert_rejected(
            "interfaces/live_voice.py",
            "async def speak(synthesizer, attempt, conversation_id) -> None:\n"
            "    async for chunk in synthesizer.synthesize(\n"
            "        attempt.result.values, conversation_id\n"
            "    ):\n"
            "        pass\n",
            "only the authoritative Core response may reach speech synthesis",
        )

    def test_the_authoritative_response_reaching_speech_is_accepted(self) -> None:
        """The gate must not refuse the one legitimate path."""
        messages = self.inspect(
            "interfaces/live_voice.py",
            "async def speak(synthesizer, outcome, conversation_id) -> None:\n"
            "    async for chunk in synthesizer.synthesize(\n"
            "        outcome.response, conversation_id\n"
            "    ):\n"
            "        pass\n",
        )
        self.assertEqual([], messages)

    def test_a_technical_diagnostic_is_still_allowed(self) -> None:
        """Codes and logs are not AL/X speaking."""
        messages = self.inspect(
            "interfaces/server.py",
            "import logging\n\n\n"
            "def report(message: dict, reason: str) -> None:\n"
            "    logging.getLogger('alx').error('Voice session failed: %s', reason)\n"
            "    message['reason'] = reason\n",
        )
        self.assertEqual([], messages)

    def test_a_structural_phase_is_still_allowed(self) -> None:
        messages = self.inspect(
            "interfaces/server.py",
            "def phase(kind: str) -> dict:\n"
            "    return {'type': 'phase', 'value': kind}\n",
        )
        self.assertEqual([], messages)


class FrontendAuthorshipTests(unittest.TestCase):
    """The frontend renders authoritative state; it does not author AL/X."""

    def setUp(self) -> None:
        self.root = REPOSITORY_ROOT

    def test_the_live_frontend_passes(self) -> None:
        from scripts.check_architecture import _frontend_violations

        self.assertEqual([], _frontend_violations(self.root))

    def test_rendering_transported_prose_as_alx_is_rejected(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        from scripts.check_architecture import _frontend_violations

        with tempfile.TemporaryDirectory() as directory:
            root = _Path(directory)
            assets = root / "src/alx/interfaces/assets"
            assets.mkdir(parents=True)
            (assets / "app.js").write_text(
                "function show(message) {\n"
                "  status.textContent = message.notice;\n"
                "}\n",
                encoding="utf-8",
            )
            found = _frontend_violations(root)
        self.assertTrue(found)
        self.assertIn("may not render transported prose", found[0].message)

    def _assets(self, directory, *, js: str = "", html: str = ""):
        from pathlib import Path as _Path

        root = _Path(directory)
        assets = root / "src/alx/interfaces/assets"
        assets.mkdir(parents=True)
        if js:
            (assets / "app.js").write_text(js, encoding="utf-8")
        if html:
            (assets / "index.html").write_text(html, encoding="utf-8")
        return root

    def test_first_person_phase_wording_is_rejected(self) -> None:
        """A label names a system state; it is not AL/X speaking.

        These are the exact strings that shipped: they read as her voice on a
        turn where she never reasoned at all.
        """
        import tempfile

        from scripts.check_architecture import _frontend_violations

        for label in ("I hear you", "Something interrupted me", "I'm listening"):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = self._assets(
                        directory,
                        js=(
                            "const phaseLabels = {\n"
                            '  listening: "Listening",\n'
                            f'  hearing: "{label}",\n'
                            "};\n"
                        ),
                    )
                    found = _frontend_violations(root)
                self.assertTrue(found, f"{label!r} must be rejected")
                self.assertIn("not a neutral system state", found[0].message)

    def test_user_directed_status_text_is_rejected(self) -> None:
        import tempfile

        from scripts.check_architecture import _frontend_violations

        with tempfile.TemporaryDirectory() as directory:
            root = self._assets(
                directory, html='<p id="status">Ready when you are</p>\n'
            )
            found = _frontend_violations(root)
        self.assertTrue(found)
        self.assertIn("not a neutral system state", found[0].message)

    def test_a_user_directed_fallback_label_is_rejected(self) -> None:
        import tempfile

        from scripts.check_architecture import _frontend_violations

        with tempfile.TemporaryDirectory() as directory:
            root = self._assets(
                directory,
                js='status.textContent = phaseLabels[phase] ?? "Sorry, try again.";\n',
            )
            found = _frontend_violations(root)
        self.assertTrue(found)
        self.assertIn("not a neutral system state", found[0].message)

    def test_only_the_approved_neutral_states_are_permitted(self) -> None:
        """The whitelist is exact, not a judgement about tone."""
        from scripts.check_architecture import NEUTRAL_PHASE_LABELS

        self.assertEqual(
            NEUTRAL_PHASE_LABELS,
            frozenset(
                {
                    "Ready",
                    "Listening",
                    "Hearing",
                    "Thinking",
                    "Speaking",
                    "Error",
                    "Disconnected",
                    "AL/X",
                }
            ),
        )

    def test_the_live_labels_are_all_neutral(self) -> None:
        from scripts.check_architecture import (
            NEUTRAL_PHASE_LABELS,
            _phase_label_violations,
        )

        path = REPOSITORY_ROOT / "src/alx/interfaces/assets/app.js"
        self.assertEqual(
            [], _phase_label_violations(path, str(path), path.read_text())
        )
        text = path.read_text()
        for prohibited in ("I hear you", "Something interrupted me"):
            self.assertNotIn(prohibited, text)

    def test_a_structural_phase_label_is_still_allowed(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        from scripts.check_architecture import _frontend_violations

        with tempfile.TemporaryDirectory() as directory:
            root = _Path(directory)
            assets = root / "src/alx/interfaces/assets"
            assets.mkdir(parents=True)
            (assets / "app.js").write_text(
                "function show(phase) {\n"
                '  status.textContent = phaseLabels[phase] ?? "AL/X";\n'
                "}\n",
                encoding="utf-8",
            )
            self.assertEqual([], _frontend_violations(root))


class DiagnosticsPrivacyBoundaryTests(ArchitectureGateTests):
    """The gate must reject every route that exports payload-bearing state.

    Governance decision D-012. Each test is a mutation: it introduces one
    prohibited diagnostic and asserts the gate rejects it. Without these the
    gate could silently stop enforcing and nothing would notice.

    The routes matter because AL/X processes private material in ordinary
    operation — a mail body sent for reasoning, Friedl's speech sent for
    transcription — and any of these would carry it out of the runtime.
    """

    def test_logging_an_exception_with_its_traceback_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import logging\n\n\n"
            "def run(error: Exception) -> None:\n"
            "    logging.getLogger('alx').exception('failed')\n",
            "prohibited diagnostic",
        )

    def test_exc_info_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import logging\n\n\n"
            "def run() -> None:\n"
            "    logging.getLogger('alx').info('failed', exc_info=True)\n",
            "exc_info",
        )

    def test_formatting_a_traceback_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import traceback\n\n\n"
            "def run(error: Exception) -> str:\n"
            "    return ''.join(traceback.format_exception(error))\n",
            "format_exception",
        )

    def test_printing_a_traceback_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import traceback\n\n\n"
            "def run() -> None:\n"
            "    traceback.print_exc()\n",
            "print_exc",
        )

    def test_capturing_frame_locals_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import traceback\n\n\n"
            "def run(error: Exception) -> object:\n"
            "    return traceback.TracebackException(\n"
            "        type(error), error, None, capture_locals=True\n"
            "    )\n",
            "capture_locals",
        )

    def test_extracting_stack_frames_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import traceback\n\n\n"
            "def run(error: Exception) -> object:\n"
            "    return traceback.extract_tb(error.__traceback__)\n",
            "extract_tb",
        )

    def test_installing_an_exception_hook_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "import sys\n\n\n"
            "def install(handler: object) -> None:\n"
            "    sys.excepthook = handler\n",
            "excepthook",
        )

    def test_an_error_reporting_sink_is_rejected(self) -> None:
        """A future observability integration must not slip in unreviewed."""
        self.assert_rejected(
            "core/step.py",
            "def report(client: object, error: Exception) -> None:\n"
            "    client.capture_exception(error)\n",
            "capture_exception",
        )

    def test_recording_an_exception_on_a_span_is_rejected(self) -> None:
        self.assert_rejected(
            "core/step.py",
            "def report(span: object, error: Exception) -> None:\n"
            "    span.record_exception(error)\n",
            "record_exception",
        )

    def test_sanitised_reporting_is_accepted(self) -> None:
        """The boundary forbids payload, not diagnosis: codes remain allowed."""
        messages = self.inspect(
            "core/step.py",
            "import logging\n\n\n"
            "def run(error: Exception) -> None:\n"
            "    logging.getLogger('alx').info(\n"
            "        'step failed: %s', type(error).__name__\n"
            "    )\n",
        )
        self.assertEqual([], messages)

    def test_the_live_source_carries_no_prohibited_diagnostic(self) -> None:
        """The repository itself must satisfy the boundary, not just samples."""
        messages = [
            violation.render()
            for violation in check_source(REPOSITORY_ROOT, self.rules)
            if "prohibited diagnostic" in violation.message
        ]
        self.assertEqual([], messages)
