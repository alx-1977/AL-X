"""Retention and reaping for sandbox workspaces, under D-027.

D-027 is mechanical about this: at the retention limit every experiment-authored
byte is deleted — the source, stdout, stderr, the session working directory and
any retained file content — and only the bounded manifest survives.

Two failure modes are equally wrong, and both are easy to reach by accident.
Deleting a run directory wholesale takes the manifest with it and destroys the
audit. Leaving a run directory intact turns `runs/` into an indefinite archive
of source code and program output. So the deletion is selective: everything in
a run goes except `manifest.json`, and the session state goes entirely.

Nothing here decides when work is finished. It applies an elapsed-time rule to
directories, which is a condition with one objectively correct answer.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from alx.contracts.sandbox import SandboxError
from alx.providers.sandbox_workspace import MANIFEST_NAME, SandboxWorkspace


LOGGER = logging.getLogger(__name__)

# How long a session's working files remain usable for iteration. Long enough
# to return to an experiment after other work; short enough that experiment
# bytes do not accumulate.
DEFAULT_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What one sweep removed. Counts only; never names or content."""

    sessions_purged: int
    entries_removed: int


class SandboxRetention:
    """Applies the D-027 retention rule to a sandbox root."""

    def __init__(
        self,
        workspace: SandboxWorkspace,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._workspace = workspace
        self._ttl = ttl_seconds

    def purge_session(self, experiment_id: str, session_id: str) -> int:
        """Remove one session's experiment-authored bytes, keeping manifests."""
        session = self._workspace.root / experiment_id / session_id
        if not session.is_dir():
            return 0
        return self._workspace.purge_transient(session)

    def sweep(self, now: float | None = None) -> RetentionReport:
        """Purge every session whose last activity is older than the TTL.

        Age is taken from the most recent modification anywhere in the session,
        so a session being actively iterated on is not purged mid-use.
        """
        moment = time.time() if now is None else now
        root = self._workspace.root
        if not root.is_dir():
            return RetentionReport(0, 0)
        sessions = 0
        entries = 0
        for experiment in sorted(self._directories(root)):
            for session in sorted(self._directories(experiment)):
                if self._age(session, moment) < self._ttl:
                    continue
                try:
                    removed = self._workspace.purge_transient(session)
                except SandboxError:
                    LOGGER.warning("Sandbox session could not be purged")
                    continue
                if removed:
                    sessions += 1
                    entries += removed
        if sessions:
            LOGGER.info(
                "Sandbox retention purged %d session(s), %d entries", sessions, entries
            )
        return RetentionReport(sessions, entries)

    @staticmethod
    def _directories(parent: Path) -> list[Path]:
        try:
            return [
                item for item in parent.iterdir()
                if item.is_dir() and not item.is_symlink()
            ]
        except OSError:
            return []

    def _age(self, session: Path, moment: float) -> float:
        """Seconds since anything in this session last changed.

        Symbolic links are never followed: an experiment that linked to a file
        with a future timestamp could otherwise keep its own workspace alive
        indefinitely.
        """
        newest = 0.0
        for current, directory_names, file_names in os.walk(session, followlinks=False):
            directory_names[:] = [
                name for name in directory_names
                if not Path(current, name).is_symlink()
            ]
            for name in (*directory_names, *file_names):
                path = Path(current, name)
                if path.is_symlink():
                    continue
                try:
                    newest = max(newest, path.stat().st_mtime)
                except OSError:
                    continue
        try:
            newest = max(newest, session.stat().st_mtime)
        except OSError:
            return 0.0
        return moment - newest

    def manifests(self, experiment_id: str, session_id: str) -> tuple[Path, ...]:
        """Every surviving run manifest for one session, in run order."""
        runs = self._workspace.root / experiment_id / session_id / "runs"
        if not runs.is_dir():
            return ()
        return tuple(
            sorted(
                item / MANIFEST_NAME
                for item in runs.iterdir()
                if (item / MANIFEST_NAME).is_file()
            )
        )
