"""Provider failures must never carry the request payload that caused them.

A provider request contains private material: a mail body sent for reasoning,
AL/X's spoken response sent for synthesis, Friedl's audio sent for
transcription. The client library attaches the request to its own exception,
so chaining that exception onto the failure AL/X propagates would keep the
payload reachable long after the call.

`raise ... from None` does not fix this. It clears `__cause__` and sets a
suppression flag, but the interpreter still records the original exception as
`__context__`, so the payload survives on the object. These tests therefore
check the object itself, not only its default rendering.
"""

from __future__ import annotations

import ssl
import traceback
from datetime import UTC, datetime, timedelta
import unittest
from typing import Any

import httpx

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.errors import ProviderError, raise_provider_failure

SECRET = "ARTIFICIAL-SECRET-PAYLOAD-8f21c"


def _payload_bearing_error() -> httpx.HTTPStatusError:
    """An exception shaped like the ones a real client raises: it holds the request."""
    request = httpx.Request("POST", "https://provider.invalid/v1", json={"input": SECRET})
    return httpx.HTTPStatusError("failed", request=request, response=httpx.Response(500, request=request))


def _reachable_text(error: BaseException) -> str:
    """Everything a diagnostic could pull off this exception and its chain."""
    seen: list[str] = []
    current: BaseException | None = error
    depth = 0
    while current is not None and depth < 10:
        seen.append(repr(current))
        seen.append(str(current))
        seen.extend(repr(value) for value in vars(current).values())
        request = getattr(current, "request", None)
        if request is not None:
            seen.append(repr(getattr(request, "content", b"")))
            seen.append(repr(dict(getattr(request, "headers", {}))))
        response = getattr(current, "response", None)
        if response is not None:
            seen.append(repr(getattr(response, "request", None)))
        current = current.__cause__ or current.__context__
        depth += 1
    return "\n".join(seen)


class ProviderFailureSanitisationTests(unittest.TestCase):
    def _raise_the_way_an_adapter_does(self) -> ProviderError:
        try:
            try:
                raise _payload_bearing_error()
            except httpx.HTTPError as error:
                code = type(error).__name__
            raise_provider_failure("openai", code)
        except ProviderError as failure:
            return failure
        raise AssertionError("the failure was not raised")

    def test_the_chain_is_severed_in_both_directions(self) -> None:
        failure = self._raise_the_way_an_adapter_does()
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)

    def test_the_payload_is_unreachable_through_the_object(self) -> None:
        failure = self._raise_the_way_an_adapter_does()
        self.assertNotIn(SECRET, _reachable_text(failure))

    def test_the_payload_is_absent_from_formatted_diagnostics(self) -> None:
        failure = self._raise_the_way_an_adapter_does()
        formatted = "".join(
            traceback.format_exception(type(failure), failure, failure.__traceback__)
        )
        self.assertNotIn(SECRET, formatted)

    def test_the_payload_is_absent_from_diagnostics_that_capture_locals(self) -> None:
        """A richer traceback walks frame locals, where the request would sit."""
        failure = self._raise_the_way_an_adapter_does()
        detailed = "".join(
            traceback.TracebackException(
                type(failure), failure, failure.__traceback__, capture_locals=True
            ).format()
        )
        self.assertNotIn(SECRET, detailed)

    def test_the_reason_survives_so_a_failure_is_still_diagnosable(self) -> None:
        failure = self._raise_the_way_an_adapter_does()
        self.assertEqual(failure.provider, "openai")
        self.assertEqual(failure.reason, "HTTPStatusError")

    def test_a_chained_failure_would_have_leaked(self) -> None:
        """Proves the tests above detect the defect they exist to prevent."""
        try:
            try:
                raise _payload_bearing_error()
            except httpx.HTTPError as error:
                raise ProviderError("openai", type(error).__name__) from error
        except ProviderError as chained:
            self.assertIn(SECRET, _reachable_text(chained))

    def test_from_none_alone_would_still_have_leaked(self) -> None:
        """`raise ... from None` clears __cause__ but the payload stays on __context__."""
        try:
            try:
                raise _payload_bearing_error()
            except httpx.HTTPError as error:
                raise ProviderError("openai", type(error).__name__) from None
        except ProviderError as suppressed:
            self.assertIsNone(suppressed.__cause__)
            self.assertIsNotNone(suppressed.__context__)
            self.assertIn(SECRET, _reachable_text(suppressed))


class AdapterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Every payload-bearing adapter applies the boundary, not just one.

    Each provider carries something private: the reasoning adapters carry the
    mail body and the conversation, the synthesizer carries what AL/X is about
    to say, the dictionary manager carries Friedl's own vocabulary. A boundary
    applied to one of them is not a boundary.
    """

    def test_the_openai_reasoning_adapter_sheds_the_request(self) -> None:
        from alx.providers import OpenAIReasoningModel

        adapter = OpenAIReasoningModel(
            "model", "unused", "https://model.invalid", 10, _failing_client(),
            streaming=False,
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(_model_request())
        self._assert_clean(caught.exception)

    def test_the_xai_reasoning_adapter_sheds_the_request(self) -> None:
        from alx.providers import XAIReasoningModel

        adapter = XAIReasoningModel(
            "model", "unused", "https://model.invalid", 10, _failing_client(),
            streaming=False,
        )
        with self.assertRaises(ProviderError) as caught:
            adapter.complete(_model_request())
        self._assert_clean(caught.exception)

    def test_the_pronunciation_adapter_sheds_the_request(self) -> None:
        from alx.providers.elevenlabs_pronunciation import (
            AliasRule,
            ElevenLabsDictionaryManager,
            PronunciationVocabulary,
        )

        manager = ElevenLabsDictionaryManager(
            "unused", "https://speech.invalid", _failing_client()
        )
        vocabulary = PronunciationVocabulary(
            schema_version=1,
            vocabulary_version="1",
            dictionary_name="alx",
            description="Vocabulary.",
            rules=(AliasRule(SECRET, "spoken", "name", False, True),),
        )
        with self.assertRaises(ProviderError) as caught:
            manager.deploy(vocabulary)
        self._assert_clean(caught.exception)

    async def test_the_speech_adapter_sheds_the_response_it_was_speaking(self) -> None:
        from alx.providers import ElevenLabsSynthesizer

        synthesizer = ElevenLabsSynthesizer(
            "model", "unused", "voice", "https://speech.invalid", "mp3_44100_128",
            10, "dictionary", "version", _failing_async_client(),
        )
        with self.assertRaises(ProviderError) as caught:
            async for _ in synthesizer.synthesize(SECRET):
                pass
        self._assert_clean(caught.exception)

    def test_the_transcription_adapter_sheds_the_audio_event(self) -> None:
        """A malformed event carries Friedl's speech; the failure must not."""
        from alx.providers.cartesia import CartesiaTranscriber

        with self.assertRaises(ProviderError) as caught:
            CartesiaTranscriber._parse_event(f'{{"transcript": "{SECRET}"', 1)
        self._assert_clean(caught.exception)

    async def test_the_transcription_stream_sheds_the_connection_failure(self) -> None:
        """The socket failure names the endpoint and key; the failure must not."""
        from alx.providers.cartesia import CartesiaTranscriber

        def refuse(endpoint: str, **options: Any) -> Any:
            raise ConnectionRefusedError(f"refused {endpoint} carrying {SECRET}")

        transcriber = CartesiaTranscriber(
            "model", SECRET, "wss://speech.invalid", "2024-11-13", "pcm_s16le",
            16000, 0.1, 0.2, 0.3, 400, connection_factory=refuse,
            ssl_context=ssl.create_default_context(),
        )

        async def no_audio() -> Any:
            return
            yield  # pragma: no cover - shapes this as an async generator

        with self.assertRaises(ProviderError) as caught:
            async for _ in transcriber.transcribe(no_audio()):
                pass
        self._assert_clean(caught.exception)

    def _assert_clean(self, failure: ProviderError) -> None:
        """Assert the boundary this fix actually guarantees.

        `unittest.assertRaises` sets `__traceback__` to None on the exception
        it stores, so a check written against `caught.exception` inspects a
        traceback with no frames and passes regardless. These assertions are
        about the exception object itself, which survives that stripping.

        Frame locals are deliberately not asserted here. See
        `FrameLocalsAreOutsideTheBoundaryTests` for why that is not a promise
        this layer can make.
        """
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)
        self.assertNotIn(SECRET, _reachable_text(failure))
        formatted = "".join(
            traceback.format_exception(type(failure), failure, failure.__traceback__)
        )
        self.assertNotIn(SECRET, formatted)



