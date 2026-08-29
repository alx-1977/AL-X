"""Compose mail observation and primitives into existing AL/X boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from alx.config import MailSettings
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers import ICloudMailAdapter, SQLiteMailObservationState
from alx.safety import AuthorityPolicy
from alx.tools import (
    ACKNOWLEDGE_MAIL_MESSAGE,
    DEFINITIONS,
    MOVE_MAIL_MESSAGE_TO_TRASH,
    READ_MAIL_MESSAGE,
    build_mail_executors,
)


MAIL_READ_PERMISSION = "mail.read"
MAIL_OBSERVATION_PERMISSION = "mail.observation"
MAIL_TRASH_PERMISSION = "mail.trash"


@dataclass(frozen=True, slots=True)
class MailRuntime:
    source: ICloudMailAdapter
    observations: SQLiteMailObservationState
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


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
