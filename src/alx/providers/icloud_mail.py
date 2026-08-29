"""Read and recoverably move iCloud mail through the IMAP protocol."""

from __future__ import annotations

import asyncio
import html
import imaplib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from alx.contracts import (
    BackgroundEvent,
    MailAccessError,
    MailContent,
    MailReference,
)


ConnectionFactory = Callable[..., Any]
LOGGER = logging.getLogger(__name__)


def _decoded(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _plain_body(item) -> str:
    candidates = item.walk() if item.is_multipart() else (item,)
    plain: list[str] = []
    rich: list[str] = []
    for part in candidates:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        media_type = part.get_content_type()
        if media_type not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        if media_type == "text/plain":
            plain.append(content)
        else:
            without_tags = re.sub(r"<[^>]+>", " ", content)
            rich.append(html.unescape(without_tags))
    selected = "\n".join(plain or rich)
    return "\n".join(line.strip() for line in selected.splitlines() if line.strip())


def _uid_validity(connection) -> str:
    _, values = connection.response("UIDVALIDITY")
    if not values or values[0] is None:
        raise MailAccessError("uidvalidity_unavailable")
    value = values[0].decode("ascii", errors="strict")
    return value.split()[-1]


def _payload(values) -> bytes:
    for value in values or ():
        if isinstance(value, tuple) and len(value) > 1 and isinstance(value[1], bytes):
            return value[1]
    raise MailAccessError("message_unavailable")


class SQLiteMailObservationState:
    """Persist only IMAP references, headers, and presentation state—never bodies."""

    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS mail_cursor "
                "(mailbox_id TEXT PRIMARY KEY, uid_validity TEXT NOT NULL, last_uid INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS mail_observations "
                "(mailbox_id TEXT NOT NULL, uid_validity TEXT NOT NULL, uid INTEGER NOT NULL, "
                "event_json TEXT NOT NULL, state TEXT NOT NULL, "
                "PRIMARY KEY(mailbox_id, uid_validity, uid))"
            )

    def close(self) -> None:
        self._connection.close()

    def new_identifiers(
        self,
        mailbox_id: str,
        uid_validity: str,
        identifiers: tuple[int, ...],
    ) -> tuple[int, ...]:
        row = self._connection.execute(
            "SELECT uid_validity, last_uid FROM mail_cursor WHERE mailbox_id = ?",
            (mailbox_id,),
        ).fetchone()
        highest = max(identifiers, default=0)
        with self._connection:
            if row is None or row[0] != uid_validity:
                self._connection.execute(
                    "DELETE FROM mail_observations WHERE mailbox_id = ?",
                    (mailbox_id,),
                )
                self._connection.execute(
                    "INSERT OR REPLACE INTO mail_cursor(mailbox_id, uid_validity, last_uid) VALUES (?, ?, ?)",
                    (mailbox_id, uid_validity, highest),
                )
                return ()
        return tuple(uid for uid in identifiers if uid > int(row[1]))

    def discover(
        self,
        mailbox_id: str,
        uid_validity: str,
        found: tuple[tuple[int, dict[str, str]], ...],
    ) -> None:
        row = self._connection.execute(
            "SELECT uid_validity, last_uid FROM mail_cursor WHERE mailbox_id = ?",
            (mailbox_id,),
        ).fetchone()
        if row is None or row[0] != uid_validity:
            raise MailAccessError("cursor_unavailable")
        last_uid = int(row[1])
        highest = max((uid for uid, _ in found), default=last_uid)
        with self._connection:
            for uid, event_data in found:
                if uid <= last_uid:
                    continue
                self._connection.execute(
                    "INSERT OR IGNORE INTO mail_observations"
                    "(mailbox_id, uid_validity, uid, event_json, state) VALUES (?, ?, ?, ?, 'pending')",
                    (mailbox_id, uid_validity, uid, json.dumps(event_data, separators=(",", ":"))),
                )
            if highest > last_uid:
                self._connection.execute(
                    "UPDATE mail_cursor SET last_uid = ? WHERE mailbox_id = ?",
                    (highest, mailbox_id),
                )

    def current(self) -> BackgroundEvent | None:
        row = self._connection.execute(
            "SELECT mailbox_id, uid_validity, uid, event_json FROM mail_observations "
            "WHERE state = 'current' ORDER BY uid LIMIT 1"
        ).fetchone()
        if row is None:
            if self._connection.execute(
                "SELECT 1 FROM mail_observations WHERE state = 'presented' LIMIT 1"
            ).fetchone():
                return None
            row = self._connection.execute(
                "SELECT mailbox_id, uid_validity, uid, event_json FROM mail_observations "
                "WHERE state = 'pending' ORDER BY uid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            with self._connection:
                self._connection.execute(
                    "UPDATE mail_observations SET state = 'current' "
                    "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ?",
                    row[:3],
                )
        return self._event(row)

    @staticmethod
    def _event(row) -> BackgroundEvent:
        data = json.loads(row[3])
        return BackgroundEvent(
            f"mail:{row[1]}:{row[2]}",
            "mail.message_arrived",
            datetime.fromisoformat(data["observed_at"]),
            data,
        )

    def contextual_events(self) -> tuple[BackgroundEvent, ...]:
        row = self._connection.execute(
            "SELECT mailbox_id, uid_validity, uid, event_json FROM mail_observations "
            "WHERE state IN ('current', 'presented') ORDER BY uid LIMIT 1"
        ).fetchone()
        return () if row is None else (self._event(row),)

    def record_delivery(self, event_id: str) -> None:
        parts = event_id.split(":")
        if len(parts) != 3 or parts[0] != "mail" or not parts[2].isdigit():
            raise MailAccessError("observation_unavailable")
        with self._connection:
            updated = self._connection.execute(
                "UPDATE mail_observations SET state = 'presented' "
                "WHERE uid_validity = ? AND uid = ? AND state = 'current'",
                (parts[1], int(parts[2])),
            ).rowcount
        if not updated:
            raise MailAccessError("observation_unavailable")

    def acknowledge(self, reference: MailReference) -> None:
        minimal = json.dumps(
            {
                "mailbox_id": reference.mailbox_id,
                "uid_validity": reference.uid_validity,
                "uid": reference.uid,
            },
            separators=(",", ":"),
        )
        with self._connection:
            updated = self._connection.execute(
                "UPDATE mail_observations SET state = 'done', event_json = ? "
                "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ?",
                (minimal, reference.mailbox_id, reference.uid_validity, int(reference.uid)),
            ).rowcount
        if not updated:
            raise MailAccessError("observation_unavailable")


class ICloudMailAdapter:
    """One account adapter; it contains no conversational or sequencing authority."""

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        secret: str,
        observations: SQLiteMailObservationState,
        poll_seconds: int,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._address = address
        self._secret = secret
        self._observations = observations
        self._poll_seconds = poll_seconds
        self._connection_factory = connection_factory or imaplib.IMAP4_SSL

    def _open(self):
        try:
            connection = self._connection_factory(
                self._host, self._port, timeout=self._poll_seconds
            )
            status, _ = connection.login(self._address, self._secret)
            if status != "OK":
                raise MailAccessError("authentication_failed")
            return connection
        except MailAccessError:
            raise
        except Exception as error:
            raise MailAccessError("connection_failed") from error

    @staticmethod
    def _close(connection) -> None:
        try:
            connection.logout()
        except Exception:
            pass

    def scan(self) -> None:
        connection = self._open()
        try:
            status, _ = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            validity = _uid_validity(connection)
            status, values = connection.uid("search", None, "ALL")
            if status != "OK":
                raise MailAccessError("search_failed")
            identifiers = tuple(
                int(value) for value in (values[0] or b"").split() if value.isdigit()
            )
            identifiers = self._observations.new_identifiers(
                "INBOX", validity, identifiers
            )
            found: list[tuple[int, dict[str, str]]] = []
            for uid in identifiers:
                status, fetched = connection.uid(
                    "fetch",
                    str(uid),
                    "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])",
                )
                if status != "OK":
                    continue
                parsed = BytesParser(policy=policy.default).parsebytes(_payload(fetched))
                found.append((uid, {
                    "mailbox_id": "INBOX",
                    "uid_validity": validity,
                    "uid": str(uid),
                    "message_id": str(parsed.get("Message-ID", "")),
                    "subject": _decoded(parsed.get("Subject")),
                    "sender": _decoded(parsed.get("From")),
                    "received_at": str(parsed.get("Date", "")),
                    "observed_at": datetime.now(UTC).isoformat(),
                }))
            if found:
                self._observations.discover("INBOX", validity, tuple(found))
        finally:
            self._close(connection)

    async def events(self):
        emitted_event_id: str | None = None
        while True:
            try:
                await asyncio.to_thread(self.scan)
            except MailAccessError as error:
                LOGGER.warning("Mail observation unavailable: %s", error.code)
                await asyncio.sleep(self._poll_seconds)
                continue
            current = self._observations.current()
            if current is not None and current.event_id != emitted_event_id:
                emitted_event_id = current.event_id
                reference = MailReference(
                    current.data["mailbox_id"],
                    current.data["uid_validity"],
                    current.data["uid"],
                )
                try:
                    content = await asyncio.to_thread(self.read, reference)
                    transient_data = {"body": content.body}
                except MailAccessError as error:
                    transient_data = {"content_unavailable": error.code}
                yield BackgroundEvent(
                    current.event_id,
                    current.kind,
                    current.occurred_at,
                    current.data,
                    transient_data,
                )
            await asyncio.sleep(self._poll_seconds)

    def read(self, reference: MailReference) -> MailContent:
        connection = self._open()
        try:
            status, _ = connection.select(self._quoted(reference.mailbox_id), readonly=True)
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            if _uid_validity(connection) != reference.uid_validity:
                raise MailAccessError("identifier_stale")
            status, values = connection.uid(
                "fetch", reference.uid, "(BODY.PEEK[])"
            )
            if status != "OK":
                raise MailAccessError("message_unavailable")
            parsed = BytesParser(policy=policy.default).parsebytes(_payload(values))
            return MailContent(
                reference,
                _decoded(parsed.get("Subject")),
                _decoded(parsed.get("From")),
                str(parsed.get("Date", "")),
                _plain_body(parsed),
            )
        finally:
            self._close(connection)

    @staticmethod
    def _quoted(mailbox_id: str) -> str:
        """Quote a mailbox name so names containing spaces remain usable.

        The server-designated Trash mailbox is commonly "Deleted Messages" on
        iCloud. Passed unquoted, IMAP reads the space as an argument separator
        and rejects the command.
        """
        if mailbox_id.startswith('"') and mailbox_id.endswith('"'):
            return mailbox_id
        escaped = mailbox_id.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _trash_mailbox(values) -> str:
        for value in values or ():
            decoded = value.decode("utf-8", errors="replace")
            if "\\Trash" in decoded:
                return decoded.rsplit(' "', 1)[-1].strip('"')
        raise MailAccessError("trash_unavailable")

    def locate_trash_mailbox(self) -> str:
        """Discover the server-designated recoverable Trash mailbox without mutation."""
        connection = self._open()
        try:
            status, values = connection.list()
            if status != "OK":
                raise MailAccessError("trash_unavailable")
            return self._trash_mailbox(values)
        finally:
            self._close(connection)

    def move_to_trash(self, reference: MailReference) -> str:
        connection = self._open()
        try:
            status, _ = connection.select(self._quoted(reference.mailbox_id), readonly=False)
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            if _uid_validity(connection) != reference.uid_validity:
                raise MailAccessError("identifier_stale")
            status, values = connection.list()
            if status != "OK":
                raise MailAccessError("trash_unavailable")
            trash = self._trash_mailbox(values)
            status, _ = connection.uid("MOVE", reference.uid, self._quoted(trash))
            if status != "OK":
                raise MailAccessError("move_failed")
            self._observations.acknowledge(reference)
            return trash
        finally:
            self._close(connection)

    def acknowledge(self, reference: MailReference) -> None:
        self._observations.acknowledge(reference)

    def record_delivery(self, event_id: str) -> None:
        self._observations.record_delivery(event_id)

    def contextual_events(self) -> tuple[BackgroundEvent, ...]:
        return self._observations.contextual_events()
