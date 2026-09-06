"""Unprompted speech appears in the terminal, as an answer does.

On 2026-09-06 the log showed speech synthesis running and audio playing for
turns nobody asked for, while the terminal showed no `ALX > ...` line for
them. The words were spoken and the transcript was missing.

The cause was one branch. An answer to Friedl yields a TEXT event before
synthesis, so the console mirrors what the Core said; an autonomous response
went straight to synthesis and yielded none. `_speak` even documents that her
wording "already reached the console", which was true only on the path that
had already emitted it.

What is asserted here is the property, not the ordering of an implementation:
whatever the Core decided to say aloud reaches the console, whichever occasion
prompted it.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.interfaces.live_voice import (  # noqa: E402
    VoiceEventKind,
    VoiceSession,
)


NOW = datetime(2026, 9, 6, 21, 35, tzinfo=UTC)
UNPROMPTED = "The follow-up came due, and the review is still not published."


class _Synthesizer:
    """Audible speech, without any speech provider."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, response, conversation_id):
        self.spoken.append(response)
        yield b"audio"


class _Transcriber:
    """No speech ever arrives, and the stream stays open.

    `transcription_end` would end the exchange when no event source is
    configured, so this waits instead: the delivery under test is what should
    drive the session.
    """

    async def transcribe(self, audio):
        await asyncio.Event().wait()
        if False:  # pragma: no cover - never reached
            yield None


async def _no_audio():
    if False:
        yield b""


class UnpromptedSpeechTranscriptTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, synthesizer) -> VoiceSession:
        return VoiceSession(
            gateway=object(),
            transcriber=_Transcriber(),
            synthesizer=synthesizer,
            person_id="friedl",
            step_budget=4,
            retention_days=30,
            clock=lambda: NOW,
        )

    async def _events(self, synthesizer):
        """Drive one autonomous delivery through the real session."""
        deliveries: asyncio.Queue[str] = asyncio.Queue()
        deliveries.put_nowait(UNPROMPTED)
        session = self._session(synthesizer)

        collected = []
        exchange = session.exchange(
            "conversation-1", _no_audio(), deliveries=deliveries
        )
        try:
            async with asyncio.timeout(5):
                async for event in exchange:
                    collected.append(event)
                    # One delivery is enough: stop once it has been spoken and
                    # the session is listening again.
                    if event.kind is VoiceEventKind.LISTENING and any(
                        item.kind is VoiceEventKind.SPEAKING for item in collected
                    ):
                        break
        finally:
            await exchange.aclose()
        return collected

    async def test_unprompted_speech_reaches_the_console(self) -> None:
        synthesizer = _Synthesizer()
        events = await self._events(synthesizer)

        # It was spoken.
        self.assertEqual(synthesizer.spoken, [UNPROMPTED])
        # And it was shown, in her own wording, unaltered.
        spoken_text = [
            event.text for event in events if event.kind is VoiceEventKind.TEXT
        ]
        self.assertEqual(
            spoken_text,
            [UNPROMPTED],
            "speech with no transcript line is what this test exists to stop",
        )

    async def test_the_transcript_precedes_the_audio(self) -> None:
        """Otherwise the terminal explains a voice that already spoke."""
        events = await self._events(_Synthesizer())
        kinds = [event.kind for event in events]
        self.assertIn(VoiceEventKind.TEXT, kinds)
        self.assertIn(VoiceEventKind.SPEAKING, kinds)
        self.assertLess(
            kinds.index(VoiceEventKind.TEXT),
            kinds.index(VoiceEventKind.SPEAKING),
        )

    async def test_it_is_shown_even_with_no_speech_transport(self) -> None:
        """A missing speaker must not also cost the transcript."""
        session = self._session(None)
        deliveries: asyncio.Queue[str] = asyncio.Queue()
        deliveries.put_nowait(UNPROMPTED)

        collected = []
        exchange = session.exchange(
            "conversation-1", _no_audio(), deliveries=deliveries
        )
        try:
            async with asyncio.timeout(5):
                async for event in exchange:
                    collected.append(event)
                    if event.kind is VoiceEventKind.LISTENING:
                        break
        finally:
            await exchange.aclose()

        self.assertEqual(
            [event.text for event in collected if event.kind is VoiceEventKind.TEXT],
            [UNPROMPTED],
        )


if __name__ == "__main__":
    unittest.main()
