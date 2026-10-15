"""iMessage to and from Friedl, through Messages on the Mac AL/X runs on (D-049).

Sending goes through Messages itself (AppleScript, `osascript`), from the
Apple ID signed in there, to the one handle configured: Friedl's. The text is
passed as an argument, never spliced into the script, so nothing in it can
become a command.

Reading is the Messages database, opened read-only. Only messages Friedl sent
from that handle are returned; everything else in it is never read into AL/X.
macOS grants the read only with Full Disk Access for the process running
AL/X; without it reading fails and says so.

This decides nothing: it sends what it is given and returns what arrived.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess  # noqa: S404 - Messages has no other interface
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alx.contracts.phone import IncomingMessage, MessagesUnavailable

LOGGER = logging.getLogger(__name__)

DEFAULT_CHAT_DATABASE = Path.home() / "Library/Messages/chat.db"
SEND_TIMEOUT_SECONDS = 30
# Messages stores times as nanoseconds since 2001-01-01 UTC.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

# The recipient and the text arrive as arguments (`item n of argv`), so the
# script itself never changes.
_SEND_SCRIPT = (
    "on run argv\n"
    'tell application "Messages"\n'
    "set alxService to 1st account whose service type = iMessage\n"
    "send (item 1 of argv) to participant (item 2 of argv) of alxService\n"
    "end tell\n"
    "end run\n"
)


def _attributed_text(blob: bytes | None) -> str:
    """The plain text inside a message's `attributedBody`.

    Recent macOS often leaves `text` empty and keeps the words only in this
    archived NSAttributedString: the bytes after "NSString", a length (one
    byte, or 0x81 and two little-endian bytes), then UTF-8.
    """
    if not blob:
        return ""
    try:
        rest = blob.split(b"NSString", 1)[1][5:]
        if rest[0] == 0x81:
            length, start = int.from_bytes(rest[1:3], "little"), 3
        else:
            length, start = rest[0], 1
        return rest[start:start + length].decode("utf-8", errors="replace")
    except (IndexError, ValueError):
        return ""


class IMessageChannel:
    """Sends to, and reads from, Friedl's one iMessage handle."""

    def __init__(self, handle: str, chat_database: Path = DEFAULT_CHAT_DATABASE,
                 runner=subprocess.run) -> None:
        if not handle.strip():
            raise ValueError("an iMessage handle is required")
        self._handle = handle.strip()
        self._database = Path(chat_database)
        self._runner = runner

    @property
    def handle(self) -> str:
        return self._handle

    def send(self, text: str) -> None:
        """Send one message to Friedl. Raises MessagesUnavailable if it did not go."""
        if not text.strip():
            raise ValueError("an empty message is not sent")
        try:
            completed = self._runner(
                ["osascript", "-e", _SEND_SCRIPT, text, self._handle],
                capture_output=True, text=True, timeout=SEND_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            LOGGER.warning("iMessage not sent: %s", type(error).__name__)
            raise MessagesUnavailable("send_failed") from None
        if completed.returncode != 0:
            # Messages' own error: not signed in, or not allowed to be driven.
            LOGGER.warning("iMessage not sent: osascript exit %s", completed.returncode)
            raise MessagesUnavailable("send_failed")

    def _connect(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(f"file:{self._database}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            raise MessagesUnavailable("read_denied") from None

    def latest_row(self) -> int:
        """The newest message row, so reading can start from now."""
        connection = self._connect()
        try:
            row = connection.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
        except sqlite3.Error:
            raise MessagesUnavailable("read_denied") from None
        finally:
            connection.close()
        return int(row[0])

    def received_after(self, row: int) -> list[IncomingMessage]:
        """Messages Friedl sent from his handle after `row`, oldest first."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT message.ROWID, message.text, message.attributedBody, message.date "
                "FROM message JOIN handle ON message.handle_id = handle.ROWID "
                "WHERE message.ROWID > ? AND message.is_from_me = 0 "
                # One-to-one only: a message of his in a group chat is not
                # one to AL/X.
                "AND message.cache_roomnames IS NULL "
                "AND lower(handle.id) = lower(?) ORDER BY message.ROWID",
                (row, self._handle),
            ).fetchall()
        except sqlite3.Error:
            raise MessagesUnavailable("read_denied") from None
        finally:
            connection.close()
        found = []
        for number, text, body, date in rows:
            words = (text or "").strip() or _attributed_text(body).strip()
            if not words:
                continue  # an attachment or reaction with no words
            sent_at = _APPLE_EPOCH + timedelta(microseconds=int(date or 0) // 1000)
            found.append(IncomingMessage(int(number), words, sent_at))
        return found


__all__ = [
    "DEFAULT_CHAT_DATABASE",
    "IMessageChannel",
    "IncomingMessage",
    "MessagesUnavailable",
]
