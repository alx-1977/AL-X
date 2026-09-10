"""Path bound for one assigned coding worktree.

The coding session edits the worktree with its own native tools, so this is no
longer the site that performs each read and write. What remains is the path
arithmetic AL/X still needs on its own side of the boundary: validating a
plan's inspection targets, listing the root to ground planning, and
normalising blocked paths into the deny entries the sandbox is built from.

Enforcement during the session itself belongs to the kernel, in
`alx.providers.coding_sandbox`. Keeping these checks here as well is not a
second execution path: nothing in this module can open a file the session
edits.
"""

from __future__ import annotations

import os
from pathlib import Path

from alx.contracts.coding import (
    MAX_REPORTED_FILES,
    CodingError,
    lexical_worktree_path,
    path_matches_blocked,
)


_BLOCKED_ENV_NAMES = frozenset({".env", ".env.local", ".env.secret"})


def diagnose_worktree(worktree: str) -> dict[str, object] | None:
    """Why a worktree string cannot be used. None if it can.

    Reports only bounded path state. It does not list directory contents.
    """
    if not isinstance(worktree, str) or not worktree.strip():
        return {
            "reason_code": "blank",
            "detail": "worktree must be a non-blank path",
        }
    try:
        root = Path(worktree).expanduser().resolve()
    except OSError:
        return {
            "reason_code": "unresolvable",
            "detail": "worktree could not be resolved",
            **_safe_received(worktree),
        }
    if not root.exists():
        return {
            "reason_code": "missing",
            "detail": "worktree path does not exist",
            "resolved": _safe_resolved(root),
            **_safe_received(worktree),
        }
    if not root.is_dir():
        return {
            "reason_code": "not_directory",
            "detail": "worktree is not a directory",
            "resolved": _safe_resolved(root),
            **_safe_received(worktree),
        }
    if not os.access(root, os.R_OK | os.X_OK):
        return {
            "reason_code": "permission_denied",
            "detail": "worktree is not readable",
            "resolved": _safe_resolved(root),
            **_safe_received(worktree),
        }
    return None


def _safe_received(worktree: str) -> dict[str, object]:
    if len(worktree) <= 80:
        return {"received": worktree}
    return {"received_length": len(worktree)}


def _safe_resolved(path: Path) -> str:
    text = str(path)
    if len(text) > 240:
        return text[:240]
    return text


class CodingWorkspace:
    """One assigned directory. Every path is checked against it."""

    def __init__(
        self,
        worktree: str,
        blocked_paths: tuple[str, ...] = (),
    ) -> None:
        failure = diagnose_worktree(worktree)
        if failure is not None:
            raise CodingError("worktree_unusable", **failure)
        self.root = Path(worktree).expanduser().resolve()
        self.blocked_paths = self._normalize_blocked(blocked_paths)

    def _normalize_blocked(self, blocked_paths: tuple[str, ...]) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for item in blocked_paths:
            lexical = lexical_worktree_path(item)
            keys = {lexical}
            candidate = self.root.joinpath(*lexical.split("/")) if lexical else self.root
            resolved = candidate.resolve()
            try:
                resolved_relative = resolved.relative_to(self.root).as_posix()
            except ValueError as error:
                raise CodingError("path_outside_worktree") from error
            if resolved_relative != ".":
                keys.add(resolved_relative)
            for key in keys:
                folded = key.casefold()
                if folded not in seen:
                    seen.add(folded)
                    names.append(key)
        return tuple(names)

    def resolve(self, relative: str) -> Path:
        """Return a child of the worktree, or refuse."""
        lexical = lexical_worktree_path(relative)
        candidate = self.root.joinpath(*lexical.split("/")) if lexical else self.root
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise CodingError("path_outside_worktree") from error
        return resolved

    def relative_of(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _blocked_name(self, name: str) -> bool:
        folded = name.casefold()
        if folded == ".git":
            return True
        if folded in _BLOCKED_ENV_NAMES or folded.startswith(".env."):
            return True
        return False

    def _blocked_path(self, path: Path) -> bool:
        relative = self.relative_of(path)
        return any(self._blocked_name(part) for part in Path(relative).parts)

    def _scope_blocked(self, relative: str, resolved: Path) -> bool:
        lexical = lexical_worktree_path(relative)
        resolved_relative = self.relative_of(resolved)
        return path_matches_blocked(
            lexical, self.blocked_paths
        ) or path_matches_blocked(resolved_relative, self.blocked_paths)

    def _refuse_if_blocked(self, relative: str, resolved: Path) -> None:
        if self._blocked_path(resolved) or self._scope_blocked(relative, resolved):
            raise CodingError("path_not_permitted")

    def list_dir(self, relative: str) -> tuple[str, ...]:
        requested = relative or "."
        path = self.resolve(requested)
        self._refuse_if_blocked(requested, path)
        if not path.is_dir():
            raise CodingError("path_outside_worktree")
        names = []
        for child in sorted(path.iterdir()):
            try:
                child_relative = self.relative_of(child)
            except ValueError:
                continue
            if self._blocked_path(child) or path_matches_blocked(
                child_relative, self.blocked_paths
            ):
                continue
            names.append(child_relative)
            if len(names) >= MAX_REPORTED_FILES:
                break
        return tuple(names)

    def validate_inspection_target(self, relative: str) -> str:
        """Validate a plan's proposed inspection path without reading it."""
        path = self.resolve(relative)
        self._refuse_if_blocked(relative, path)
        return self.relative_of(path)
