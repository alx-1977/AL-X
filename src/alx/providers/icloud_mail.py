"""Read and recoverably move iCloud mail through the IMAP protocol."""

from __future__ import annotations

import asyncio
import hashlib
import html
import imaplib
import io
import json
import logging
import mimetypes
import re
import sqlite3
import zipfile
from threading import RLock
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from alx.contracts import (
    BackgroundEvent,
    MailAccessError,
    MailAttachment,
    MailContent,
    MailParticipants,
    MailReference,
    MailSearchCriteria,
    MailSearchResult,
    MailThreading,
)
from alx.contracts.provenance import (
    RetentionPolicy,
    provenance_from_storage,
    provenance_to_storage,
)
from alx.providers.pdf_limits import (
    PDF_DECODED_STREAM_BYTES,
    enforce_pdf_decode_limits,
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


def _has_attachments(item) -> bool:
    """Report explicit MIME attachments without treating an HTML body as one."""
    candidates = item.walk() if item.is_multipart() else (item,)
    return any(
        not part.is_multipart()
        and (
            part.get_content_disposition() == "attachment"
            or part.get_filename() is not None
        )
        for part in candidates
    )


def _attachment_parts(item):
    """Enumerate explicit attachments with stable MIME-walk identifiers."""
    candidates = item.walk() if item.is_multipart() else (item,)
    found = []
    for index, part in enumerate(candidates, start=1):
        if part.is_multipart():
            continue
        if part.get_content_disposition() != "attachment" and part.get_filename() is None:
            continue
        found.append((str(index), part))
    return tuple(found)


def _page_within_bounds(page) -> bool:
    """Measure a page's stored stream without letting pypdf decode it.

    page.get_contents() decodes to answer, so it cannot bound its own decode.
    The indirect object's retained bytes are what arrived on the wire.
    """
    contents = page.get("/Contents")
    items = contents if isinstance(contents, list) else [contents]
    stored = 0
    for item in items:
        if item is None:
            continue
        try:
            obj = item.get_object() if hasattr(item, "get_object") else item
        except Exception:
            return False
        stored += len(getattr(obj, "_data", b"") or b"")
        if stored > _PAGE_STORED_BYTES:
            return False
    return True


def _attachment_text(media_type: str, payload: bytes, charset: str | None) -> str:
    """Extract domain text without granting the adapter accounting authority.

    Every attachment is untrusted input, so the size limit applies to all of
    them rather than only to PDFs: a 24MB CSV was previously decoded and
    returned whole.
    """
    if len(payload) > _DOCUMENT_BYTES:
        return ""
    if media_type.startswith("text/") or media_type in (
        "application/csv",
        "application/json",
        "application/xml",
    ):
        return payload.decode(charset or "utf-8", errors="replace")[
            :_DOCUMENT_TEXT_CHARACTERS
        ]
    if media_type == "application/pdf":
        if not payload.startswith(b"%PDF-"):
            return ""
        if len(payload) > _PDF_BYTES:
            return ""
        try:
            from pypdf import PdfReader

            enforce_pdf_decode_limits()
            reader = PdfReader(io.BytesIO(payload), strict=True)
            if len(reader.pages) > _DOCUMENT_PAGES:
                return ""
            # Every PDF attachment reaches this path, not only a DHL one, and
            # a page's content stream is decoded before any text appears. A
            # page that is large on the wire is refused before that work.
            extracted: list[str] = []
            characters = 0
            decoded_bytes = 0
            for page in reader.pages:
                if not _page_within_bounds(page):
                    return ""
                contents = page.get_contents()
                if contents is not None:
                    decoded_bytes += len(contents.get_data())
                    if decoded_bytes > _DOCUMENT_DECODED_CONTENT_BYTES:
                        return ""
                text = page.extract_text()
                if not text:
                    continue
                characters += len(text)
                if characters > _DOCUMENT_TEXT_CHARACTERS:
                    return ""
                extracted.append(text)
            return "\n".join(extracted)
        except Exception:
            return ""
    return ""


def _mail_attachment(attachment_id: str, part) -> tuple[MailAttachment, bytes]:
    payload = part.get_payload(decode=True) or b""
    media_type = part.get_content_type() or "application/octet-stream"
    filename = _decoded(part.get_filename()) or f"attachment-{attachment_id}"
    return (
        MailAttachment(
            attachment_id,
            filename,
            media_type,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            "",
        ),
        payload,
    )


# A mail attachment is untrusted input, and its text is extracted before any
# capability sees it, so the limits belong here rather than in one consumer.
# Bound the transport size before parsing.  This does not bound decompression:
# a tiny stored stream can have an extreme expansion ratio, so pdf_limits also
# caps pypdf's decoded output.  The retained V1 worksheets are about 1.5KB; a
# scanned multi-page invoice is a few MB.
_DOCUMENT_BYTES = 25 * 1024 * 1024
_PDF_BYTES = 8 * 1024 * 1024
# Refuse an unusually large stored page before decoding it.  This is only a
# fast preflight; the independent decoded-output ceiling is the bomb defence.
_PAGE_STORED_BYTES = 512 * 1024
_DOCUMENT_PAGES = 200
_DOCUMENT_TEXT_CHARACTERS = 2_000_000
_DOCUMENT_DECODED_CONTENT_BYTES = PDF_DECODED_STREAM_BYTES


_ARCHIVE_MEMBER_LIMIT = 100
_ARCHIVE_MEMBER_BYTES = 25 * 1024 * 1024
_ARCHIVE_TOTAL_BYTES = 50 * 1024 * 1024
_ARCHIVE_DEPTH_LIMIT = 4


def _message_attachments(item) -> tuple[tuple[MailAttachment, bytes], ...]:
    """Expose bounded nested ZIP members as stable transient attachments."""
    found: list[tuple[MailAttachment, bytes]] = []
    expanded_members = 0
    expanded_bytes = 0

    def expand(attachment_id: str, payload: bytes, depth: int) -> None:
        nonlocal expanded_members, expanded_bytes
        if depth > _ARCHIVE_DEPTH_LIMIT:
            raise MailAccessError("archive_unsafe")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = [member for member in archive.infolist() if not member.is_dir()]
                if (
                    expanded_members + len(members) > _ARCHIVE_MEMBER_LIMIT
                    or any(
                        member.file_size > _ARCHIVE_MEMBER_BYTES
                        or member.flag_bits & 1
                        for member in members
                    )
                    or expanded_bytes + sum(member.file_size for member in members)
                    > _ARCHIVE_TOTAL_BYTES
                ):
                    raise MailAccessError("archive_unsafe")
                for index, member in enumerate(members, start=1):
                    content = archive.read(member)
                    expanded_members += 1
                    expanded_bytes += len(content)
                    filename = Path(member.filename).name
                    if not filename:
                        continue
                    member_id = f"{attachment_id}:{index}"
                    media_type = (
                        mimetypes.guess_type(filename)[0]
                        or "application/octet-stream"
                    )
                    found.append(
                        (
                            MailAttachment(
                                member_id,
                                filename,
                                media_type,
                                len(content),
                                hashlib.sha256(content).hexdigest(),
                                "",
                            ),
                            content,
                        )
                    )
                    if zipfile.is_zipfile(io.BytesIO(content)):
                        expand(member_id, content, depth + 1)
        except MailAccessError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            raise MailAccessError("archive_unsafe") from None

    for attachment_id, part in _attachment_parts(item):
        outer, payload = _mail_attachment(attachment_id, part)
        found.append((outer, payload))
        if outer.media_type not in ("application/zip", "application/x-zip-compressed") \
                and not zipfile.is_zipfile(io.BytesIO(payload)):
            continue
        expand(attachment_id, payload, 1)
    return tuple(found)


# RFC 5322 message identifiers are angle-addr tokens.
_MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")


def _identifier(value: str | None) -> str:
    """Return the first RFC 5322 message identifier in a header."""
    found = _MESSAGE_ID_PATTERN.findall(str(value or ""))
    return found[0] if found else ""


def _identifiers(value: str | None) -> tuple[str, ...]:
    """Return every message identifier in a header, order preserved."""
    seen: list[str] = []
    for item in _MESSAGE_ID_PATTERN.findall(str(value or "")):
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _address_list(item, name: str) -> tuple[str, ...]:
    """Return the decoded addresses on one header, order preserved."""
    values = item.get_all(name)
    if not values:
        return ()
    found: list[str] = []
    for _display, address in getaddresses([str(v) for v in values]):
        if address and address not in found:
            found.append(address)
    return tuple(found)


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
    """Persist only IMAP references, headers, and presentation state—never bodies.

    One concurrent state machine. The process poller writes from its worker
    thread while a voice session reads context, delivers and acknowledges from
    another, over one shared connection, so every transition states the value
    it expects to replace and is applied under `_lock`. A transition that
    matches nothing has been overtaken; it is re-read rather than forced,
    because the later state is the true one.

        pending ──current()──> current ──record_delivery()──> presented
           │                      │                              │
           │                      └───────── acknowledge() ──────>│
           └──────────────── acknowledge() ─────────────────> done <┘
           └── reconcile(), never exposed ──────────────────> done

    Two orthogonal facts travel beside `state` and never move backwards:

    `context_exposed`  0 until the observation is put in front of the Core as
                       waiting context, 1 afterwards. It records only that she
                       was shown it, never that she said anything about it.

    `reported_vanished` 0 not vanished, 1 found gone and awaiting delivery,
                       2 carried to her. Durable so a disappearance found with
                       no session open survives until one returns.
    """

    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        # Serializes every transition of this store across the poller thread
        # and the Core-turn thread. Deliberately not the Core-turn lock: a
        # mechanical store write must never wait on a reasoning turn, nor a
        # reasoning turn on an IMAP round trip.
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS mail_cursor "
                "(mailbox_id TEXT PRIMARY KEY, uid_validity TEXT NOT NULL, last_uid INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS mail_observations "
                "(mailbox_id TEXT NOT NULL, uid_validity TEXT NOT NULL, uid INTEGER NOT NULL, "
                "event_json TEXT NOT NULL, state TEXT NOT NULL, content_origins TEXT, "
                "content_recorded_at TEXT, content_expires_at TEXT, mail_references TEXT, "
                "reported_vanished INTEGER NOT NULL DEFAULT 0, "
                "context_exposed INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(mailbox_id, uid_validity, uid))"
            )
            columns = {
                item[1]
                for item in self._connection.execute(
                    "PRAGMA table_info(mail_observations)"
                )
            }
            for column in (
                "content_origins",
                "content_recorded_at",
                "content_expires_at",
                "mail_references",
            ):
                if column not in columns:
                    self._connection.execute(
                        f'ALTER TABLE mail_observations ADD COLUMN "{column}" TEXT'
                    )
            # Records that an announced disappearance has been reported to AL/X
            # once. It is not a presentation state: the observation keeps its
            # own state until she releases it.
            if "reported_vanished" not in columns:
                self._connection.execute(
                    "ALTER TABLE mail_observations "
                    "ADD COLUMN reported_vanished INTEGER NOT NULL DEFAULT 0"
                )
            # Records that the Core was shown this observation as waiting
            # context. Existing rows default to 0: nothing before this column
            # existed was shown as waiting, because the context did not exist.
            if "context_exposed" not in columns:
                self._connection.execute(
                    "ALTER TABLE mail_observations "
                    "ADD COLUMN context_exposed INTEGER NOT NULL DEFAULT 0"
                )

    def close(self) -> None:
        self._connection.close()

    def new_identifiers(
        self,
        mailbox_id: str,
        uid_validity: str,
        identifiers: tuple[int, ...],
    ) -> tuple[int, ...]:
        with self._lock:
            row = self._connection.execute(
                "SELECT uid_validity, last_uid FROM mail_cursor WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            if row is None or row[0] != uid_validity:
                # A new mailbox generation: every stored identifier belongs to
                # the old one and means nothing now.
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM mail_observations WHERE mailbox_id = ?",
                        (mailbox_id,),
                    )
                    self._connection.execute(
                        "INSERT OR REPLACE INTO mail_cursor"
                        "(mailbox_id, uid_validity, last_uid) VALUES (?, ?, ?)",
                        (mailbox_id, uid_validity, max(identifiers, default=0)),
                    )
                return ()
            last_uid = int(row[1])
        return tuple(uid for uid in identifiers if uid > last_uid)

    def discover(
        self,
        mailbox_id: str,
        uid_validity: str,
        found: tuple[tuple[int, dict[str, str]], ...],
        attempted: tuple[int, ...] = (),
    ) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT uid_validity, last_uid FROM mail_cursor WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            if row is None or row[0] != uid_validity:
                raise MailAccessError("cursor_unavailable")
            last_uid = int(row[1])
            self._record_discovered(
                mailbox_id, uid_validity, found, attempted, last_uid
            )

    def _record_discovered(
        self, mailbox_id, uid_validity, found, attempted, last_uid
    ) -> None:
        # The cursor advances across the identifiers this scan actually tried,
        # stopping before the first that failed so it is retried next time.
        # IMAP identifiers increase but need not be contiguous, so a permanent
        # gap left by a deleted message must not stall the cursor forever.
        recorded = {uid for uid, _ in found}
        considered = sorted(
            uid for uid in (attempted or recorded) if uid > last_uid
        )
        highest = last_uid
        for uid in considered:
            if uid not in recorded:
                break
            highest = uid
        with self._connection:
            for uid, event_data in found:
                if uid <= last_uid:
                    continue
                reference = MailReference(mailbox_id, uid_validity, str(uid))
                observed_at = datetime.fromisoformat(event_data["observed_at"])
                provenance = provenance_to_storage(
                    RetentionPolicy().direct_mail(observed_at, (reference,))
                )
                self._connection.execute(
                    "INSERT OR IGNORE INTO mail_observations"
                    "(mailbox_id, uid_validity, uid, event_json, state, "
                    "content_origins, content_recorded_at, content_expires_at, "
                    "mail_references) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        mailbox_id,
                        uid_validity,
                        uid,
                        json.dumps(event_data, separators=(",", ":")),
                        *provenance,
                    ),
                )
            if highest > last_uid:
                self._connection.execute(
                    "UPDATE mail_cursor SET last_uid = ? WHERE mailbox_id = ?",
                    (highest, mailbox_id),
                )

    def reconcile(
        self,
        mailbox_id: str,
        uid_validity: str,
        present: tuple[int, ...],
    ) -> int:
        """Settle observations whose message has left the mailbox.

        Whether a tracked identifier is still in the mailbox has one correct
        answer, so Law 2 places the detection here. What its disappearance
        means does not, so this decides nothing about it.

        A `pending` observation the Core has never been shown was never put in
        front of Friedl: nothing is owed, so it is settled silently.

        Anything the Core has seen -- a `current` or `presented` observation,
        or a `pending` one already shown as waiting context -- is hers to
        account for. Releasing it quietly would leave her unable to explain
        something she may have raised, so it is queued as a fact for her to
        reason about. She releases it herself; this does not.

        The branch is decided by durable mechanical facts, `state` and
        `context_exposed`, never by reading who sent the message or what it
        says.

        Only identifiers at or below the cursor are considered. A higher one has
        not been scanned yet and its absence carries no meaning.

        Detection is durable and separate from delivery. Scanning runs for the
        life of the process, while the transport that carries a fact to AL/X
        comes and goes, so a disappearance found with nobody connected is
        recorded here and delivered later by `pending_vanished`. Returning it
        only in memory would lose it exactly when no session was open.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT uid_validity, last_uid FROM mail_cursor WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            if row is None or row[0] != uid_validity:
                return 0
            last_uid = int(row[1])
            live = set(present)
            rows = self._connection.execute(
                "SELECT uid, state, context_exposed FROM mail_observations "
                "WHERE mailbox_id = ? AND uid_validity = ? "
                "AND state IN ('pending', 'current', 'presented') "
                "AND COALESCE(reported_vanished, 0) = ? ORDER BY uid",
                (mailbox_id, uid_validity, self._NOT_VANISHED),
            ).fetchall()
            announced = 0
            for uid, state, exposed in rows:
                if int(uid) > last_uid or int(uid) in live:
                    continue
                # A pending observation the Core has never been shown was
                # never put in front of Friedl, so nothing is owed and it is
                # settled. Once it has been shown as waiting, its
                # disappearance is hers to account for exactly as an announced
                # one is. Nothing here reads the subject, the sender or the
                # body to decide which.
                if state == "pending" and not exposed:
                    if self._settle_silently(mailbox_id, uid_validity, int(uid)):
                        continue
                    # Overtaken between the read and the write: it has been
                    # promoted or released since. The later state is the true
                    # one, so it is left for the next scan rather than forced.
                    continue
                if self._mark_vanished(mailbox_id, uid_validity, int(uid), state):
                    announced += 1
            return announced

    def _settle_silently(
        self, mailbox_id: str, uid_validity: str, uid: int
    ) -> bool:
        """Complete an unexposed pending observation. False when overtaken."""
        with self._connection:
            return bool(
                self._connection.execute(
                    "UPDATE mail_observations SET state = 'done', event_json = ? "
                    "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ? "
                    "AND state = 'pending' AND context_exposed = 0",
                    (
                        json.dumps(
                            {
                                "mailbox_id": mailbox_id,
                                "uid_validity": uid_validity,
                                "uid": str(uid),
                            },
                            separators=(",", ":"),
                        ),
                        mailbox_id,
                        uid_validity,
                        uid,
                    ),
                ).rowcount
            )

    def _mark_vanished(
        self, mailbox_id: str, uid_validity: str, uid: int, state: str
    ) -> bool:
        """Queue one disappearance for AL/X. False when overtaken.

        The row keeps its own state: this records only that it was found gone
        and is awaiting delivery. A concurrent promotion is harmless, because
        the disappearance is queued rather than the state overwritten -- but
        the observed state is still required to match, so a row released to
        `done` meanwhile is not resurrected.
        """
        with self._connection:
            return bool(
                self._connection.execute(
                    "UPDATE mail_observations SET reported_vanished = ? "
                    "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ? "
                    "AND state = ? AND COALESCE(reported_vanished, 0) = ?",
                    (
                        self._VANISHED_UNDELIVERED,
                        mailbox_id,
                        uid_validity,
                        uid,
                        state,
                        self._NOT_VANISHED,
                    ),
                ).rowcount
            )

    # Detection states for `reported_vanished`. Delivery is recorded separately
    # from detection so that finding a message gone while nobody is connected
    # still reaches AL/X when a transport returns.
    _NOT_VANISHED = 0
    _VANISHED_UNDELIVERED = 1
    _VANISHED_DELIVERED = 2

    def pending_vanished(self) -> tuple[BackgroundEvent, ...]:
        """Disappearances found but not yet carried to AL/X.

        Every state reconciliation can queue one from is included. A `pending`
        observation qualifies once it has been exposed as waiting context, and
        omitting it here would strand the row: queued for ever and delivered
        never.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT mailbox_id, uid_validity, uid, event_json, content_origins, "
                "content_recorded_at, content_expires_at, mail_references "
                "FROM mail_observations WHERE reported_vanished = ? "
                "AND state IN ('pending', 'current', 'presented') ORDER BY uid",
                (self._VANISHED_UNDELIVERED,),
            ).fetchall()
        return tuple(self._vanished_event(row) for row in rows)

    def record_vanished_delivery(self, event_id: str) -> bool:
        """Mark one vanished fact as carried. True when it moved.

        False means it had already been delivered or the observation was
        released meanwhile, which is the same benign race `record_delivery`
        tolerates rather than raises.
        """
        parts = event_id.split(":")
        if (
            len(parts) != 4
            or parts[0] != "mail"
            or not parts[2].isdigit()
            or parts[3] != "vanished"
        ):
            raise MailAccessError("observation_unavailable")
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE mail_observations SET reported_vanished = ? "
                "WHERE uid_validity = ? AND uid = ? AND reported_vanished = ?",
                (
                    self._VANISHED_DELIVERED,
                    parts[1],
                    int(parts[2]),
                    self._VANISHED_UNDELIVERED,
                ),
            ).rowcount
        return bool(updated)

    @staticmethod
    def _vanished_event(row) -> BackgroundEvent:
        """The same observation, reported as gone rather than as arrived."""
        data = dict(json.loads(row[3]))
        return BackgroundEvent(
            f"mail:{row[1]}:{row[2]}:vanished",
            "mail.message_vanished",
            datetime.now(UTC),
            data,
            provenance=provenance_from_storage(*row[4:8]),
        )

    def current(self) -> BackgroundEvent | None:
        """Return the one observation eligible for delivery.

        A successfully delivered observation remains `presented` until the
        Core releases it through a structured acknowledgement or Trash action.
        While it is presented, later pending observations stay queued and do
        not enter the conversation behind Friedl's back.
        """
        with self._lock:
            presented = self._connection.execute(
                "SELECT 1 FROM mail_observations WHERE state = 'presented' LIMIT 1"
            ).fetchone()
            if presented is not None:
                return None
            row = self._connection.execute(
                "SELECT mailbox_id, uid_validity, uid, event_json, content_origins, "
                "content_recorded_at, content_expires_at, mail_references FROM mail_observations "
                "WHERE state = 'current' ORDER BY uid LIMIT 1"
            ).fetchone()
            if row is None:
                row = self._connection.execute(
                    "SELECT mailbox_id, uid_validity, uid, event_json, content_origins, "
                    "content_recorded_at, content_expires_at, mail_references FROM mail_observations "
                    "WHERE state = 'pending' ORDER BY uid LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                with self._connection:
                    promoted = self._connection.execute(
                        "UPDATE mail_observations SET state = 'current' "
                        "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ? "
                        "AND state = 'pending'",
                        row[:3],
                    ).rowcount
                if not promoted:
                    # Settled or promoted between the read and the write.
                    # Nothing is eligible on this pass; the next one re-reads.
                    return None
        return self._event(row)

    @staticmethod
    def _event(row) -> BackgroundEvent:
        data = json.loads(row[3])
        return BackgroundEvent(
            f"mail:{row[1]}:{row[2]}",
            "mail.message_arrived",
            datetime.fromisoformat(data["observed_at"]),
            data,
            provenance=provenance_from_storage(*row[4:]),
        )

    # One new observation is announced at a time. The bound also keeps context
    # safe if legacy state contains more than one already-presented observation.
    CONTEXTUAL_EVENT_LIMIT = 5
    # Mail arrives in bursts, and a burst is mostly receipts and notifications.
    # Announcing each in turn would spend a reasoning call and a spoken
    # interruption on every one. So AL/X is shown what is waiting as well as
    # what she is holding, and judges in a single turn what is worth saying --
    # possibly nothing. Deciding that here, by sender or subject, would be
    # exactly the routing Law 1 forbids.
    WAITING_EVENT_LIMIT = 10

    def contextual_events(self) -> tuple[BackgroundEvent, ...]:
        """Report what she is holding, then what is waiting behind it.

        Waiting observations are context, not announcements: they stay
        `pending` and none of them becomes current merely by being seen. They
        already carry mail provenance and its expiry, so showing them retains
        nothing new.

        The waiting window is the *oldest* pending observations, because that
        is the order delivery will present them in. Showing the newest instead
        would let a burst of recent mail hide the older items that are actually
        next, and she would be judging a queue she is not going to be given.

        Being shown one is recorded durably: from here on its disappearance is
        hers to account for, not something reconciliation may settle quietly.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT mailbox_id, uid_validity, uid, event_json, content_origins, "
                "content_recorded_at, content_expires_at, mail_references FROM mail_observations "
                "WHERE state IN ('current', 'presented') ORDER BY uid DESC LIMIT ?",
                (self.CONTEXTUAL_EVENT_LIMIT,),
            ).fetchall()
            waiting = self._connection.execute(
                "SELECT mailbox_id, uid_validity, uid, event_json, content_origins, "
                "content_recorded_at, content_expires_at, mail_references FROM mail_observations "
                "WHERE state = 'pending' ORDER BY uid LIMIT ?",
                (self.WAITING_EVENT_LIMIT,),
            ).fetchall()
            if waiting:
                with self._connection:
                    for row in waiting:
                        self._connection.execute(
                            "UPDATE mail_observations SET context_exposed = 1 "
                            "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ?",
                            (row[0], row[1], int(row[2])),
                        )
        return (
            *(self._event(row) for row in rows),
            *(self._waiting_event(row) for row in waiting),
        )

    @staticmethod
    def _waiting_event(row) -> BackgroundEvent:
        """A queued observation, marked as not yet raised with Friedl."""
        return BackgroundEvent(
            f"mail:{row[1]}:{row[2]}:waiting",
            "mail.message_waiting",
            datetime.fromisoformat(json.loads(row[3])["observed_at"]),
            json.loads(row[3]),
            provenance=provenance_from_storage(*row[4:]),
        )

    def record_delivery(self, event_id: str) -> bool:
        """Mark a delivered observation `presented`. True when it moved.

        False means the observation was no longer `current` and there was
        nothing to record: AL/X may have acknowledged or trashed it during the
        very turn that announced it, or a later scan reconciled it away. That
        is a benign race, not a fault -- the announcement already reached
        Friedl -- so it is reported rather than raised. It killed two live
        voice sessions on 2026-09-04 when raised.

        A vanished report announces no mail, so it presents nothing and the
        observation keeps its own state until AL/X releases it. Its delivery is
        still recorded, because the fact is durable and would otherwise be
        carried again by the next session.

        A malformed identifier is still an error, because nothing can be
        recorded for it and the caller passed something impossible.
        """
        parts = event_id.split(":")
        if len(parts) == 4 and parts[0] == "mail" and parts[3] == "vanished":
            self.record_vanished_delivery(event_id)
            return False
        if len(parts) != 3 or parts[0] != "mail" or not parts[2].isdigit():
            raise MailAccessError("observation_unavailable")
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE mail_observations SET state = 'presented' "
                "WHERE uid_validity = ? AND uid = ? AND state = 'current'",
                (parts[1], int(parts[2])),
            ).rowcount
        return bool(updated)

    def acknowledge(self, reference: MailReference) -> None:
        minimal = json.dumps(
            {
                "mailbox_id": reference.mailbox_id,
                "uid_validity": reference.uid_validity,
                "uid": reference.uid,
            },
            separators=(",", ":"),
        )
        # Releasing is hers, so any live state may be released and only an
        # already-settled or absent row is refused. The predicate keeps a
        # `done` row from being rewritten by a repeated release.
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE mail_observations SET state = 'done', event_json = ? "
                "WHERE mailbox_id = ? AND uid_validity = ? AND uid = ? "
                "AND state IN ('pending', 'current', 'presented')",
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
        """Observe the mailbox once, mechanically.

        Discovery, cursor advance and reconciliation only. Nothing here decides
        whether mail matters or reaches AL/X; what it finds is written to
        durable state and carried to her separately by `events`.
        """
        connection = self._open()
        try:
            status, _ = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            validity = _uid_validity(connection)
            status, values = connection.uid("search", None, "ALL")
            if status != "OK":
                raise MailAccessError("search_failed")
            present = tuple(
                int(value) for value in (values[0] or b"").split() if value.isdigit()
            )
            # Reconcile before narrowing: what is still in the mailbox is known
            # only from the full listing, and an observation Friedl handled
            # himself is settled from the same evidence that finds new mail.
            self._observations.reconcile("INBOX", validity, present)
            identifiers = self._observations.new_identifiers(
                "INBOX", validity, present
            )
            found: list[tuple[int, dict[str, str]]] = []
            for uid in identifiers:
                status, fetched = connection.uid(
                    "fetch",
                    str(uid),
                    "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT FROM TO CC REPLY-TO DATE)])",
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
            self._observations.discover(
                "INBOX", validity, tuple(found), tuple(identifiers)
            )
        finally:
            self._close(connection)

    async def events(self):
        """Carry durable observations to AL/X. This does not scan.

        Scanning owns the mailbox and runs for the life of the process; this
        owns delivery and lives only as long as a transport that can carry a
        fact to her. Separating them means mail found while nobody was
        connected is still waiting here when a session returns, and that a
        poll cycle costs nothing merely because it ran.
        """
        emitted_event_id: str | None = None
        while True:
            # Found gone while nobody was connected, or during this session.
            # Reported before any new arrival so AL/X settles what she already
            # told Friedl about before taking on the next thing.
            for event in await asyncio.to_thread(self._observations.pending_vanished):
                yield event
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
                    transient_data = {
                        "body": content.body,
                        "has_attachments": content.has_attachments,
                    }
                except MailAccessError as error:
                    transient_data = {"content_unavailable": error.code}
                yield BackgroundEvent(
                    current.event_id,
                    current.kind,
                    current.occurred_at,
                    current.data,
                    transient_data,
                    current.provenance,
                )
            await asyncio.sleep(self._poll_seconds)

    def _read_parsed(self, reference: MailReference):
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
            return BytesParser(policy=policy.default).parsebytes(_payload(values))
        finally:
            self._close(connection)

    @staticmethod
    def _search_text(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _seen(values) -> bool:
        for value in values or ():
            metadata = value[0] if isinstance(value, tuple) and value else value
            if isinstance(metadata, bytes) and b"\\Seen" in metadata:
                return True
        return False

    @staticmethod
    def _bodystructure_has_attachments(values) -> bool:
        metadata: list[bytes] = []
        for value in values or ():
            candidate = value[0] if isinstance(value, tuple) and value else value
            if isinstance(candidate, bytes):
                metadata.append(candidate)
        joined = b" ".join(metadata).upper()
        marker = joined.find(b"BODYSTRUCTURE")
        if marker < 0:
            raise MailAccessError("search_failed")
        structure = joined[marker:]
        # MIME filename parameters and attachment dispositions are the same
        # facts used by _has_attachments, without downloading part payloads.
        return any(
            token in structure
            for token in (b'"ATTACHMENT"', b'"FILENAME"', b'"NAME"')
        )

    def search(
        self, criteria: MailSearchCriteria
    ) -> tuple[tuple[MailSearchResult, ...], bool]:
        """Search stable message identifiers without changing mail state."""
        connection = self._open()
        try:
            status, _ = connection.select(
                self._quoted(criteria.mailbox_id), readonly=True
            )
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            validity = _uid_validity(connection)
            terms: list[str] = []
            if criteria.sender:
                terms.extend(("FROM", self._search_text(criteria.sender)))
            if criteria.subject:
                terms.extend(("SUBJECT", self._search_text(criteria.subject)))
            if criteria.date_from:
                terms.extend(
                    ("SINCE", date.fromisoformat(criteria.date_from).strftime("%d-%b-%Y"))
                )
            if criteria.date_to:
                exclusive = date.fromisoformat(criteria.date_to) + timedelta(days=1)
                terms.extend(("BEFORE", exclusive.strftime("%d-%b-%Y")))
            if criteria.seen_state != "any":
                terms.append(criteria.seen_state.upper())
            status, values = connection.uid("search", None, *(terms or ("ALL",)))
            if status != "OK":
                raise MailAccessError("search_failed")
            identifiers = tuple(
                reversed(
                    tuple(
                        value.decode("ascii")
                        for value in (values[0] or b"").split()
                        if value.isdigit()
                    )
                )
            )
            found: list[MailSearchResult] = []
            incomplete = False
            scan_limit = min(1000, max(criteria.limit * 10, 100))
            for index, uid in enumerate(identifiers):
                if index >= scan_limit:
                    incomplete = True
                    break
                status, fetched = connection.uid(
                    "fetch", uid, "(BODY.PEEK[HEADER] BODYSTRUCTURE FLAGS)"
                )
                if status != "OK":
                    incomplete = True
                    continue
                parsed = BytesParser(policy=policy.default).parsebytes(_payload(fetched))
                has_attachments = self._bodystructure_has_attachments(fetched)
                if (
                    criteria.has_attachments is not None
                    and has_attachments is not criteria.has_attachments
                ):
                    continue
                found.append(
                    MailSearchResult(
                        MailReference(criteria.mailbox_id, validity, uid),
                        _decoded(parsed.get("Subject")),
                        _decoded(parsed.get("From")),
                        str(parsed.get("Date", "")),
                        has_attachments,
                        self._seen(fetched),
                    )
                )
                if len(found) >= criteria.limit:
                    if index + 1 < len(identifiers):
                        incomplete = True
                    break
            return tuple(found), incomplete
        finally:
            self._close(connection)

    def read(self, reference: MailReference) -> MailContent:
        parsed = self._read_parsed(reference)
        return MailContent(
                reference,
                _decoded(parsed.get("Subject")),
                _decoded(parsed.get("From")),
                str(parsed.get("Date", "")),
                _plain_body(parsed),
                MailParticipants(
                    sender=_decoded(parsed.get("From")),
                    reply_to=_decoded(parsed.get("Reply-To")),
                    recipients=_address_list(parsed, "To"),
                    carbon_copy=_address_list(parsed, "Cc"),
                ),
                MailThreading(
                    message_id=_identifier(parsed.get("Message-ID")),
                    in_reply_to=_identifier(parsed.get("In-Reply-To")),
                    references=_identifiers(parsed.get("References")),
                ),
                _has_attachments(parsed),
            )

    def list_attachments(self, reference: MailReference) -> tuple[MailAttachment, ...]:
        parsed = self._read_parsed(reference)
        return tuple(
            attachment
            for attachment, _payload in _message_attachments(parsed)
        )

    def read_attachment(
        self, reference: MailReference, attachment_id: str
    ) -> tuple[MailAttachment, bytes]:
        if not isinstance(attachment_id, str) or not attachment_id.strip():
            raise MailAccessError("attachment_unavailable")
        parsed = self._read_parsed(reference)
        for attachment, payload in _message_attachments(parsed):
            if attachment.attachment_id == attachment_id:
                return (
                    MailAttachment(
                        attachment.attachment_id,
                        attachment.filename,
                        attachment.media_type,
                        attachment.size,
                        attachment.sha256,
                        _attachment_text(attachment.media_type, payload, None),
                    ),
                    payload,
                )
        raise MailAccessError("attachment_unavailable")

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

    def file_message(self, reference: MailReference, mailbox: str) -> str:
        """Move one message to a named mailbox, releasing mail attention.

        Filing a processed invoice is not deletion: the message stays in the
        account, and the mailbox is configured rather than chosen by AL/X.
        """
        if not isinstance(mailbox, str) or not mailbox.strip():
            raise MailAccessError("mailbox_unavailable")
        connection = self._open()
        try:
            status, _ = connection.select(
                self._quoted(reference.mailbox_id), readonly=False
            )
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            if _uid_validity(connection) != reference.uid_validity:
                raise MailAccessError("identifier_stale")
            status, _ = connection.uid("MOVE", reference.uid, self._quoted(mailbox))
            if status != "OK":
                raise MailAccessError("move_failed")
            self._observations.acknowledge(reference)
            return mailbox
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

    def mark_seen(self, reference: MailReference) -> None:
        """Set only the standard IMAP Seen flag on one stable message reference."""
        connection = self._open()
        try:
            status, _ = connection.select(
                self._quoted(reference.mailbox_id), readonly=False
            )
            if status != "OK":
                raise MailAccessError("mailbox_unavailable")
            if _uid_validity(connection) != reference.uid_validity:
                raise MailAccessError("identifier_stale")
            status, _ = connection.uid(
                "STORE", reference.uid, "+FLAGS.SILENT", r"(\Seen)"
            )
            if status != "OK":
                raise MailAccessError("flag_update_failed")
        finally:
            self._close(connection)

    def acknowledge(self, reference: MailReference) -> None:
        self._observations.acknowledge(reference)

    def record_delivery(self, event_id: str) -> bool:
        return self._observations.record_delivery(event_id)

    def contextual_events(self) -> tuple[BackgroundEvent, ...]:
        return self._observations.contextual_events()
