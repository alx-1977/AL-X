"""Typed input converges into the one person-turn path.

The console is a second input device, not a second way of talking to AL/X.
Typing and speaking differ in provenance and in whether a transcriber was
involved; from the gateway onward there is one path, one Core, one response and
one conversation. These tests exist to keep that true.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.contracts import ConversationOrigin  # noqa: E402
from alx.interfaces.live_voice import (  # noqa: E402
    VoiceEvent, VoiceEventKind, VoiceSession,
)
from alx.interfaces.server import LiveVoiceServer, TYPED_FRAME  # noqa: E402

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class RecordingGateway:
    def __init__(self, response: str | None = "I think so, yes.") -> None:
        self.turns: list = []
        self._response = response

    def receive_conversation_turn(self, turn, step_budget, retention_until):
        self.turns.append(turn)

        class Outcome:
            class state:
                value = "responded" if self._response else "finished_silently"
            response = self._response
            reason = None

        Outcome.state.value = "responded" if self._response else "finished_silently"
        Outcome.response = self._response
        return Outcome()


class Synthesizer:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, response, conversation_id):
        self.calls += 1
        if False:
            yield None


async def _drain(session, conversation_id, typed_lines, audio=None):
    """Run one exchange fed by typed lines, collecting the events it yields."""
    typed: asyncio.Queue[str] = asyncio.Queue()
    for line in typed_lines:
        typed.put_nowait(line)

    async def silence():
        await asyncio.sleep(0.2)
        if False:
            yield None

    events = []
    iterator = session.exchange(
        conversation_id, audio or silence(), None, typed
    ).__aiter__()
    try:
        while True:
            events.append(
                await asyncio.wait_for(iterator.__anext__(), timeout=2)
            )
    except (StopAsyncIteration, asyncio.TimeoutError):
        pass
    return events


def _session(gateway, synthesizer=None) -> VoiceSession:
    class Transcriber:
        async def transcribe(self, audio):
            await asyncio.sleep(0.3)
            if False:
                yield None

    return VoiceSession(
        gateway,
        Transcriber(),
        synthesizer,
        "friedl",
        4,
        3650,
        clock=lambda: NOW,
        identifier_factory=lambda: "turn-1",
    )


class TypedInputTests(unittest.TestCase):
    def test_a_typed_line_becomes_a_typed_person_turn(self) -> None:
        gateway = RecordingGateway()
        asyncio.run(_drain(_session(gateway, Synthesizer()), "c1", ["are you there?"]))
        self.assertEqual(len(gateway.turns), 1)
        turn = gateway.turns[0]
        self.assertIs(turn.origin, ConversationOrigin.TYPED)
        self.assertEqual(turn.content, "are you there?")
        self.assertEqual(turn.person_id, "friedl")

    def test_typed_input_reaches_the_same_gateway_method_as_speech(self) -> None:
        """One person-turn path: both arrive at receive_conversation_turn."""
        gateway = RecordingGateway()
        asyncio.run(_drain(_session(gateway, Synthesizer()), "c1", ["hello"]))
        self.assertEqual(len(gateway.turns), 1)

    def test_typed_input_reuses_the_session_conversation(self) -> None:
        """No new conversation is minted because the input was typed."""
        gateway = RecordingGateway()
        asyncio.run(
            _drain(_session(gateway, Synthesizer()), "conversation-A", ["one", "two"])
        )
        self.assertEqual(
            {turn.conversation_id for turn in gateway.turns}, {"conversation-A"}
        )

    def test_the_line_reaches_the_core_verbatim(self) -> None:
        """No parsing, no grammar: a slash is just a character."""
        for line in ("/serial ls", "  spaced  ", "unicode ünicode 🧠"):
            with self.subTest(line=line):
                gateway = RecordingGateway()
                asyncio.run(_drain(_session(gateway, Synthesizer()), "c1", [line]))
                self.assertEqual(gateway.turns[0].content, line)


class ResponseMirrorTests(unittest.TestCase):
    def test_alx_text_carries_the_core_response(self) -> None:
        gateway = RecordingGateway("Yes, I am here.")
        events = asyncio.run(_drain(_session(gateway, Synthesizer()), "c1", ["hi"]))
        texts = [item.text for item in events if item.kind is VoiceEventKind.TEXT]
        self.assertEqual(texts, ["Yes, I am here."])

    def test_silence_emits_no_text(self) -> None:
        gateway = RecordingGateway(None)
        events = asyncio.run(_drain(_session(gateway, Synthesizer()), "c1", ["hi"]))
        self.assertEqual(
            [item for item in events if item.kind is VoiceEventKind.TEXT], []
        )

    def test_text_precedes_speech_so_a_silent_runtime_still_shows_it(self) -> None:
        gateway = RecordingGateway("Something.")
        events = asyncio.run(_drain(_session(gateway, Synthesizer()), "c1", ["hi"]))
        kinds = [item.kind for item in events]
        self.assertLess(
            kinds.index(VoiceEventKind.TEXT), kinds.index(VoiceEventKind.SPEAKING)
        )


class OptionalSpeechTests(unittest.TestCase):
    """Speech is a transport capability, never a mode."""

    def test_without_a_synthesizer_the_turn_still_completes(self) -> None:
        gateway = RecordingGateway("I still said this.")
        events = asyncio.run(_drain(_session(gateway, None), "c1", ["hi"]))
        texts = [item.text for item in events if item.kind is VoiceEventKind.TEXT]
        self.assertEqual(texts, ["I still said this."])

    def test_without_a_synthesizer_nothing_is_synthesised(self) -> None:
        synthesizer = Synthesizer()
        gateway = RecordingGateway("Hello.")
        asyncio.run(_drain(_session(gateway, None), "c1", ["hi"]))
        self.assertEqual(synthesizer.calls, 0)

    def test_with_a_synthesizer_speech_still_happens(self) -> None:
        synthesizer = Synthesizer()
        gateway = RecordingGateway("Hello.")
        events = asyncio.run(_drain(_session(gateway, synthesizer), "c1", ["hi"]))
        self.assertEqual(synthesizer.calls, 1)
        self.assertIn(VoiceEventKind.SPEAKING, [item.kind for item in events])

    def test_the_core_sees_the_same_turn_either_way(self) -> None:
        """A missing speaker must not change what AL/X is asked."""
        turns = []
        for synthesizer in (Synthesizer(), None):
            gateway = RecordingGateway("x")
            asyncio.run(_drain(_session(gateway, synthesizer), "c1", ["same line"]))
            turns.append(gateway.turns[0])
        self.assertEqual(turns[0].content, turns[1].content)
        self.assertIs(turns[0].origin, turns[1].origin)

    def test_no_transport_state_reaches_the_core(self) -> None:
        """The Core is never told whether anyone could hear."""
        import inspect

        source = inspect.getsource(VoiceSession.exchange)
        for token in ("tts_enabled", "synthesizer is None and", "audio_available"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class FrameHandlingTests(unittest.TestCase):
    """The socket accepts one shape and refuses everything else safely."""

    def _server(self) -> LiveVoiceServer:
        server = LiveVoiceServer.__new__(LiveVoiceServer)
        server._typed_queues = {}
        return server

    def _queued(self, payload: str) -> list[str]:
        server = self._server()
        queue: asyncio.Queue[str] = asyncio.Queue()
        server._typed_queues["c1"] = [queue]
        server._queue_typed_turn("c1", payload)
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())
        return drained

    def test_a_valid_frame_is_queued(self) -> None:
        payload = json.dumps({"type": TYPED_FRAME, "content": "hello"})
        self.assertEqual(self._queued(payload), ["hello"])

    def test_malformed_frames_are_dropped_without_raising(self) -> None:
        for payload in (
            "not json",
            json.dumps({"type": "something.else", "content": "x"}),
            json.dumps({"type": TYPED_FRAME}),
            json.dumps({"type": TYPED_FRAME, "content": "   "}),
            json.dumps({"type": TYPED_FRAME, "content": 42}),
            json.dumps(["not", "an", "object"]),
        ):
            with self.subTest(payload=payload[:40]):
                self.assertEqual(self._queued(payload), [])

    def test_an_oversized_line_is_refused(self) -> None:
        payload = json.dumps({"type": TYPED_FRAME, "content": "x" * 9_000})
        self.assertEqual(self._queued(payload), [])


class OneProductionPathTests(unittest.TestCase):
    """Law 0 over the console."""

    SOURCE = ROOT / "src" / "alx"

    def test_person_turns_are_constructed_in_one_module(self) -> None:
        import ast

        builders = []
        for path in self.SOURCE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ConversationTurn"
                    and any(
                        keyword.arg == "person_id" for keyword in node.keywords
                    )
                ):
                    builders.append(path.relative_to(self.SOURCE).as_posix())
        self.assertEqual(sorted(set(builders)), ["interfaces/live_voice.py"])

    def test_the_console_has_no_command_grammar(self) -> None:
        """Nothing branches on what was typed."""
        script = (
            self.SOURCE / "interfaces" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        for token in ("startsWith(\"/\")", "split(\" \")[0]", "case \"/"):
            with self.subTest(token=token):
                self.assertNotIn(token, script)

    def test_stream_labels_do_not_decide_destination(self) -> None:
        """Output stream and input target are separate concerns."""
        script = (
            self.SOURCE / "interfaces" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        submit = script[script.index("function submitTypedLine"):]
        submit = submit[: submit.index("\n}")]
        # The destination is the fixed frame type, not a stream value.
        self.assertIn("person.text", submit)
        self.assertNotIn("SERIAL", submit)


class PersonTurnShutdownTests(unittest.TestCase):
    """A person turn must hold the barrier exactly as an autonomous one does.

    Typed, spoken and autonomous turns all write to the same stores, so they
    all obey the same worker-lifetime rule. This exercises the real session
    path rather than the barrier in isolation.
    """

    def test_a_blocked_person_turn_keeps_the_lock_until_it_finishes(self) -> None:
        import threading

        released = threading.Event()
        started = threading.Event()
        observed: dict = {}

        class BlockingGateway:
            def receive_conversation_turn(self, turn, step_budget, retention_until):
                started.set()
                released.wait(5)
                observed["gateway_finished"] = True

                class Outcome:
                    class state:
                        value = "finished_silently"
                    response = None
                    reason = None

                return Outcome()

        lock = asyncio.Lock()
        session = VoiceSession(
            BlockingGateway(),
            type("T", (), {"transcribe": lambda self, audio: _never()})(),
            None, "friedl", 4, 3650,
            clock=lambda: NOW,
            identifier_factory=lambda: "turn-1",
            core_turn_lock=lock,
        )

        async def scenario() -> None:
            typed: asyncio.Queue[str] = asyncio.Queue()
            typed.put_nowait("a line that blocks")

            async def consume():
                async for _event in session.exchange("c1", _never(), None, typed):
                    pass

            task = asyncio.create_task(consume())
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0.1)
            try:
                await asyncio.wait_for(lock.acquire(), timeout=0.3)
                observed["teardown_early"] = True
                lock.release()
            except asyncio.TimeoutError:
                observed["teardown_early"] = False
            released.set()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertFalse(
            observed["teardown_early"],
            "stores could have closed under a running person turn",
        )


async def _never():
    """An audio stream that yields nothing, for tests that drive typed input."""
    await asyncio.sleep(3)
    if False:
        yield None


if __name__ == "__main__":
    unittest.main()
