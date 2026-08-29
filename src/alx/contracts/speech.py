"""Provider-neutral speech transport contracts with no intent authority."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from alx.contracts.records import StructuredData, freeze_data


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Ordered audio bytes; this record carries no conversational meaning."""

    stream_id: str
    sequence: int
    payload: bytes
    media_type: str
    sample_rate_hz: int | None = None
    final: bool = False

    def __post_init__(self) -> None:
        _required(self.stream_id, "stream_id")
        _required(self.media_type, "media_type")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload must be bytes")
        if not self.payload and not self.final:
            raise ValueError("a non-final audio chunk must contain bytes")
        if self.sample_rate_hz is not None and self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")


class TranscriptionState(str, Enum):
    PARTIAL = "partial"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class TranscriptionEvent:
    """STT output only; consumers may not interpret or route its content."""

    stream_id: str
    event_id: str
    state: TranscriptionState
    content: str
    occurred_at: datetime
    acoustic_metadata: StructuredData = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.stream_id, "stream_id")
        _required(self.event_id, "event_id")
        _required(self.content, "content")
        _aware(self.occurred_at, "occurred_at")
        object.__setattr__(
            self,
            "acoustic_metadata",
            freeze_data(self.acoustic_metadata),
        )


class SpeechTranscriber(Protocol):
    """Replaceable STT port. It returns words and acoustic facts, never intent."""

    def transcribe(
        self,
        chunks: AsyncIterable[AudioChunk],
    ) -> AsyncIterator[TranscriptionEvent]: ...


class SpeechSynthesizer(Protocol):
    """Replaceable TTS port for an already-authoritative AL/X response."""

    def synthesize(
        self,
        response: str,
        correlation_id: str | None = None,
    ) -> AsyncIterator[AudioChunk]: ...
