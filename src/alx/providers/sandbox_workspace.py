"""The D-027 filesystem boundary: where a run may write, and what it changed.

This module decides one mechanical question — which paths belong to a session
— and it decides nothing else. It never reads a file's content, never judges
an experiment, and never executes anything.

Two properties matter more than the individual rules.

Every path is derived from validated identifiers and then resolved and checked
to be a proper child of the sandbox root. A design that trusted the identifier
alone would be one string away from writing outside the workspace.

The hash walk never follows a symbolic link. This is not tidiness: the walk
runs in the parent process, outside the sandbox, with the parent's own
authority. A walk that followed links would read files the confined process is
forbidden to read and publish their hashes as the experiment's own evidence.
A link is recorded as a link, and its target is never opened.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alx.contracts.sandbox import (
    MAX_WALKED_FILES,
    ArtifactMetadata,
    FileChange,
    SandboxError,
    valid_identifier,
)


# Written once per run and never modified. Kept when the transient bytes are
# deleted, which is what lets the audit outlive the content.
MANIFEST_NAME = "manifest.json"

_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class WalkResult:
    """One snapshot of a session's state, and whether it is complete.

    `truncated` is carried rather than inferred: comparing two partial
    snapshots yields artifact counts that look authoritative and are not.
    """

    entries: dict[str, tuple[str, int]]
    truncated: bool

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.entries.values())


@dataclass(frozen=True, slots=True)
class SessionPaths:
    """Where one session's state and one run's evidence live."""

    root: Path
    session_state: Path
    run_directory: Path
    source_directory: Path
    manifest_path: Path


class SandboxWorkspace:
    """Creates, resolves and reaps session directories under one root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self, experiment_id: str, session_id: str, run_id: str) -> SessionPaths:
        """Create the session state and this run's directory, or refuse."""
        try:
            state = self._child(experiment_id, session_id, "state")
            run = self._child(experiment_id, session_id, "runs", run_id)
            source = run / "source"
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            run.mkdir(parents=True, exist_ok=False, mode=0o700)
            source.mkdir(mode=0o700)
        except SandboxError:
            raise
        except OSError as error:
            raise SandboxError("workspace_unavailable") from error
        return SessionPaths(self._root, state, run, source, run / MANIFEST_NAME)

    # Fixed structural segments the layout itself contributes. They are not
    # caller input, and listing them here keeps the identifier rule strict for
    # everything that is.
    _STRUCTURAL = frozenset({"state", "runs"})

    def _child(self, *segments: str) -> Path:
        """Join validated segments and prove the result stays inside the root.

        The identifier pattern already excludes separators and dots, so the
        resolved-path check below is the second defence rather than the only
        one. Both are kept: the pattern could be widened by a future edit and
        this check would still hold.
        """
        for segment in segments:
            if segment not in self._STRUCTURAL and not valid_identifier(segment):
                raise SandboxError("workspace_unavailable")
        candidate = self._root.joinpath(*segments)
        resolved = candidate.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise SandboxError("workspace_unavailable")
        return resolved

    @contextmanager
    def lease(self, experiment_id: str, session_id: str):
        """Hold one session exclusively for the length of a run.

        Two protections in one lock. Overlapping runs in a session would share
        the working-copy filename, so one could overwrite the other's program
        between the copy and the exec and attribute a manifest to the wrong
        source. And retention in another process could delete a session's state
        while that session was mid-run.

        The lock is a file, so it holds across processes rather than only
        across threads.
        """
        session = self._child(experiment_id, session_id)
        session.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (session / ".lease").open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise SandboxError("session_busy") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def is_leased(self, session: Path) -> bool:
        """Whether another process is currently running in this session."""
        marker = session / ".lease"
        if not marker.is_file():
            return False
        try:
            handle = marker.open("a+")
        except OSError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            handle.close()

    def walk(self, directory: Path) -> "WalkResult":
        """Hash every regular file under `directory`, never following links.

        Returns the entries plus whether the snapshot is complete. Symbolic
        links are recorded by name with an empty digest so that creating one is
        visible evidence, while its target is never opened.

        Every entry counts against the bound, links included. Counting only
        regular files let a program create tens of thousands of small links and
        force the privileged parent to enumerate all of them after the run had
        already been stopped.

        Truncation is reported rather than silent: a partial snapshot compared
        against another partial snapshot produces artifact counts that are
        quietly wrong, which is worse than an explicit "incomplete".
        """
        results: dict[str, tuple[str, int]] = {}
        if not directory.exists():
            return WalkResult(results, False)
        for current, directory_names, file_names in os.walk(directory, followlinks=False):
            # Do not descend into linked directories either.
            directory_names[:] = [
                name for name in directory_names
                if not Path(current, name).is_symlink()
            ]
            for name in sorted(file_names):
                if name == ".lease":
                    continue
                if len(results) >= MAX_WALKED_FILES:
                    return WalkResult(results, True)
                path = Path(current, name)
                relative = str(path.relative_to(directory))
                if path.is_symlink():
                    results[relative] = ("", 0)
                    continue
                if not path.is_file():
                    continue
                try:
                    results[relative] = (self._digest(path), path.stat().st_size)
                except OSError as error:
                    raise SandboxError("output_unreadable") from error
        return WalkResult(results, False)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def changes(
        before: dict[str, tuple[str, int]], after: dict[str, tuple[str, int]]
    ) -> tuple[ArtifactMetadata, ...]:
        """The difference between two walks, which is what the run produced."""
        artifacts: list[ArtifactMetadata] = []
        for name in sorted(set(after) - set(before)):
            digest, size = after[name]
            artifacts.append(ArtifactMetadata(name, FileChange.CREATED, size, digest))
        for name in sorted(set(after) & set(before)):
            if after[name] != before[name]:
                digest, size = after[name]
                artifacts.append(
                    ArtifactMetadata(name, FileChange.MODIFIED, size, digest)
                )
        for name in sorted(set(before) - set(after)):
            artifacts.append(ArtifactMetadata(name, FileChange.DELETED, 0, ""))
        return tuple(artifacts)

    def purge_state(self, session_state: Path) -> None:
        """Empty a session's working directory, keeping the directory itself.

        Used when a run overflows the workspace ceiling: the run's evidence is
        already written, and leaving the overflow in place would let one run
        deny the disk to every later one.
        """
        resolved = Path(session_state).resolve()
        if self._root not in resolved.parents:
            raise SandboxError("workspace_unavailable")
        for entry in sorted(resolved.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                self._remove_tree(entry)
            else:
                entry.unlink()

    def purge_transient(self, session_root: Path) -> int:
        """Delete every experiment-authored byte, keeping each run manifest.

        D-027 requires that at the retention limit the source, stdout, stderr,
        session state and any retained file content are removed, and that only
        the bounded manifest survives. Deleting the run directory wholesale
        would take the manifest with it; keeping the directory would leave an
        indefinite archive of source and output. So the deletion is selective.
        """
        resolved = Path(session_root).resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise SandboxError("workspace_unavailable")
        removed = 0
        state = resolved / "state"
        if state.exists():
            self._remove_tree(state)
            removed += 1
        runs = resolved / "runs"
        if runs.is_dir():
            for run in sorted(runs.iterdir()):
                if not run.is_dir() or run.is_symlink():
                    continue
                for entry in sorted(run.iterdir()):
                    if entry.name == MANIFEST_NAME and entry.is_file():
                        continue
                    self._remove_tree(entry) if entry.is_dir() else entry.unlink()
                    removed += 1
        return removed

    def _remove_tree(self, path: Path) -> None:
        """Remove a directory that is provably inside the root, without links.

        `shutil.rmtree` following a symbolic link out of the workspace is how a
        cleanup routine becomes a deletion incident, so the link check comes
        first and the walk itself never follows one.
        """
        resolved = Path(path).resolve()
        if self._root not in resolved.parents:
            raise SandboxError("workspace_unavailable")
        if Path(path).is_symlink():
            Path(path).unlink()
            return
        shutil.rmtree(path, ignore_errors=False)
