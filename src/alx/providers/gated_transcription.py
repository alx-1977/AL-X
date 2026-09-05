"""Speech transcription that pays only for audio around actual speech.

Cartesia bills per second of audio received and documents that "silence is
also included, even if no transcript is produced". Its turns endpoint also
documents that a paused stream does not finalize: "if you stop sending audio,
the server will wait for more audio chunks to arrive rather than assuming that
the user is silent". Those two facts together are why a microphone that is
merely open costs money indefinitely, and why the fix cannot be to pause the
stream and wait.

What this does instead is hold the socket closed until someone speaks. Local
detection decides when to open it and when to stop paying; Cartesia still
decides where the person's turn actually ended, because it receives the same
audio including the trailing silence and may emit `turn.end` before the local
grace period expires. When it does, transmission stops immediately.

One utterance is one short-lived connection, ended with the documented
`{"type": "close"}` command so the server flushes its buffer rather than
discarding the last second of speech.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass

from alx.contracts import AudioChunk, SpeechTranscriber, TranscriptionEvent, TranscriptionState
from alx.providers.speech_activity import (
    FRAME_MILLISECONDS,
    SpeechActivityDetector,
    SpeechGate,
)


LOGGER = logging.getLogger(__name__)

# Enough to precede a detected onset with the syllable that triggered it, plus
# the connection establishment that follows. Detection needs 120 ms of speech
# to confirm; 300 ms keeps that and a little before it.
PREROLL_MILLISECONDS = 300

# The bounded upper limit on trailing silence. Cartesia's endpointer normally
# ends the turn first, at which point transmission stops; this exists so a
# turn still closes when it does not. It must exceed an ordinary pause for
# thought, or a single sentence would be split into two person turns.
GRACE_MILLISECONDS = 2000


@dataclass
class SpeechTransmissionTotals:
    """Bounded per-session counters. No audio, no transcripts, no wording."""

    listening_seconds: float = 0.0
    speech_positive_seconds: float = 0.0
    transmitted_seconds: float = 0.0
    connections_opened: int = 0
    final_transcripts: int = 0
    provider_failures: int = 0

    def as_values(self) -> dict[str, float | int]:
        transmitted = round(self.transmitted_seconds, 3)
        positive = round(self.speech_positive_seconds, 3)
        return {
            "listening_seconds": round(self.listening_seconds, 3),
            "speech_positive_seconds": positive,
            "transmitted_seconds": transmitted,
            "connections_opened": self.connections_opened,
            "final_transcripts": self.final_transcripts,
            "provider_failures": self.provider_failures,
            # The efficiency ratio the defect is measured by. One means every
            # transmitted second was speech; the pathological case was
            # unbounded, transmitting hours against zero speech.
            "transmission_ratio": (
                round(transmitted / positive, 3) if positive > 0 else 0.0
            ),
        }


class GatedTranscriber:
    """A SpeechTranscriber that opens a paid stream only around speech."""

    def __init__(
        self,
        transcriber: SpeechTranscriber,
        sample_rate_hz: int,
        preroll_milliseconds: int = PREROLL_MILLISECONDS,
        grace_milliseconds: int = GRACE_MILLISECONDS,
        totals: SpeechTransmissionTotals | None = None,
        telemetry: Callable[[dict[str, float | int]], None] | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._detector = SpeechActivityDetector(sample_rate_hz)
        self._preroll_milliseconds = preroll_milliseconds
        self._grace_milliseconds = grace_milliseconds
        self._sample_rate_hz = sample_rate_hz
        self.totals = totals or SpeechTransmissionTotals()
        self._telemetry = telemetry

    def _frame_seconds(self) -> float:
        return FRAME_MILLISECONDS / 1000.0

    async def transcribe(
        self,
        chunks: AsyncIterable[AudioChunk],
    ) -> AsyncIterator[TranscriptionEvent]:
        gate = SpeechGate(
            self._detector,
            self._preroll_milliseconds,
            self._grace_milliseconds,
        )
        frame_seconds = self._frame_seconds()
        residue = b""
        stream_id = ""
        sequence = 0
        live: "asyncio.Queue[bytes | None]" | None = None
        utterance: AsyncIterator[TranscriptionEvent] | None = None

        async def close_utterance() -> AsyncIterator[TranscriptionEvent]:
            """Drain the events the provider still owes for this utterance."""
            nonlocal live, utterance
            if utterance is None:
                return
            assert live is not None
            await live.put(None)
            async for event in utterance:
                yield event
            utterance = None
            live = None
            gate.reset()

        async for chunk in chunks:
            stream_id = chunk.stream_id or stream_id
            residue += chunk.payload
            frames = self._detector.frames(residue)
            residue = residue[len(frames) * self._detector.frame_bytes :]
            for frame in frames:
                self.totals.listening_seconds += frame_seconds
                verdict, payloads = gate.push(frame)
                # Only frames the detector actually called speech count as
                # speech. Counting the trailing grace here would flatter the
                # ratio this defect is measured by.
                if gate.voiced:
                    self.totals.speech_positive_seconds += frame_seconds
                if verdict == "drop":
                    continue
                if utterance is None:
                    # Onset. The connection opens here and nowhere else: not
                    # when the microphone opens, and never merely because the
                    # previous one timed out.
                    live = asyncio.Queue()
                    utterance = self._utterance(
                        stream_id, sequence, list(payloads), live
                    )
                    sequence += 1
                else:
                    for payload in payloads:
                        await live.put(payload)
                if verdict == "finalize":
                    async for event in close_utterance():
                        yield event

        async for event in close_utterance():
            yield event
        self._publish()

    async def _utterance(
        self,
        stream_id: str,
        sequence: int,
        opening: list[bytes],
        live: "asyncio.Queue[bytes | None]",
    ) -> AsyncIterator[TranscriptionEvent]:
        """One paid connection, fed live so Cartesia can end it early.

        The audio is not buffered to completion first. Frames go out as they
        are classified, which keeps latency identical to the previous
        always-on stream and -- the reason Option C works -- lets Cartesia's
        own endpointer emit `turn.end` part-way through the trailing grace.
        When it does, `finished` is set, the sender stops, and the remaining
        grace is never transmitted or paid for.
        """
        self.totals.connections_opened += 1
        stream = f"{stream_id or 'utterance'}-{sequence}"
        finished = asyncio.Event()

        async def outgoing() -> AsyncIterator[AudioChunk]:
            index = 0
            for frame in opening:
                self.totals.transmitted_seconds += self._frame_seconds()
                yield AudioChunk(
                    stream, index, frame, "audio/pcm", self._sample_rate_hz
                )
                index += 1
            while not finished.is_set():
                frame = await live.get()
                if frame is None:
                    return
                self.totals.transmitted_seconds += self._frame_seconds()
                yield AudioChunk(
                    stream, index, frame, "audio/pcm", self._sample_rate_hz
                )
                index += 1

        try:
            async for event in self._transcriber.transcribe(outgoing()):
                if event.state is TranscriptionState.FINAL:
                    self.totals.final_transcripts += 1
                    # Cartesia has decided the turn is over. Nothing further
                    # is worth paying to send, whatever the local grace still
                    # had left to run.
                    finished.set()
                yield event
                if finished.is_set():
                    break
        except Exception:
            # A failed utterance does not end the session. The microphone
            # stays open, the gate returns to idle, and the next time someone
            # speaks a fresh connection is attempted. There is no retry of the
            # audio itself: repeating it would risk a duplicate person turn.
            self.totals.provider_failures += 1
            LOGGER.warning("Speech provider failed for one utterance")
        finally:
            finished.set()
        self._publish()

    def _publish(self) -> None:
        """Bounded aggregates only, once per utterance. Never per frame."""
        values = self.totals.as_values()
        LOGGER.info(
            "Speech transmission totals: listening=%.1fs speech=%.1fs "
            "transmitted=%.1fs ratio=%.2f connections=%d transcripts=%d "
            "failures=%d",
            values["listening_seconds"],
            values["speech_positive_seconds"],
            values["transmitted_seconds"],
            values["transmission_ratio"],
            values["connections_opened"],
            values["final_transcripts"],
            values["provider_failures"],
        )
        if self._telemetry is not None:
            self._telemetry(values)
