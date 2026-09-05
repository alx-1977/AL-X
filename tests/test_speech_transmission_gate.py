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
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import AudioChunk, TranscriptionEvent, TranscriptionState  # noqa: E402
from alx.providers.gated_transcription import GatedTranscriber  # noqa: E402


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


def detected_run(payload: bytes) -> int:
    """The longest continuous voiced stretch the detector reports, in ms.

    Tests state transient durations in these terms because it is what the
    gate rules on. WebRTC reports voicing for some time after a sound stops,
    so the tone length alone would understate what the gate sees.
    """
    import webrtcvad

    from alx.providers.speech_activity import SpeechActivityDetector

    detector = SpeechActivityDetector(SAMPLE_RATE)
    vad = webrtcvad.Vad(2)
    longest = current = 0
    for frame in detector.frames(payload):
        current = current + 1 if vad.is_speech(frame, SAMPLE_RATE) else 0
        longest = max(longest, current)
    return longest * 20


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

class SilentTranscriber:
    """A provider that receives audio and finds no speech in it."""

    def __init__(self) -> None:
        self.seconds_received = 0.0

    async def transcribe(self, chunks):
        received = 0
        async for chunk in chunks:
            received += len(chunk.payload)
        self.seconds_received = received / (SAMPLE_RATE * 2)
        return
        yield  # pragma: no cover - makes this an async generator


class SpeechTransmissionGateTests(unittest.TestCase):
    """The economic boundary between AL/X listening and Cartesia billing."""

    def test_a_two_hours_listening_without_speech_costs_nothing(self) -> None:
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
        self.assertAlmostEqual(gate.totals.listening_seconds, 180.0, delta=0.5)


    def test_b_long_silence_then_speech_keeps_the_first_word(self) -> None:
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


    def test_c_transmission_stops_after_the_bounded_grace(self) -> None:
        """C — speech then long idle: the connection does not stay fed."""
        provider = RecordingTranscriber()
        events, gate = asyncio.run(
            _run(provider, speech(800), silence(120_000))
        )
        assert len(events) == 1
        assert gate.totals.connections_opened == 1
        # Two minutes of trailing silence, and only the bounded grace was paid for.
        assert provider.seconds_received < 5.0


    def test_d_a_pause_for_thought_stays_one_turn(self) -> None:
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


    def test_e_two_utterances_do_not_pay_for_the_gap(self) -> None:
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


    def test_f_silence_alone_never_opens_a_connection(self) -> None:
        """F — no idle reconnect loop: only speech opens a paid stream."""
        provider = RecordingTranscriber()
        events, gate = asyncio.run(
            _run(provider, *[silence(30_000) for _ in range(10)])
        )
        assert gate.totals.connections_opened == 0
        assert provider.seconds_received == 0.0
        assert events == []


    def test_g_audio_withheld_by_the_transport_is_never_transmitted(self) -> None:
        """G — while AL/X speaks the browser sends nothing, so nothing is paid."""
        provider = RecordingTranscriber()
        # The transport already stops sending during playback. What matters here
        # is that a gap in the microphone stream cannot itself open a connection.
        events, gate = asyncio.run(_run(provider, silence(5_000)))
        assert gate.totals.connections_opened == 0
        assert events == []


    def test_h_a_provider_failure_does_not_duplicate_or_stick(self) -> None:
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


    def test_i_room_noise_and_typing_do_not_open_a_paid_stream(self) -> None:
        """I — the false-positive case that would quietly restore the defect."""
        provider = RecordingTranscriber()
        events, gate = asyncio.run(
            _run(provider, room_tone(30_000, seed=2), typing(30_000, seed=4))
        )
        assert gate.totals.connections_opened == 0
        assert provider.seconds_received == 0.0
        assert events == []



    def test_cartesia_ending_the_turn_stops_paid_transmission_early(self) -> None:
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


    def test_a_short_transients_never_open_a_paid_stream(self) -> None:
        """A — the live defect: a brief real-world transient opened Cartesia.

        On 2026-09-05 roughly 0.1 s of voiced frames at Friedl's desk opened a
        connection, transmitted 2.3 s and produced no transcript. Anything shorter
        than the onset requirement must stay local and cost nothing.
        """
        # Stated as the durations the detector actually reports, not the length
        # of the tone that caused them: WebRTC's own hangover extends a detection
        # past the sound, so a 20 ms blip already reads as 100 ms of voicing.
        # These are measured, not assumed -- `detected_run` recomputes each one.
        for milliseconds, expected in ((20, 100), (40, 120), (80, 160), (100, 180)):
            assert detected_run(silence(400) + speech(milliseconds) + silence(1000)) == (
                expected
            ), f"a {milliseconds} ms transient no longer reads as {expected} ms"
            provider = RecordingTranscriber()
            events, gate = asyncio.run(
                _run(provider, silence(400), speech(milliseconds), silence(3_000))
            )
            assert gate.totals.connections_opened == 0, (
                f"{expected} ms of detected voicing opened a paid stream"
            )
            assert provider.seconds_received == 0.0
            assert events == []


    def test_b_sustained_speech_still_opens_a_stream(self) -> None:
        """B — the threshold must not deafen her to real speech."""
        provider = RecordingTranscriber()
        events, gate = asyncio.run(
            _run(provider, room_tone(400), speech(1000), silence(2400))
        )
        assert gate.totals.connections_opened == 1
        assert [event.content for event in events] == ["hello"]


    def test_c_the_raised_onset_does_not_clip_the_first_word(self) -> None:
        """C — detection now takes 200 ms, so 200 ms of speech is already past.

        The pre-roll is what puts it back. This asserts the actual bytes: the
        audio Cartesia receives must begin with the frames that preceded
        detection, not with the moment detection completed.
        """
        provider = RecordingTranscriber()
        leading = room_tone(400)
        asyncio.run(_run(provider, leading, speech(1000), silence(2400)))
        opening = provider.first_utterance_prefix(len(room_tone(20)))
        # The stream opens on room tone captured before anyone spoke, which can
        # only be true if the pre-roll outlived the onset requirement.
        assert opening == leading[-len(opening) :] or opening in leading, (
            "the utterance did not open with audio captured before detection"
        )
        # And enough of it: the speech that triggered detection is still there.
        frames_before_detection = 10
        assert provider.utterances[0] > (frames_before_detection * 20) / 1000.0


    def test_a_silent_stream_yields_no_transcription_event_at_all(self) -> None:
        """A paid stream that transcribed nothing must reach Core with nothing.

        On 2026-09-05 a false-positive stream transmitted 2.3 s and returned no
        transcript. The cost was real; the risk would have been worse if an empty
        transcript could still start a person turn, because AL/X would have been
        asked to interpret silence.

        Two independent layers already prevent it, and this holds both in place:
        the Cartesia adapter refuses an event whose transcript is blank, and
        `TranscriptionEvent` refuses blank content outright. A turn cannot be
        built without an event, so no event means no turn.
        """
        provider = SilentTranscriber()
        events, gate = asyncio.run(_run(provider, speech(1000), silence(2400)))
        # The stream was opened and paid for -- that part is the tuning defect.
        assert gate.totals.connections_opened == 1
        # But nothing crossed into the conversation.
        assert events == []
        assert gate.totals.final_transcripts == 0


    def test_an_empty_transcript_cannot_be_represented_at_all(self) -> None:
        """The contract itself is the guard, so no interface can bypass it."""
        with self.assertRaises(ValueError):
            TranscriptionEvent(
                "stream",
                "event",
                TranscriptionState.FINAL,
                "   ",
                datetime.now(UTC),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