class FrameLocalsAreOutsideTheBoundaryTests(unittest.TestCase):
    """Record what this fix does not promise, so no one relies on it.

    A traceback built with `capture_locals=True` walks every frame in the
    stack and renders its variables. Any frame that was processing a mail body
    when the call failed still holds it: the adapter that built the request,
    and above it the Core turn that passed the body down. Clearing the
    adapter's own locals would not change that, because the caller's frame
    holds the same content independently.

    So richer diagnostics over a stack handling private material cannot be
    made safe at the provider boundary. The boundary that is enforceable is
    the exception object: no cause, no context, no retained request. What
    protects the frames is that AL/X logs only sanitised codes and never
    exports a traceback with captured locals from a payload-carrying path.
    """

    def test_capture_locals_still_reaches_the_payload(self) -> None:
        from alx.providers import OpenAIReasoningModel

        failure = _capture(
            lambda: OpenAIReasoningModel(
                "model", "unused", "https://model.invalid", 10, _failing_client(),
                streaming=False,
            ).complete(_model_request())
        )
        detailed = "".join(
            traceback.TracebackException(
                type(failure), failure, failure.__traceback__, capture_locals=True
            ).format()
        )
        self.assertIn(SECRET, detailed)

    def test_the_callers_frame_holds_it_independently(self) -> None:
        """Proves clearing adapter locals would not close this."""
        from alx.providers import OpenAIReasoningModel

        adapter = OpenAIReasoningModel(
            "model", "unused", "https://model.invalid", 10, _failing_client(),
            streaming=False,
        )

        def a_core_turn(body: str) -> Any:
            from alx.contracts import ModelMessage, ModelRequest, ModelRole

            return adapter.complete(
                ModelRequest(
                    (ModelMessage(ModelRole.USER, body),), "alx_decision",
                    {"type": "object"},
                )
            )

        failure = _capture(lambda: a_core_turn(SECRET))
        frames = traceback.TracebackException(
            type(failure), failure, failure.__traceback__, capture_locals=True
        ).stack
        holding = [f.name for f in frames if SECRET in str(f.locals)]
        self.assertIn("a_core_turn", holding)

    def test_ordinary_diagnostics_stay_clean(self) -> None:
        """The default rendering, which is what any log actually prints."""
        from alx.providers import OpenAIReasoningModel

        failure = _capture(
            lambda: OpenAIReasoningModel(
                "model", "unused", "https://model.invalid", 10, _failing_client(),
                streaming=False,
            ).complete(_model_request())
        )
        formatted = "".join(
            traceback.format_exception(type(failure), failure, failure.__traceback__)
        )
        self.assertNotIn(SECRET, formatted)


