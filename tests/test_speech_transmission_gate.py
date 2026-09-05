"""The paid speech boundary: audio reaches Cartesia only around real speech.

These tests exist because of a measured defect. On 2026-09-05 AL/X sat
listening for two hours, transcribed nothing, and still sent roughly 7,272
seconds of audio to Cartesia, which bills per second received and counts
silence. The microphone being open was enough to spend money.

Every test here drives the real `GatedTranscriber` over real PCM through the
real detector. None of them assert against a constant the implementation
happens to hold.
"""

from __future__ import annotations

import asyncio
import math
import random
import struct
from datetime import UTC, datetime

import pytest

from alx.contracts import AudioChunk, TranscriptionEvent, TranscriptionState
from alx.providers.gated_transcription import GatedTranscriber


SAMPLE_RATE = 16000


def _pcm(sample_of, milliseconds: int) -> bytes:
    count = SAMPLE_RATE * milliseconds // 1000
    return struct.pack(
        "<%dh" % count,
        *[max(-32768, min(32767, int(sample_of(i)))) for i in range(count)],
    )


def silence(milliseconds: int) -> bytes:
    return _pcm(lambda index: 0, milliseconds)


def speech(milliseconds: int) -> bytes:
    """Voiced audio: a glottal fundamental with two formants above it."""
    return _pcm(
        lambda index: 9000 * math.sin(2 * math.pi * 120 * index / SAMPLE_RATE)
        + 4000 * math.sin(2 * math.pi * 700 * index / SAMPLE_RATE)
        + 2500 * math.sin(2 * math.pi * 1220 * index / SAMPLE_RATE),
        milliseconds,
    )


def room_tone(milliseconds: int, seed: int = 5) -> bytes:
    """Fan rumble and hiss at the level of an ordinary desk."""
    generator = random.Random(seed)
    return _pcm(
        lambda index: 400 * math.sin(2 * math.pi * 60 * index / SAMPLE_RATE)
        + generator.gauss(0, 120),
        milliseconds,
    )


def typing(milliseconds: int, seed: int = 9) -> bytes:
    """Room tone with a keyboard impulse roughly every 400 ms."""
    generator = random.Random(seed)
    count = SAMPLE_RATE * milliseconds // 1000
    samples = [
        400 * math.sin(2 * math.pi * 60 * index / SAMPLE_RATE)
        + generator.gauss(0, 120)
        for index in range(count)
    ]
    for start in range(0, count, int(SAMPLE_RATE * 0.4)):
        for offset in range(int(SAMPLE_RATE * 0.015)):
            if start + offset < count:
                samples[start + offset] += (
                    14000
                    * math.sin(2 * math.pi * 2400 * offset / SAMPLE_RATE)
                    * math.exp(-offset / 60)
                )
    return _pcm(lambda index: samples[index], milliseconds)


class RecordingTranscriber:
    """Stands in for Cartesia and records exactly what it was paid to receive."""

    def __init__(self, transcript: str = "hello", fail: bool = False) -> None:
        self.utterances: list[float] = []
        self.audio: list[bytes] = []
        self._transcript = transcript
        self._fail = fail

    async def transcribe(self, chunks):
        received = bytearray()
        async for chunk in chunks:
            received += chunk.payload
        self.audio.append(bytes(received))
        self.utterances.append(len(received) / (SAMPLE_RATE * 2))
        if self._fail:
            raise RuntimeError("provider unavailable")
        yield TranscriptionEvent(
            "stream",
            f"event-{len(self.utterances)}",
            TranscriptionState.FINAL,
            self._transcript,
            datetime.now(UTC),
        )

    @property
    def seconds_received(self) -> float:
        return sum(self.utterances)

    def first_utterance_prefix(self, size: int) -> bytes:
        """The opening bytes of the first paid stream, for pre-roll checks."""
        return self.audio[0][:size] if self.audio else b""


async def _run(transcriber, *blobs):
    gate = GatedTranscriber(transcriber, SAMPLE_RATE)

    async def microphone():
        for index, blob in enumerate(blobs):
            yield AudioChunk("mic", index, blob, "audio/pcm", SAMPLE_RATE)

    events = [event async for event in gate.transcribe(microphone())]
    return events, gate


def test_a_two_hours_listening_without_speech_costs_nothing():
    """A — the exact measured failure: listening, typing, no speech."""
    provider = RecordingTranscriber()
    # Two hours compressed to its acoustic content: room tone and typing.
    # Real time is irrelevant; what was billed was audio, not duration.
    events, gate = asyncio.run(
        _run(provider, room_tone(60_000), typing(60_000), room_tone(60_000))
    )
    assert events == []
    assert provider.seconds_received == 0.0
    assert gate.totals.connections_opened == 0
    assert gate.totals.final_transcripts == 0
    # AL/X stayed available throughout: every frame was heard locally.
    assert gate.totals.listening_seconds == pytest.approx(180.0, abs=0.5)


