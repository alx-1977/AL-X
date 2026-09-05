"""Local speech detection; it gates paid audio and decides nothing else.

This is a billing boundary, not a turn boundary. Cartesia remains the
authority on where a person's turn ends: it sees the same audio and emits
`turn.end` from its own endpointer. What this decides is narrower and purely
economic -- whether the next twenty milliseconds of microphone input are worth
paying to transmit.

The distinction matters because Cartesia bills per second of audio received
and states that "silence is also included, even if no transcript is
produced". A microphone that is merely available therefore costs money for as
long as it is open, whether or not anyone speaks.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import webrtcvad

from alx.providers.errors import ProviderError


# 16 kHz, 16-bit, mono is what the browser worklet already produces and what
# WebRTC accepts without resampling. A frame is a fixed byte count at that
# rate; WebRTC permits 10, 20 or 30 ms and nothing else.
FRAME_MILLISECONDS = 20
BYTES_PER_SAMPLE = 2

# Measured against synthesised room tone and keyboard impulses at 16 kHz:
# fan noise and typing never produced more than four consecutive positive
# frames, while voiced speech produced an unbroken run. Six consecutive
# frames is 120 ms -- above the noise ceiling, below a syllable.
ONSET_FRAMES = 6

# One positive frame is enough to keep a turn alive once it has started.
# Ending is governed by the grace period below, not by a single frame.
AGGRESSIVENESS = 2


@dataclass(frozen=True, slots=True)
class SpeechActivityDetector:
    """Frame-level speech detection over raw PCM. Holds no conversation state."""

    sample_rate_hz: int
    aggressiveness: int = AGGRESSIVENESS

    def __post_init__(self) -> None:
        if self.sample_rate_hz not in (8000, 16000, 32000, 48000):
            raise ProviderError("speech_activity", "unsupported_sample_rate")
        if not 0 <= self.aggressiveness <= 3:
            raise ProviderError("speech_activity", "unsupported_aggressiveness")

    @property
    def frame_bytes(self) -> int:
        return (
            self.sample_rate_hz * FRAME_MILLISECONDS // 1000
        ) * BYTES_PER_SAMPLE

    def _detector(self) -> "webrtcvad.Vad":
        return webrtcvad.Vad(self.aggressiveness)

    def frames(self, payload: bytes) -> tuple[bytes, ...]:
        """Split PCM into whole frames, discarding any trailing partial frame."""
        size = self.frame_bytes
        return tuple(
            payload[start : start + size]
            for start in range(0, len(payload) - size + 1, size)
        )


class SpeechGate:
    """Decides which PCM is worth paying to transmit, and when to finalize.

    It has exactly three outcomes per frame, and no vocabulary beyond them:

      DROP      nobody is speaking; the frame is held in the pre-roll only
      SEND      the frame belongs to an utterance in progress
      FINALIZE  the utterance has been quiet long enough to close it out

    Speech resuming during the grace period cancels finalization and continues
    the same utterance, so a pause for thought does not become two turns. The
    grace period is bounded, so a gate that never hears silence still closes.
    """

    def __init__(
        self,
        detector: SpeechActivityDetector,
        preroll_milliseconds: int,
        grace_milliseconds: int,
    ) -> None:
        if preroll_milliseconds <= 0 or grace_milliseconds <= 0:
            raise ProviderError("speech_activity", "invalid_gate_bounds")
        self._detector = detector
        self._vad = detector._detector()
        self._preroll_frames = max(
            1, preroll_milliseconds // FRAME_MILLISECONDS
        )
        self._grace_frames = max(1, grace_milliseconds // FRAME_MILLISECONDS)
        self._preroll: deque[bytes] = deque(maxlen=self._preroll_frames)
        self._speaking = False
        self._onset_run = 0
        self._silent_run = 0
        self._voiced = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def voiced(self) -> bool:
        """Whether the frame just pushed was itself speech.

        Distinct from `speaking`, which stays true through the trailing grace.
        Telemetry needs the narrower fact to report honestly how much of the
        transmitted audio was actually someone talking.
        """
        return self._voiced

    def reset(self) -> None:
        """Return to idle after an utterance is finalized or abandoned."""
        self._preroll.clear()
        self._speaking = False
        self._onset_run = 0
        self._silent_run = 0
        self._voiced = False

    def push(self, frame: bytes) -> tuple[str, tuple[bytes, ...]]:
        """Classify one whole frame and return what should be transmitted."""
        if len(frame) != self._detector.frame_bytes:
            raise ProviderError("speech_activity", "invalid_frame_size")
        voiced = self._vad.is_speech(frame, self._detector.sample_rate_hz)
        self._voiced = voiced
        if not self._speaking:
            # Silence costs nothing and is remembered only long enough to
            # keep the first syllable of whatever comes next.
            self._preroll.append(frame)
            self._onset_run = self._onset_run + 1 if voiced else 0
            if self._onset_run < ONSET_FRAMES:
                return ("drop", ())
            # Onset confirmed. Everything still in the pre-roll precedes the
            # detection and is sent with it, so "Can you check..." keeps its
            # first word.
            self._speaking = True
            self._onset_run = 0
            self._silent_run = 0
            opening = tuple(self._preroll)
            self._preroll.clear()
            return ("send", opening)
        if voiced:
            # Speech resumed. Whatever grace had accumulated is discarded:
            # this is the same utterance continuing, not a new one.
            self._silent_run = 0
            return ("send", (frame,))
        self._silent_run += 1
        if self._silent_run < self._grace_frames:
            # Still inside the grace period. The silence is transmitted on
            # purpose: Cartesia's own endpointer is watching it, and may end
            # the turn before the grace period does.
            return ("send", (frame,))
        return ("finalize", (frame,))
