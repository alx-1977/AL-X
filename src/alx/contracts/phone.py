"""What passes between AL/X and Friedl's phone (D-049)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class MessagesUnavailable(Exception):
    """Messages could not be read or sent through; `code` says which way."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One message Friedl sent: its row in the database, words and time."""

    row: int
    text: str
    sent_at: datetime


__all__ = ["IncomingMessage", "MessagesUnavailable"]
