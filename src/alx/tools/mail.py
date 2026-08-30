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
    MailContent,
    MailObservationControl,
    MailReference,
    MailSendError,
    OutboundReply,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy


READ_MAIL_MESSAGE = "read_mail_message"
ACKNOWLEDGE_MAIL_MESSAGE = "acknowledge_mail_message"
MOVE_MAIL_MESSAGE_TO_TRASH = "move_mail_message_to_trash"
SEND_MAIL_REPLY = "send_mail_reply"

_FAILURES = (
    "arguments_unusable",
    "connection_failed",
    "authentication_failed",
    "mailbox_unavailable",
    "identifier_stale",
    "message_unavailable",
    "observation_unavailable",
    "trash_unavailable",
    "move_failed",
    "recipients_refused",
    "send_rejected",
    "send_outcome_unknown",
)

_STRING = StructuredSchema(ValueKind.STRING)
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
        },
        (
            "reference", "subject", "sender", "received_at", "body",
            "recipients", "carbon_copy", "reply_references",
        ),
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
        },
        (
            "transmitted_message_id", "accepted", "sender_address",
            "recipients_accepted", "recipients_refused", "subject",
        ),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

DEFINITIONS = (READ_DEFINITION, ACKNOWLEDGE_DEFINITION, TRASH_DEFINITION)

# Sending is irreversible and has no recorded production-deployment
# authorisation, so it is defined but deliberately not part of the registered
# set. Composing it into a runtime requires a governance decision first.
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
        }
        return CapabilityResult(
            call_id_source(),
            READ_MAIL_MESSAGE,
            CapabilityResultState.SUCCEEDED,
            {**durable, "body": content.body},
            durable_values=durable,
            provenance=RetentionPolicy().direct_mail(now(), (content.reference,)),
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

    return {
        READ_MAIL_MESSAGE: read,
        ACKNOWLEDGE_MAIL_MESSAGE: acknowledge,
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
