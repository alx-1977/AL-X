"""Compose watching for outstanding external work, or leave it absent.

Returning None means nothing watches, which is the honest state when there is
no store or no observer: a watcher that could see nothing would report
"unknown" forever and look like a fault.

Watching is read-only observation of what a service has already published, so
it carries no permission and no capability. It cannot request work, only notice
that work it was told about has finished.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alx.continuity.tasks import SQLiteTaskStore, TaskStoreCorrupt
from alx.contracts.task import ExternalTask
from alx.interfaces.task_poller import TaskPoller
from alx.providers.qodo_status import QodoStatusObserver


LOGGER = logging.getLogger(__name__)

# Often enough that a wait feels watched, rarely enough that it is not a load.
# Read-only GETs against one pull request; nothing here spends anything.
DEFAULT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class TaskRuntime:
    """The store and the watcher, or nothing at all."""

    store: SQLiteTaskStore
    poller: TaskPoller


def build_task_runtime(
    storage_root: Path | None,
    repository: str,
    token: str,
    announce: Callable[[str, str], None],
    completed: Callable[[ExternalTask], None],
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    observers: dict[str, Any] | None = None,
) -> TaskRuntime | None:
    """Compose the task store and its watcher, or leave both absent."""
    if storage_root is None:
        LOGGER.info("No storage root: external work is not watched")
        return None

    try:
        store = SQLiteTaskStore(storage_root / "external-tasks.sqlite3")
    except TaskStoreCorrupt:
        LOGGER.warning("External task store is unusable: work is not watched")
        return None

    if observers is None:
        if not repository.strip() or not token.strip():
            LOGGER.info("No configured observer: external work is not watched")
            return None
        try:
            observers = {"qodo": QodoStatusObserver(repository, token)}
        except ValueError:
            LOGGER.warning("Task observer is misconfigured: work is not watched")
            return None

    LOGGER.info(
        "External task watching enabled: %s", ", ".join(sorted(observers))
    )
    return TaskRuntime(
        store=store,
        poller=TaskPoller(
            store, observers, interval_seconds, announce, completed
        ),
    )
