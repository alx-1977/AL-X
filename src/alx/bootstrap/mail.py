"""Compose mail observation and primitives into existing AL/X boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alx.config import MailSendSettings, MailSettings
from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
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
    ACKNOWLEDGE_MAIL_MESSAGE,
    DEFINITIONS,
    MOVE_MAIL_MESSAGE_TO_TRASH,
    READ_MAIL_MESSAGE,
    SEND_DEFINITIONS,
    SEND_MAIL_REPLY,
    build_mail_executors,
    build_send_executors,
)


MAIL_READ_PERMISSION = "mail.read"
MAIL_OBSERVATION_PERMISSION = "mail.observation"
MAIL_TRASH_PERMISSION = "mail.trash"
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
        READ_MAIL_MESSAGE: AuthorityPolicy(frozenset({MAIL_READ_PERMISSION})),
        ACKNOWLEDGE_MAIL_MESSAGE: AuthorityPolicy(
            frozenset({MAIL_OBSERVATION_PERMISSION})
        ),
        MOVE_MAIL_MESSAGE_TO_TRASH: AuthorityPolicy(
            frozenset({MAIL_TRASH_PERMISSION}), approval_required=True
        ),
    }
    return MailRuntime(
        source,
        observations,
        DEFINITIONS,
        policies,
        build_mail_executors(source, source, call_id_source),
        frozenset(
            {MAIL_READ_PERMISSION, MAIL_OBSERVATION_PERMISSION, MAIL_TRASH_PERMISSION}
        ),
    )
