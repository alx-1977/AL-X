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

import hashlib
import os
import shutil
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

    def walk(self, directory: Path) -> dict[str, tuple[str, int]]:
        """Hash every regular file under `directory`, never following links.

        Returns `{relative path: (sha256, size)}`. Symbolic links are recorded
        by name with an empty digest so that creating one is visible evidence,
        while its target is never opened.
        """
        results: dict[str, tuple[str, int]] = {}
        if not directory.exists():
            return results
        for current, directory_names, file_names in os.walk(directory, followlinks=False):
            # Do not descend into linked directories either.
            directory_names[:] = [
                name for name in directory_names
                if not Path(current, name).is_symlink()
            ]
            for name in sorted(file_names):
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
                if len(results) > MAX_WALKED_FILES:
                    return results
        return results

    @staticmethod
    def total_bytes(walked: dict[str, tuple[str, int]]) -> int:
        """How much a session's state occupies, from a walk already taken."""
        return sum(size for _, size in walked.values())

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
