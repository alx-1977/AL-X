"""Language-blind primitive capabilities for one observed mail item."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    MailAccessError,
    MailAccount,
    MailObservationControl,
    MailReference,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)


READ_MAIL_MESSAGE = "read_mail_message"
ACKNOWLEDGE_MAIL_MESSAGE = "acknowledge_mail_message"
MOVE_MAIL_MESSAGE_TO_TRASH = "move_mail_message_to_trash"

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
        },
        ("reference", "subject", "sender", "received_at", "body"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

ACKNOWLEDGE_DEFINITION = CapabilityDefinition(
    ACKNOWLEDGE_MAIL_MESSAGE,
    "Finish handling one current mail notification locally so a later notification can become current, without changing the mail item itself.",
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

DEFINITIONS = (READ_DEFINITION, ACKNOWLEDGE_DEFINITION, TRASH_DEFINITION)


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
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
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
        durable: dict[str, Any] = {
            "reference": _reference_values(content.reference),
            "subject": content.subject,
            "sender": content.sender,
            "received_at": content.received_at,
        }
        return CapabilityResult(
            call_id_source(),
            READ_MAIL_MESSAGE,
            CapabilityResultState.SUCCEEDED,
            {**durable, "body": content.body},
            durable_values=durable,
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
