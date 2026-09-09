"""Filesystem bound for one assigned coding worktree.

Paths are resolved and must remain children of the worktree. This is the
authority boundary for repository editing: the job may read and write only
inside the worktree Core named. It cannot grant itself another tree, follow a
symlink out, or write governance or credential files.
"""

from __future__ import annotations

from pathlib import Path

from alx.contracts.coding import (
    MAX_FILE_CHARACTERS,
    MAX_REPORTED_FILES,
    CodingError,
)


_BLOCKED_ENV_NAMES = frozenset({".env", ".env.local", ".env.secret"})


class CodingWorkspace:
    """One assigned directory. Every path is checked against it."""

    def __init__(self, worktree: str) -> None:
        if not isinstance(worktree, str) or not worktree.strip():
            raise CodingError("worktree_unusable")
        root = Path(worktree).expanduser().resolve()
        if not root.is_dir():
            raise CodingError("worktree_unusable")
        self.root = root

    def resolve(self, relative: str) -> Path:
        """Return a child of the worktree, or refuse."""
        if not isinstance(relative, str) or not relative.strip():
            raise CodingError("path_outside_worktree")
        if Path(relative).is_absolute():
            raise CodingError("path_outside_worktree")
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise CodingError("path_outside_worktree") from error
        return candidate

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

    def list_dir(self, relative: str) -> tuple[str, ...]:
        path = self.resolve(relative or ".")
        if not path.is_dir():
            raise CodingError("path_outside_worktree")
        names = []
        for child in sorted(path.iterdir()):
            try:
                names.append(self.relative_of(child))
            except ValueError:
                continue
            if len(names) >= MAX_REPORTED_FILES:
                break
        return tuple(names)

    def read_text(self, relative: str) -> str:
        path = self.resolve(relative)
        if self._blocked_path(path):
            raise CodingError("path_not_permitted")
        if not path.is_file():
            raise CodingError("path_outside_worktree")
        text = path.read_text(encoding="utf-8")
        if len(text) > MAX_FILE_CHARACTERS:
            # Refuse rather than return a prefix the model could write back
            # over the unseen remainder.
            raise CodingError("file_too_large")
        return text

    def write_text(self, relative: str, content: str) -> str:
        if not isinstance(content, str):
            raise CodingError("arguments_unusable")
        if len(content) > MAX_FILE_CHARACTERS:
            raise CodingError("arguments_unusable")
        path = self.resolve(relative)
        if self._blocked_path(path):
            raise CodingError("path_not_permitted")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.resolve().relative_to(self.root)
        except ValueError as error:
            raise CodingError("path_outside_worktree") from error
        path.write_text(content, encoding="utf-8")
        return self.relative_of(path)
