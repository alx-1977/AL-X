from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AudioChunk,
    TranscriptionEvent,
    TranscriptionState,
)


NOW = datetime(2026, 8, 27, tzinfo=UTC)


class SpeechContractTests(unittest.TestCase):
    def test_audio_carries_transport_data_only(self) -> None:
        chunk = AudioChunk("stream-1", 0, b"audio", "audio/pcm", 16_000)
        self.assertEqual(chunk.payload, b"audio")
        self.assertFalse(chunk.final)

    def test_transcription_carries_acoustic_evidence_without_deciding_intent(self) -> None:
        event = TranscriptionEvent(
            "stream-1",
            "event-1",
            TranscriptionState.FINAL,
            "natural speech",
            NOW,
            {"speaker_reference": "friedl", "confidence": 0.91},
        )
        self.assertEqual(event.content, "natural speech")
        self.assertEqual(event.acoustic_metadata["speaker_reference"], "friedl")
        self.assertNotIn("addressed_to_alx", event.acoustic_metadata)
        with self.assertRaises(TypeError):
            event.acoustic_metadata["confidence"] = 1.0

    def test_non_final_empty_audio_and_naive_event_time_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AudioChunk("stream-1", 0, b"", "audio/pcm")
        with self.assertRaises(ValueError):
            TranscriptionEvent(
                "stream-1",
                "event-1",
                TranscriptionState.FINAL,
                "speech",
                datetime(2026, 8, 27),
            )


if __name__ == "__main__":
    unittest.main()
