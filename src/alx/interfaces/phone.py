"""Friedl's phone: talking to AL/X by iMessage when he is not at his screen (D-049).

Two directions, both mechanical:

- What he sends from his phone becomes a person turn in one conversation of
  its own, the phone thread, exactly as a typed line does in the browser, and
  her answer goes back to his phone.
- What she would say to him while he is away (D-048) is sent to his phone
  instead of waiting for his next session, and is copied into the phone
  thread so that his answer there is an answer to something it holds.
  Whatever she says in the phone thread goes to his phone, wherever he is.

Which words, and whether to say anything, are hers. This only carries them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from alx.contracts.phone import IncomingMessage, MessagesUnavailable

LOGGER = logging.getLogger(__name__)

# The phone thread: one conversation for everything said by iMessage.
PHONE_CONVERSATION_ID = "phone"
DEFAULT_POLL_SECONDS = 10


class PhoneLink:
    def __init__(
        self,
        channel: Any,
        # The voice session, whose remote_turn makes his message a person
        # turn exactly as a typed line is made one.
        session: Any,
        cursor_path: Path,
        presence: Callable[[], tuple[str, datetime | None]],
        poll_seconds: int = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._channel = channel
        self._session = session
        self._cursor_path = Path(cursor_path)
        self._presence = presence
        self._poll_seconds = poll_seconds
        self._last_failure = ""
        # Where an answer goes when his phone cannot be reached: the ordinary
        # delivery (his screen, or kept for his next session). Set once the
        # server exists.
        self.fallback: Callable[[str], Any] | None = None

    # ---- out ---------------------------------------------------------------

    def carries(self, conversation_id: str) -> bool:
        """Whether a reply made in this conversation goes to his phone now."""
        if conversation_id == PHONE_CONVERSATION_ID:
            return True
        try:
            return self._presence()[0] == "away"
        except Exception as error:  # noqa: BLE001 - unknown: the screen path
            LOGGER.warning("Presence unreadable: %s", type(error).__name__)
            return False

    def send(self, text: str) -> bool:
        """Send to his phone; False when it did not go (the caller keeps it)."""
        try:
            self._channel.send(text)
        except (MessagesUnavailable, ValueError) as error:
            self._note(getattr(error, "code", "send_failed"))
            return False
        self._last_failure = ""
        return True

    # ---- in ----------------------------------------------------------------

    def _cursor(self) -> int | None:
        try:
            return int(json.loads(self._cursor_path.read_text())["row"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_cursor(self, row: int) -> None:
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cursor_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"row": row}))
        temporary.replace(self._cursor_path)

    def _note(self, code: str) -> None:
        """Log a failure once, not on every poll."""
        if code != self._last_failure:
            LOGGER.warning("Phone link: %s", code)
            self._last_failure = code

    async def poll_once(self) -> int:
        """Take what Friedl sent since last time; how many messages were taken.

        On first start it begins from now: what was in Messages before AL/X
        could read it is not answered. The cursor moves past a message only
        once its turn is held, so a stop while it waits for the Core loses
        nothing; the turn's identity comes from the message, so one offered
        again after a stop is taken once.
        """
        cursor = self._cursor()
        try:
            if cursor is None:
                self._save_cursor(await asyncio.to_thread(self._channel.latest_row))
                return 0
            arrived = await asyncio.to_thread(self._channel.received_after, cursor)
        except MessagesUnavailable as error:
            self._note(error.code)
            return 0
        self._last_failure = ""
        for message in arrived:
            await self._answer(message)
            self._save_cursor(message.row)
        return len(arrived)

    async def _answer(self, message: IncomingMessage) -> None:
        outcome = await self._session.remote_turn(
            PHONE_CONVERSATION_ID, message.text, turn_id=f"imessage-{message.row}")
        if outcome is None:
            return  # already taken before a stop
        response = getattr(outcome, "response", None)
        if not response:
            return
        if await asyncio.to_thread(self.send, response):
            return
        if self.fallback is not None:
            await asyncio.to_thread(self.fallback, response)

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the link keeps going
                LOGGER.warning("Phone link poll failed: %s", type(error).__name__)
            await asyncio.sleep(self._poll_seconds)


__all__ = ["DEFAULT_POLL_SECONDS", "PHONE_CONVERSATION_ID", "PhoneLink"]
