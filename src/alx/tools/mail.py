"""Language-blind primitive capabilities for one observed mail item."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from email.utils import getaddresses

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    MailAccessError,
    MailAccount,
    MailAttachment,
    MailContent,
    MailObservationControl,
    MailReference,
    MailSearchCriteria,
    MailSendError,
    OutboundReply,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy


READ_MAIL_MESSAGE = "read_mail_message"
SEARCH_MAIL_MESSAGES = "search_mail_messages"
LIST_MAIL_ATTACHMENTS = "list_mail_attachments"
READ_MAIL_ATTACHMENT = "read_mail_attachment"
ACKNOWLEDGE_MAIL_MESSAGE = "acknowledge_mail_message"
MARK_MAIL_MESSAGE_SEEN = "mark_mail_message_seen"
MOVE_MAIL_MESSAGE_TO_TRASH = "move_mail_message_to_trash"
SEND_MAIL_REPLY = "send_mail_reply"

_FAILURES = (
    "arguments_unusable",
    "connection_failed",
    "authentication_failed",
    "mailbox_unavailable",
    "identifier_stale",
    "message_unavailable",
    "search_failed",
    "attachment_unavailable",
    "archive_unsafe",
    "observation_unavailable",
    "trash_unavailable",
    "move_failed",
    "flag_update_failed",
    "recipients_refused",
    "send_rejected",
    "send_outcome_unknown",
)

_STRING = StructuredSchema(ValueKind.STRING)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_REFERENCE_PROPERTIES = {
    "mailbox_id": _STRING,
    "uid_validity": _STRING,
    "uid": _STRING,
}
_REFERENCE = StructuredSchema(
    ValueKind.OBJECT,
    _REFERENCE_PROPERTIES,
    tuple(_REFERENCE_PROPERTIES),
    extra_properties=False,
)


def _input() -> StructuredSchema:
    return StructuredSchema(
        ValueKind.OBJECT,
        _REFERENCE_PROPERTIES,
        tuple(_REFERENCE_PROPERTIES),
        extra_properties=False,
    )


READ_DEFINITION = CapabilityDefinition(
    READ_MAIL_MESSAGE,
    "Retrieve the content of one identified mail item without changing its server state.",
    _input(),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "reference": _REFERENCE,
            "subject": _STRING,
            "sender": _STRING,
            "received_at": _STRING,
            "body": _STRING,
            "reply_to": _STRING,
            "recipients": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "carbon_copy": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "message_id": _STRING,
            "reply_references": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "has_attachments": _BOOLEAN,
        },
        (
            "reference", "subject", "sender", "received_at", "body",
            "recipients", "carbon_copy", "reply_references",
            "has_attachments",
        ),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

_SEARCH_ITEM = StructuredSchema(
    ValueKind.OBJECT,
    {
        "reference": _REFERENCE,
        "subject": _STRING,
        "sender": _STRING,
        "received_at": _STRING,
        "has_attachments": _BOOLEAN,
        "seen": _BOOLEAN,
    },
    (
        "reference",
        "subject",
        "sender",
        "received_at",
        "has_attachments",
        "seen",
    ),
    extra_properties=False,
)

SEARCH_DEFINITION = CapabilityDefinition(
    SEARCH_MAIL_MESSAGES,
    "Search one mailbox with structured sender, subject, date, Seen-state and attachment criteria; return stable message references without changing mail or notification state.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "mailbox_id": _STRING,
            "sender": _STRING,
            "subject": _STRING,
            "date_from": _STRING,
            "date_to": _STRING,
            "seen_state": _STRING,
            "has_attachments": _BOOLEAN,
            "limit": _INTEGER,
        },
        ("mailbox_id",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "messages": StructuredSchema(ValueKind.ARRAY, items=_SEARCH_ITEM),
            "truncated": _BOOLEAN,
        },
        ("messages", "truncated"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

_ATTACHMENT = StructuredSchema(
    ValueKind.OBJECT,
    {
        "attachment_id": _STRING,
        "filename": _STRING,
        "media_type": _STRING,
        "size": _INTEGER,
        "sha256": _STRING,
        "text_available": _BOOLEAN,
    },
    ("attachment_id", "filename", "media_type", "size", "sha256", "text_available"),
    extra_properties=False,
)

LIST_ATTACHMENTS_DEFINITION = CapabilityDefinition(
    LIST_MAIL_ATTACHMENTS,
    "List the explicit MIME attachments on one identified mail item without changing it or returning file content.",
    _input(),
    StructuredSchema(
        ValueKind.OBJECT,
        {"reference": _REFERENCE, "attachments": StructuredSchema(ValueKind.ARRAY, items=_ATTACHMENT)},
        ("reference", "attachments"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

READ_ATTACHMENT_DEFINITION = CapabilityDefinition(
    READ_MAIL_ATTACHMENT,
    "Read provider-extracted text from one exact attachment on an identified mail item without changing the message.",
    StructuredSchema(
        ValueKind.OBJECT,
        {**_REFERENCE_PROPERTIES, "attachment_id": _STRING},
        (*_REFERENCE_PROPERTIES, "attachment_id"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"reference": _REFERENCE, **dict(_ATTACHMENT.properties), "text": _STRING},
        ("reference", *_ATTACHMENT.required, "text"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

ACKNOWLEDGE_DEFINITION = CapabilityDefinition(
    ACKNOWLEDGE_MAIL_MESSAGE,
    "Release one current mail notification after it has been handled, dismissed, or explicitly skipped so a later notification can become current; this changes no mail item or Seen/Unseen state.",
    _input(),
    StructuredSchema(
        ValueKind.OBJECT,
        {"reference": _REFERENCE, "acknowledged": StructuredSchema(ValueKind.BOOLEAN)},
        ("reference", "acknowledged"),
        extra_properties=False,
    ),
    SideEffect.ATTENTION_STATE,
    _FAILURES,
)

MARK_SEEN_DEFINITION = CapabilityDefinition(
    MARK_MAIL_MESSAGE_SEEN,
    "Set the standard Seen flag on one identified mail item without moving, deleting, replying to, or otherwise changing it.",
    _input(),
    StructuredSchema(
        ValueKind.OBJECT,
        {"reference": _REFERENCE, "seen": _BOOLEAN},
        ("reference", "seen"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

TRASH_DEFINITION = CapabilityDefinition(
    MOVE_MAIL_MESSAGE_TO_TRASH,
    "Move one identified mail item to the account's recoverable Trash mailbox.",
    _input(),
    StructuredSchema(
        ValueKind.OBJECT,
        {"reference": _REFERENCE, "trash_mailbox_id": _STRING, "moved": StructuredSchema(ValueKind.BOOLEAN)},
        ("reference", "trash_mailbox_id", "moved"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

_ADDRESS_LIST = StructuredSchema(ValueKind.ARRAY, items=_STRING)

SEND_REPLY_DEFINITION = CapabilityDefinition(
    SEND_MAIL_REPLY,
    (
        "Transmit one prepared reply from the configured mail identity to explicit "
        "recipients, carrying the threading identifiers supplied with it."
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            **_REFERENCE_PROPERTIES,
            "to": _ADDRESS_LIST,
            "carbon_copy": _ADDRESS_LIST,
            "subject": _STRING,
            "body": _STRING,
        },
        (*_REFERENCE_PROPERTIES, "to", "subject", "body"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "transmitted_message_id": _STRING,
            "accepted": StructuredSchema(ValueKind.BOOLEAN),
            "sender_address": _STRING,
            "recipients_accepted": _ADDRESS_LIST,
            "recipients_refused": _ADDRESS_LIST,
            "subject": _STRING,
            "in_reply_to": _STRING,
            "source_has_attachments": _BOOLEAN,
        },
        (
            "transmitted_message_id", "accepted", "sender_address",
            "recipients_accepted", "recipients_refused", "subject",
            "source_has_attachments",
        ),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

DEFINITIONS = (
    SEARCH_DEFINITION,
    READ_DEFINITION,
    LIST_ATTACHMENTS_DEFINITION,
    READ_ATTACHMENT_DEFINITION,
    ACKNOWLEDGE_DEFINITION,
    MARK_SEEN_DEFINITION,
    TRASH_DEFINITION,
)

# Sending is irreversible and configured separately under D-011, so a runtime
# without send credentials can still read mail without being able to transmit.
SEND_DEFINITIONS = (SEND_REPLY_DEFINITION,)


def _addresses(arguments: StructuredData, field_name: str) -> tuple[str, ...]:
    values = arguments.get(field_name)
    if values is None:
        return ()
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{field_name} must be a list of addresses")
    found: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} entries must be addresses")
        found.append(item)
    return tuple(found)


def _outbound_reply(arguments: StructuredData, source: MailContent) -> OutboundReply:
    """Read a structured reply against the message it answers.

    Threading and the permitted recipients come from the source message rather
    than from the arguments, so the reply cannot become new correspondence to
    an address the message never carried.
    """
    subject = arguments.get("subject")
    body = arguments.get("body")
    for name, value in (("subject", subject), ("body", body)):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    observed = [
        address
        for address in (
            source.participants.reply_to,
            source.participants.sender,
            *source.participants.recipients,
            *source.participants.carbon_copy,
        )
        if address and "@" in address
    ]
    return OutboundReply(
        to=_addresses(arguments, "to"),
        subject=subject,
        body=body,
        in_reply_to=source.threading.message_id,
        references=source.threading.reply_references(),
        carbon_copy=_addresses(arguments, "carbon_copy"),
        permitted_recipients=tuple(_plain_addresses(observed)),
    )


def _plain_addresses(values: Sequence[str]) -> tuple[str, ...]:
    """Reduce observed header values to bare addresses."""
    found: list[str] = []
    for _display, address in getaddresses(list(values)):
        if address and address not in found:
            found.append(address)
    return tuple(found)


def _reference(arguments: StructuredData) -> MailReference:
    values: list[str] = []
    for field_name in ("mailbox_id", "uid_validity", "uid"):
        value = arguments.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(field_name)
        values.append(value)
    return MailReference(*values)


def _reference_values(reference: MailReference) -> dict[str, str]:
    return {
        "mailbox_id": reference.mailbox_id,
        "uid_validity": reference.uid_validity,
        "uid": reference.uid,
    }


def _optional_string(arguments: StructuredData, name: str, default: str = "") -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise ValueError(name)
    return value.strip()


def _attachment_values(attachment: MailAttachment) -> dict[str, Any]:
    return {
        "attachment_id": attachment.attachment_id,
        "filename": attachment.filename,
        "media_type": attachment.media_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "text_available": bool(attachment.text),
    }


def build_mail_executors(
    account: MailAccount,
    observations: MailObservationControl,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))
    def failed(capability_id: str, code: str) -> CapabilityResult:
        return CapabilityResult(
            call_id_source(),
            capability_id,
            CapabilityResultState.FAILED,
            failure={"code": code},
        )

    def read(arguments: StructuredData) -> CapabilityResult:
        try:
            reference = _reference(arguments)
            content = account.read(reference)
        except ValueError:
            return failed(READ_MAIL_MESSAGE, "arguments_unusable")
        except MailAccessError as error:
            return failed(READ_MAIL_MESSAGE, error.code)
        # Addresses and identifiers are references, not message content, so they
        # remain durable. The body stays transient.
        durable: dict[str, Any] = {
            "reference": _reference_values(content.reference),
            "subject": content.subject,
            "sender": content.sender,
            "received_at": content.received_at,
            "reply_to": content.participants.reply_to,
            "recipients": tuple(content.participants.recipients),
            "carbon_copy": tuple(content.participants.carbon_copy),
            "message_id": content.threading.message_id,
            "reply_references": content.threading.reply_references(),
            "has_attachments": content.has_attachments,
        }
        return CapabilityResult(
            call_id_source(),
            READ_MAIL_MESSAGE,
            CapabilityResultState.SUCCEEDED,
            {**durable, "body": content.body},
            durable_values=durable,
            provenance=RetentionPolicy().direct_mail(now(), (content.reference,)),
        )

    def search(arguments: StructuredData) -> CapabilityResult:
        try:
            has_attachments = arguments.get("has_attachments")
            if has_attachments is not None and not isinstance(has_attachments, bool):
                raise ValueError("has_attachments")
            limit = arguments.get("limit", 50)
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError("limit")
            criteria = MailSearchCriteria(
                _optional_string(arguments, "mailbox_id"),
                _optional_string(arguments, "sender"),
                _optional_string(arguments, "subject"),
                _optional_string(arguments, "date_from"),
                _optional_string(arguments, "date_to"),
                _optional_string(arguments, "seen_state", "any"),
                has_attachments,
                limit,
            )
            messages, truncated = account.search(criteria)
        except (TypeError, ValueError):
            return failed(SEARCH_MAIL_MESSAGES, "arguments_unusable")
        except MailAccessError as error:
            return failed(SEARCH_MAIL_MESSAGES, error.code)
        values = {
            "messages": tuple(
                {
                    "reference": _reference_values(item.reference),
                    "subject": item.subject,
                    "sender": item.sender,
                    "received_at": item.received_at,
                    "has_attachments": item.has_attachments,
                    "seen": item.seen,
                }
                for item in messages
            ),
            "truncated": truncated,
        }
        references = tuple(item.reference for item in messages)
        return CapabilityResult(
            call_id_source(),
            SEARCH_MAIL_MESSAGES,
            CapabilityResultState.SUCCEEDED,
            values,
            provenance=(
                RetentionPolicy().direct_mail(now(), references)
                if references
                else None
            ),
        )

    def acknowledge(arguments: StructuredData) -> CapabilityResult:
        try:
            reference = _reference(arguments)
            observations.acknowledge(reference)
        except ValueError:
            return failed(ACKNOWLEDGE_MAIL_MESSAGE, "arguments_unusable")
        except MailAccessError as error:
            return failed(ACKNOWLEDGE_MAIL_MESSAGE, error.code)
        return CapabilityResult(
            call_id_source(),
            ACKNOWLEDGE_MAIL_MESSAGE,
            CapabilityResultState.SUCCEEDED,
            {"reference": _reference_values(reference), "acknowledged": True},
        )

    def list_attachments(arguments: StructuredData) -> CapabilityResult:
        try:
            reference = _reference(arguments)
            attachments = account.list_attachments(reference)
        except ValueError:
            return failed(LIST_MAIL_ATTACHMENTS, "arguments_unusable")
        except MailAccessError as error:
            return failed(LIST_MAIL_ATTACHMENTS, error.code)
        values = {
            "reference": _reference_values(reference),
            "attachments": tuple(_attachment_values(item) for item in attachments),
        }
        return CapabilityResult(
            call_id_source(), LIST_MAIL_ATTACHMENTS,
            CapabilityResultState.SUCCEEDED, values,
            provenance=RetentionPolicy().direct_mail(now(), (reference,)),
        )

    def read_attachment(arguments: StructuredData) -> CapabilityResult:
        try:
            reference = _reference(arguments)
            attachment_id = arguments.get("attachment_id")
            if not isinstance(attachment_id, str) or not attachment_id.strip():
                raise ValueError("attachment_id")
            attachment, _payload_bytes = account.read_attachment(reference, attachment_id)
        except ValueError:
            return failed(READ_MAIL_ATTACHMENT, "arguments_unusable")
        except MailAccessError as error:
            return failed(READ_MAIL_ATTACHMENT, error.code)
        durable = {"reference": _reference_values(reference), **_attachment_values(attachment)}
        return CapabilityResult(
            call_id_source(), READ_MAIL_ATTACHMENT,
            CapabilityResultState.SUCCEEDED,
            {**durable, "text": attachment.text},
            durable_values=durable,
            provenance=RetentionPolicy().direct_mail(now(), (reference,)),
        )

    def move_to_trash(arguments: StructuredData) -> CapabilityResult:
        try:
            reference = _reference(arguments)
            destination = account.move_to_trash(reference)
        except ValueError:
            return failed(MOVE_MAIL_MESSAGE_TO_TRASH, "arguments_unusable")
        except MailAccessError as error:
            return failed(MOVE_MAIL_MESSAGE_TO_TRASH, error.code)
        return CapabilityResult(
            call_id_source(),
            MOVE_MAIL_MESSAGE_TO_TRASH,
            CapabilityResultState.SUCCEEDED,
            {
                "reference": _reference_values(reference),
                "trash_mailbox_id": destination,
                "moved": True,
            },
        )

    def mark_seen(arguments: StructuredData) -> CapabilityResult:
        try:
            reference = _reference(arguments)
            account.mark_seen(reference)
        except ValueError:
            return failed(MARK_MAIL_MESSAGE_SEEN, "arguments_unusable")
        except MailAccessError as error:
            return failed(MARK_MAIL_MESSAGE_SEEN, error.code)
        return CapabilityResult(
            call_id_source(),
            MARK_MAIL_MESSAGE_SEEN,
            CapabilityResultState.SUCCEEDED,
            {"reference": _reference_values(reference), "seen": True},
        )

    return {
        SEARCH_MAIL_MESSAGES: search,
        READ_MAIL_MESSAGE: read,
        LIST_MAIL_ATTACHMENTS: list_attachments,
        READ_MAIL_ATTACHMENT: read_attachment,
        ACKNOWLEDGE_MAIL_MESSAGE: acknowledge,
        MARK_MAIL_MESSAGE_SEEN: mark_seen,
        MOVE_MAIL_MESSAGE_TO_TRASH: move_to_trash,
    }


def build_send_executors(
    sender: Any,
    account: MailAccount,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    """Bind the reply primitive to one configured sending identity.

    This is built separately from the observation executors so a runtime can
    read mail without being able to send it.
    """

    def send_reply(arguments: StructuredData) -> CapabilityResult:
        try:
            # The source message is read here so its threading and its observed
            # addresses constrain the reply, rather than the model asserting
            # them. D-011 authorises replying to an existing message only.
            source = account.read(_reference(arguments))
        except (ValueError, MailAccessError) as error:
            return CapabilityResult(
                call_id_source(), SEND_MAIL_REPLY, CapabilityResultState.FAILED,
                failure={"code": getattr(error, "code", "arguments_unusable")},
            )
        try:
            reply = _outbound_reply(arguments, source)
        except ValueError:
            return CapabilityResult(
                call_id_source(),
                SEND_MAIL_REPLY,
                CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        try:
            outcome = sender.send_reply(reply)
        except MailSendError as error:
            return CapabilityResult(
                call_id_source(),
                SEND_MAIL_REPLY,
                CapabilityResultState.FAILED,
                failure={"code": error.code},
            )
        values: dict[str, Any] = {
            "transmitted_message_id": outcome.transmitted_message_id,
            "accepted": outcome.accepted,
            "sender_address": sender.address,
            "recipients_accepted": tuple(outcome.recipients_accepted),
            "recipients_refused": tuple(outcome.recipients_refused),
            "subject": reply.subject,
            "source_has_attachments": source.has_attachments,
        }
        if reply.in_reply_to:
            values["in_reply_to"] = reply.in_reply_to
        # A refused recipient means the message did not reach everyone, so the
        # result is partial rather than a clean success.
        state = (
            CapabilityResultState.PARTIAL
            if outcome.recipients_refused
            else CapabilityResultState.SUCCEEDED
        )
        return CapabilityResult(call_id_source(), SEND_MAIL_REPLY, state, values)

    return {SEND_MAIL_REPLY: send_reply}
