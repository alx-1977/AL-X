"""Compose mail observation and primitives into existing AL/X boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alx.config import MailSendSettings, MailSettings
from alx.contracts import (
    ApprovalScope,
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    GoalState,
    MailAccount,
    StructuredData,
)
from alx.providers import (
    ICloudMailAdapter,
    ICloudMailSender,
    SQLiteMailObservationState,
)
from alx.safety import AuthorityPolicy
from alx.tools import (
    CAPTURE_SUPPLIER_INVOICE,
    ACKNOWLEDGE_MAIL_MESSAGE,
    DEFINITIONS,
    MARK_MAIL_MESSAGE_SEEN,
    LIST_MAIL_ATTACHMENTS,
    FILE_PROCESSED_MAIL_MESSAGE,
    MOVE_MAIL_MESSAGE_TO_TRASH,
    READ_MAIL_MESSAGE,
    READ_MAIL_ATTACHMENT,
    SEARCH_MAIL_MESSAGES,
    SEND_DEFINITIONS,
    SEND_MAIL_REPLY,
    build_mail_executors,
    build_send_executors,
)


MAIL_READ_PERMISSION = "mail.read"
MAIL_OBSERVATION_PERMISSION = "mail.observation"
MAIL_TRASH_PERMISSION = "mail.trash"
MAIL_SEEN_PERMISSION = "mail.seen"
# Sending is a separate authority. Granting reading never grants sending.
MAIL_SEND_PERMISSION = "mail.send"


@dataclass(frozen=True, slots=True)
class MailRuntime:
    source: ICloudMailAdapter
    observations: SQLiteMailObservationState
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def captured_invoice_filing_scopes(
    state: GoalState | None,
) -> tuple[ApprovalScope, ...]:
    """Derive filing authority from a capture that actually completed.

    D-020 authorises filing a processed supplier invoice, not any message. The
    scope names the exact mail item whose capture reported completion, so a
    message AL/X merely looked at cannot be filed, and the evidence comes from
    the capability result rather than from anything the model asserts.
    """
    if state is None:
        return ()
    scopes: list[ApprovalScope] = []
    for attempt in state.attempts:
        call = attempt.call
        result = attempt.result
        if (
            call is None
            or call.capability_id != CAPTURE_SUPPLIER_INVOICE
            or result is None
            or result.state is not CapabilityResultState.SUCCEEDED
            or result.values.get("completed") is not True
        ):
            continue
        reference = {
            name: call.arguments.get(name)
            for name in ("mailbox_id", "uid_validity", "uid")
        }
        if any(not isinstance(value, str) or not value for value in reference.values()):
            continue
        scope = ApprovalScope(FILE_PROCESSED_MAIL_MESSAGE, reference)
        if scope not in scopes:
            scopes.append(scope)
    return tuple(scopes)


def mail_post_reply_standing_scopes(
    state: GoalState | None,
) -> tuple[ApprovalScope, ...]:
    """Derive exact mailbox scopes from confirmed reply evidence.

    The source attachment fact is produced by the send primitive from the
    message it actually answered. Model assertions cannot create these scopes.
    """
    if state is None:
        return ()
    scopes: list[ApprovalScope] = []
    invoked_trash = {
        tuple(attempt.call.arguments.get(name) for name in (
            "mailbox_id", "uid_validity", "uid"
        ))
        for attempt in state.attempts
        if attempt.call is not None
        and attempt.call.capability_id == MOVE_MAIL_MESSAGE_TO_TRASH
        and attempt.implementation_invoked
    }
    for attempt in state.attempts:
        if (
            attempt.call is None
            or attempt.call.capability_id != SEND_MAIL_REPLY
            or attempt.result is None
            or attempt.result.state is not CapabilityResultState.SUCCEEDED
            or attempt.result.values.get("accepted") is not True
            or not isinstance(
                attempt.result.values.get("source_has_attachments"), bool
            )
        ):
            continue
        reference = {
            name: attempt.call.arguments.get(name)
            for name in ("mailbox_id", "uid_validity", "uid")
        }
        if any(not isinstance(value, str) or not value for value in reference.values()):
            continue
        scopes.append(ApprovalScope(MARK_MAIL_MESSAGE_SEEN, reference))
        reference_key = tuple(reference[name] for name in (
            "mailbox_id", "uid_validity", "uid"
        ))
        if (
            attempt.result.values.get("source_has_attachments") is False
            and reference_key not in invoked_trash
        ):
            scopes.append(ApprovalScope(MOVE_MAIL_MESSAGE_TO_TRASH, reference))
    return tuple(scopes)


def build_mail_send_runtime(
    settings: MailSendSettings,
    account: MailAccount,
    call_id_source: Callable[[], str],
) -> tuple[tuple[CapabilityDefinition, ...], Mapping[str, AuthorityPolicy], Mapping[str, Any], frozenset[str]]:
    """Compose the reply capability, authorised by DECISIONS.md D-011.

    Sending is built separately from observation so a runtime can read mail
    without being able to send it. Every send requires its own exactly scoped,
    expiring approval.
    """
    sender = ICloudMailSender(
        settings.smtp_host,
        settings.smtp_port,
        settings.address,
        settings.secret,
        settings.timeout_seconds,
    )
    policies = {
        SEND_MAIL_REPLY: AuthorityPolicy(
            frozenset({MAIL_SEND_PERMISSION}), approval_required=True
        ),
    }
    return (
        SEND_DEFINITIONS,
        policies,
        build_send_executors(sender, account, call_id_source),
        frozenset({MAIL_SEND_PERMISSION}),
    )


def build_mail_runtime(
    settings: MailSettings,
    storage_root: Path,
    call_id_source: Callable[[], str],
) -> MailRuntime:
    observations = SQLiteMailObservationState(storage_root / "mail-observations.sqlite3")
    source = ICloudMailAdapter(
        settings.imap_host,
        settings.imap_port,
        settings.address,
        settings.secret,
        observations,
        settings.poll_seconds,
    )
    policies = {
        SEARCH_MAIL_MESSAGES: AuthorityPolicy(frozenset({MAIL_READ_PERMISSION})),
        READ_MAIL_MESSAGE: AuthorityPolicy(frozenset({MAIL_READ_PERMISSION})),
        LIST_MAIL_ATTACHMENTS: AuthorityPolicy(frozenset({MAIL_READ_PERMISSION})),
        READ_MAIL_ATTACHMENT: AuthorityPolicy(frozenset({MAIL_READ_PERMISSION})),
        ACKNOWLEDGE_MAIL_MESSAGE: AuthorityPolicy(
            frozenset({MAIL_OBSERVATION_PERMISSION})
        ),
        MARK_MAIL_MESSAGE_SEEN: AuthorityPolicy(
            frozenset({MAIL_SEEN_PERMISSION}),
            approval_required=True,
            standing_scope_allowed=True,
        ),
        MOVE_MAIL_MESSAGE_TO_TRASH: AuthorityPolicy(
            frozenset({MAIL_TRASH_PERMISSION}),
            approval_required=True,
            standing_scope_allowed=True,
        ),
        # D-020 authorises filing a *processed* invoice. Granting it outright
        # let any identified message be moved, so authority is instead derived
        # from a capture that actually succeeded on that exact message.
        FILE_PROCESSED_MAIL_MESSAGE: AuthorityPolicy(
            frozenset({MAIL_TRASH_PERMISSION}),
            approval_required=True,
            standing_scope_allowed=True,
        ),
    }
    return MailRuntime(
        source,
        observations,
        DEFINITIONS,
        policies,
        build_mail_executors(
            source,
            source,
            call_id_source,
            processed_mailbox=settings.processed_mailbox,
        ),
        frozenset(
            {
                MAIL_READ_PERMISSION,
                MAIL_OBSERVATION_PERMISSION,
                MAIL_SEEN_PERMISSION,
                MAIL_TRASH_PERMISSION,
            }
        ),
    )
