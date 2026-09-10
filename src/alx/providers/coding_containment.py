"""Generate the per-job containment for one native coding-agent session.

The coding model runs as a real agent inside the assigned worktree, so the
authority boundary cannot live in a per-operation Python check any more. It
moves to the kernel: a generated custom sandbox profile whose deny list is
enforced by the operating system against every process the agent starts.

Two properties make this safe to rely on, and both were measured against the
installed CLI before this module existed:

- a *custom* profile refuses to start when it cannot be applied, so a
  malformed deny entry fails the job closed rather than running unenforced.
  A built-in profile warns and continues, which is why one is never used here;
- deny entries are kernel-enforced for read, write and rename, so a denied
  file cannot be read, written, moved elsewhere and then read, or reached
  through a symlink.

Nothing here interprets Friedl or decides what the coding job should do. It
turns an assigned worktree and a blocked-path list into the exact text of a
containment policy.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - reads git metadata locations, never model input
from pathlib import Path

from alx.contracts.coding import CodingError, lexical_worktree_path


PROFILE_NAME = "alx_coding_job"

# Credential shapes that must never be readable by a coding job, independent of
# what Core listed. A job that legitimately edits code has no reason to read a
# private key, and a repository that happens to contain one must not leak it.
CREDENTIAL_DENY_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/credentials.json",
    "**/auth.json",
)

# `*`, `?` and `[` always mean glob to the CLI, and an unsupported metacharacter
# makes it refuse to start. Brace alternation and escapes are not supported, so
# a blocked path carrying one is rejected here with a named reason rather than
# being turned into a profile the CLI will reject with a less useful message.
_UNSUPPORTED_GLOB_CHARACTERS = frozenset({"{", "}", "\\"})


def git_metadata_paths(worktree: Path) -> tuple[str, ...]:
    """Absolute git metadata locations that must be denied for this worktree.

    A normal repository keeps `.git` as a directory. A linked worktree keeps a
    `.git` *file* pointing at the parent repository's git directory, so denying
    only the worktree's own `.git` leaves the real object store readable
    through the pointer's target. Both locations are returned, and the caller
    denies each with the semantics its type requires.
    """
    paths: list[str] = []
    local = worktree / ".git"
    if local.exists() or local.is_symlink():
        paths.append(str(local))
    for option in ("--git-dir", "--git-common-dir"):
        resolved = _git_path(worktree, option)
        if resolved is not None and str(resolved) not in paths:
            paths.append(str(resolved))
    return tuple(paths)


def _git_path(worktree: Path, option: str) -> Path | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, never model input
            ["git", "rev-parse", option],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (worktree / candidate).resolve()
    return candidate


def _quote(value: str) -> str:
    """A TOML basic string. Refuse rather than emit an escape the CLI rejects."""
    if '"' in value or "\\" in value or "\n" in value:
        raise CodingError(
            "sandbox_unusable", reason_code="path_not_representable"
        )
    return f'"{value}"'


def deny_entries(
    worktree: Path, blocked_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Every path and glob the kernel must refuse for this job.

    Blocked paths arrive as worktree-relative names from Core. They are
    anchored to this worktree as absolute paths so a same-named file elsewhere
    on the host is unaffected, and each is denied together with its
    descendants, which the CLI's directory semantics already provide.
    """
    entries: list[str] = list(git_metadata_paths(worktree))
    entries.extend(CREDENTIAL_DENY_GLOBS)
    for item in blocked_paths:
        if any(character in item for character in _UNSUPPORTED_GLOB_CHARACTERS):
            raise CodingError(
                "sandbox_unusable", reason_code="blocked_path_not_expressible"
            )
        lexical = lexical_worktree_path(item)
        if not lexical:
            raise CodingError(
                "sandbox_unusable", reason_code="blocked_path_is_worktree_root"
            )
        entries.append(str(worktree / lexical))
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return tuple(ordered)


def render_profile(worktree: Path, blocked_paths: tuple[str, ...]) -> str:
    """The exact `sandbox.toml` text for one coding job.

    `strict` is the base because it limits reads to the working directory and
    system paths rather than the whole filesystem. The worktree is the only
    granted writable location; `read_only`/`read_write` are literal directory
    grants rather than globs, so nothing else is named there.
    """
    lines = [
        f"[profiles.{PROFILE_NAME}]",
        'extends = "strict"',
        # Ineffective on macOS (Linux-only enforcement). Declared because it is
        # correct on Linux and because the job must not silently depend on it.
        "restrict_network = true",
        f"read_write = [{_quote(str(worktree))}]",
        "deny = [",
    ]
    for entry in deny_entries(worktree, blocked_paths):
        lines.append(f"  {_quote(entry)},")
    lines.append("]")
    return "\n".join(lines) + "\n"


def write_profile(
    grok_home: Path, worktree: Path, blocked_paths: tuple[str, ...]
) -> Path:
    """Install the generated profile into this job's isolated Grok home."""
    path = grok_home / "sandbox.toml"
    path.write_text(render_profile(worktree, blocked_paths), encoding="utf-8")
    return path


__all__ = [
    "CREDENTIAL_DENY_GLOBS",
    "PROFILE_NAME",
    "deny_entries",
    "git_metadata_paths",
    "render_profile",
    "write_profile",
]