class RuntimeLogsAreSanitisedTests(unittest.TestCase):
    """End to end: a failing reasoning request must log only sanitised codes.

    This is what actually protects the frames. The exception boundary keeps
    the payload off the object; this keeps it out of the logs, because the
    Core records a type name and message and never a traceback.
    """

    def test_a_mail_bearing_turn_that_fails_logs_no_payload(self) -> None:
        """The real Core, a real mail body, a failing provider.

        This drives `CoreAgent.process` rather than reproducing its handler,
        so it keeps testing the boundary if that handler is ever rewritten.
        """
        import tempfile

        from alx.core.loop import CoreAgent, CoreState
        from alx.core.model_reasoner import ModelReasoner
        from alx.contracts import (
            ConversationOrigin,
            ConversationSnapshot,
            ConversationTurn,
        )
        from alx.goals import SQLiteGoalStore

        class FailingModel:
            """Fails the way the adapters now do: a clean ProviderError."""

            def complete(self, request: Any) -> Any:
                try:
                    raise _payload_bearing_error()
                except httpx.HTTPError as error:
                    code = type(error).__name__
                raise_provider_failure("openai", code)

        now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        retention = now + timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            core = CoreAgent(
                store,
                ModelReasoner(FailingModel(), "laws", "identity"),
                lambda call, state: None,
                (),
            )
            conversation = ConversationSnapshot(
                "conversation-1",
                (
                    ConversationTurn(
                        "conversation-1",
                        "turn-1",
                        ConversationOrigin.SPEECH_TRANSCRIPT,
                        f"Reply to the supplier who wrote: {SECRET}",
                        now,
                        "friedl",
                    ),
                ),
                1,
                retention,
            )

            with self.assertLogs("alx", level="DEBUG") as captured:
                outcome = core.process(conversation, retention, 3)

        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "reasoner_error")

        logged = "\n".join(captured.output)
        self.assertNotIn(SECRET, logged)
        self.assertIn("ProviderError", logged)
        self.assertIn("openai provider failure: HTTPStatusError", logged)

    def test_the_failing_outcome_carries_no_payload_either(self) -> None:
        """A diagnostic event returned to the caller is a route too."""
        import tempfile

        from alx.core.loop import CoreAgent
        from alx.core.model_reasoner import ModelReasoner
        from alx.contracts import (
            ConversationOrigin,
            ConversationSnapshot,
            ConversationTurn,
        )
        from alx.goals import SQLiteGoalStore

        class FailingModel:
            def complete(self, request: Any) -> Any:
                try:
                    raise _payload_bearing_error()
                except httpx.HTTPError as error:
                    code = type(error).__name__
                raise_provider_failure("openai", code)

        now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        retention = now + timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGoalStore(Path(directory) / "goals.sqlite3")
            self.addCleanup(store.close)
            core = CoreAgent(
                store,
                ModelReasoner(FailingModel(), "laws", "identity"),
                lambda call, state: None,
                (),
            )
            conversation = ConversationSnapshot(
                "conversation-1",
                (
                    ConversationTurn(
                        "conversation-1",
                        "turn-1",
                        ConversationOrigin.SPEECH_TRANSCRIPT,
                        f"Reply to the supplier who wrote: {SECRET}",
                        now,
                        "friedl",
                    ),
                ),
                1,
                retention,
            )
            with self.assertLogs("alx", level="DEBUG"):
                outcome = core.process(conversation, retention, 3)

        self.assertNotIn(SECRET, repr(outcome))

    def test_the_boundary_is_enforced_by_the_architecture_gate(self) -> None:
        """Promise 3 of D-012 is a build gate, not a substring scan here.

        `scripts/check_architecture.py` parses the source and rejects every
        prohibited diagnostic route; `tests/test_architecture_gates.py` proves
        it rejects each one. This asserts the live source satisfies it.
        """
        from scripts.check_architecture import check_source, load_rules

        root = Path(__file__).resolve().parents[1]
        offenders = [
            violation.render()
            for violation in check_source(root, load_rules(root))
            if "prohibited diagnostic" in violation.message
        ]
        self.assertEqual(offenders, [])


def _capture(call: Any) -> ProviderError:
    """Run `call` and return the failure with its traceback still attached.

    `assertRaises` discards the traceback, which would make any frame-level
    assertion vacuous. Tests that need real frames use this instead.
    """
    try:
        call()
    except ProviderError as failure:
        assert failure.__traceback__ is not None
        return failure
    raise AssertionError("no provider failure was raised")


def _model_request() -> Any:
    from alx.contracts import ModelMessage, ModelRequest, ModelRole

    return ModelRequest(
        (ModelMessage(ModelRole.USER, SECRET),),
        "alx_decision",
        {"type": "object"},
    )