def test_b_long_silence_then_speech_keeps_the_first_word():
    """B — onset survives ten minutes of silence before it."""
    provider = RecordingTranscriber()
    events, gate = asyncio.run(
        _run(provider, silence(600_000), speech(1200), silence(2400))
    )
    assert [event.content for event in events] == ["hello"]
    assert gate.totals.connections_opened == 1
    # The ten minutes of silence was never transmitted.
    assert provider.seconds_received < 5.0
    # Detection needs several consecutive voiced frames to confirm, so the
    # frames that triggered it are already in the past when it fires. The
    # pre-roll is what puts them back. Without it the utterance would begin
    # mid-word, so the audio Cartesia receives must start strictly before
    # the point of detection.
    onset = provider.first_utterance_prefix(len(silence(20)))
    assert onset == silence(20), "the utterance must open with pre-roll audio"


def test_c_transmission_stops_after_the_bounded_grace():
    """C — speech then long idle: the connection does not stay fed."""
    provider = RecordingTranscriber()
    events, gate = asyncio.run(
        _run(provider, speech(800), silence(120_000))
    )
    assert len(events) == 1
    assert gate.totals.connections_opened == 1
    # Two minutes of trailing silence, and only the bounded grace was paid for.
    assert provider.seconds_received < 5.0


def test_d_a_pause_for_thought_stays_one_turn():
    """D — a natural mid-sentence pause must not become a second person turn."""
    provider = RecordingTranscriber()
    events, gate = asyncio.run(
        _run(
            provider,
            speech(900),
            silence(700),
            speech(900),
            silence(2400),
        )
    )
    assert gate.totals.connections_opened == 1
    assert len(events) == 1


def test_e_two_utterances_do_not_pay_for_the_gap():
    """E — speech, long idle, speech: both heard, the gap never sent."""
    provider = RecordingTranscriber()
    events, gate = asyncio.run(
        _run(
            provider,
            speech(800),
            silence(2400),
            silence(300_000),
            speech(800),
            silence(2400),
        )
    )
    assert len(events) == 2
    assert gate.totals.connections_opened == 2
    # Five minutes separated them; nothing like that was transmitted.
    assert provider.seconds_received < 10.0


def test_f_silence_alone_never_opens_a_connection():
    """F — no idle reconnect loop: only speech opens a paid stream."""
    provider = RecordingTranscriber()
    events, gate = asyncio.run(
        _run(provider, *[silence(30_000) for _ in range(10)])
    )
    assert gate.totals.connections_opened == 0
    assert provider.seconds_received == 0.0
    assert events == []


def test_g_audio_withheld_by_the_transport_is_never_transmitted():
    """G — while AL/X speaks the browser sends nothing, so nothing is paid."""
    provider = RecordingTranscriber()
    # The transport already stops sending during playback. What matters here
    # is that a gap in the microphone stream cannot itself open a connection.
    events, gate = asyncio.run(_run(provider, silence(5_000)))
    assert gate.totals.connections_opened == 0
    assert events == []


def test_h_a_provider_failure_does_not_duplicate_or_stick():
    """H — one failed utterance, no retry of the audio, session continues."""
    provider = RecordingTranscriber(fail=True)
    events, gate = asyncio.run(
        _run(provider, speech(800), silence(2400), speech(800), silence(2400))
    )
    # Each utterance was attempted exactly once. Re-sending the audio would
    # risk the same words arriving as two person turns.
    assert len(provider.utterances) == 2
    assert gate.totals.provider_failures == 2
    assert events == []


def test_i_room_noise_and_typing_do_not_open_a_paid_stream():
    """I — the false-positive case that would quietly restore the defect."""
    provider = RecordingTranscriber()
    events, gate = asyncio.run(
        _run(provider, room_tone(30_000, seed=2), typing(30_000, seed=4))
    )
    assert gate.totals.connections_opened == 0
    assert provider.seconds_received == 0.0
    assert events == []


class EarlyEndpointTranscriber:
    """Cartesia deciding the turn ended before the local grace expired."""

    def __init__(self, end_after_seconds: float) -> None:
        self._threshold = end_after_seconds
        self.seconds_received = 0.0

    async def transcribe(self, chunks):
        received = 0
        ended = False
        async for chunk in chunks:
            received += len(chunk.payload)
            if not ended and received >= self._threshold * SAMPLE_RATE * 2:
                ended = True
                yield TranscriptionEvent(
                    "stream",
                    "event",
                    TranscriptionState.FINAL,
                    "done",
                    datetime.now(UTC),
                )
            # Deliberately keeps consuming afterwards. A real socket does not
            # stop accepting audio just because it emitted `turn.end`, so a
            # gate that ignored the event would go on sending -- and paying.
        self.seconds_received = received / (SAMPLE_RATE * 2)


def test_cartesia_ending_the_turn_stops_paid_transmission_early():
    """Cartesia stays the turn authority, and ending early stops the spend.

    The local grace is an upper bound, not a target. When the provider's own
    endpointer decides the person has finished, the rest of the grace is
    neither transmitted nor paid for.
    """
    provider = EarlyEndpointTranscriber(end_after_seconds=1.0)
    events, gate = asyncio.run(_run(provider, speech(900), silence(2400)))
    assert len(events) == 1
    # Strictly less than speech plus the full grace, which is what would have
    # been sent had the local timer alone governed the utterance.
    assert gate.totals.transmitted_seconds < 2.0
    assert gate.totals.listening_seconds > gate.totals.transmitted_seconds