def _failing_client() -> httpx.Client:
    """A transport that fails the way a provider outage does, echoing the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"echo": request.content.decode()})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _failing_async_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"echo": request.content.decode()})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class CreditExhaustionVisibilityTests(unittest.TestCase):
    """A spent account must be diagnosable, not a silent hang.

    Live failure: xAI began returning 403 mid-session. The panel said only
    `reasoner_error` and the log only `HTTPStatusError`, so the runtime looked
    broken when the account had simply run out of credit. A status code is a
    number the provider assigned to the outcome; it carries no part of the
    request, so it is safe to keep where the exception itself is not.
    """

    def test_a_status_code_is_recovered_from_a_failure(self) -> None:
        from alx.providers.errors import status_code_of

        class Response:
            status_code = 403

        class Failure(Exception):
            response = Response()

        self.assertEqual(status_code_of(Failure()), 403)

    def test_a_failure_without_a_response_yields_none(self) -> None:
        from alx.providers.errors import status_code_of

        self.assertIsNone(status_code_of(ValueError("boom")))
        self.assertIsNone(status_code_of(TimeoutError()))

    def test_a_non_integer_status_is_refused(self) -> None:
        """A provider object is not trusted to hold a sane value."""
        from alx.providers.errors import status_code_of

        class Response:
            status_code = "403 Forbidden"

        class Failure(Exception):
            response = Response()

        self.assertIsNone(status_code_of(Failure()))

    def test_the_response_body_is_never_read(self) -> None:
        """The body is untrusted external text and may quote the request."""
        from alx.providers.errors import status_code_of

        touched: list[str] = []

        class Response:
            status_code = 403

            @property
            def text(self):
                touched.append("text")
                return "secret request echoed back"

            @property
            def content(self):
                touched.append("content")
                return b"secret"

        class Failure(Exception):
            response = Response()

        status_code_of(Failure())
        self.assertEqual(touched, [])

    def test_the_credit_failure_is_logged_at_error_level(self) -> None:
        """Visible on a server with no browser attached."""
        source = (
            Path(__file__).resolve().parents[1] / "src/alx/providers/xai.py"
        ).read_text()
        self.assertIn("if status in (402, 403):", source)
        self.assertIn("out of credit", source)
        self.assertIn("LOGGER.error", source)

    def test_the_panel_names_the_credit_condition(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/alx/interfaces/assets/app.js"
        ).read_text()
        self.assertIn("status === 402 || status === 403", source)
        self.assertIn("credit or spending limit", source)


class KimiProviderTests(unittest.TestCase):
    """A second vendor on the same protocol is configuration, not code.

    The blueprint keeps the model a configuration choice: changing provider
    must not create a new conversation route, tool set or adapter. Kimi speaks
    the same OpenAI-style /v1/chat/completions the xAI transport already uses,
    so it is selected by base URL and key.
    """

    def settings(self, provider: str):
        from alx.config import ReasoningSettings

        return ReasoningSettings(
            provider,
            "kimi-k3",
            "key",
            "https://api.moonshot.ai",
            30,
            False,
            "default",
            "medium",
        )

    def test_kimi_builds_on_the_shared_transport(self) -> None:
        from alx.bootstrap.providers import _build_reasoning_model
        from alx.providers import XAIReasoningModel

        model = _build_reasoning_model(self.settings("kimi"), None)
        self.assertIsInstance(model, XAIReasoningModel)

    def test_an_unknown_provider_is_still_refused(self) -> None:
        """A silent fallback would hide a misconfiguration."""
        from alx.bootstrap.providers import _build_reasoning_model

        self.assertIsNone(_build_reasoning_model(self.settings("nonesuch"), None))

    def test_the_core_accepts_kimi_as_its_reasoning_provider(self) -> None:
        import ast

        source = (
            Path(__file__).resolve().parents[1] / "src/alx/bootstrap/providers.py"
        ).read_text()
        self.assertIn('settings.reasoning.provider in ("xai", "kimi")', source)
        ast.parse(source)

    def test_switching_vendor_adds_no_second_path(self) -> None:
        """One adapter, one transport: no parallel route was introduced."""
        source = (
            Path(__file__).resolve().parents[1] / "src/alx/bootstrap/providers.py"
        ).read_text()
        self.assertNotIn("KimiReasoningModel", source)
        self.assertFalse(
            (
                Path(__file__).resolve().parents[1] / "src/alx/providers/kimi.py"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
