"""Compose D-075: AL/X looks at pictures and PDF pages herself."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Mapping

from alx.providers.coding_process import run_confined_converter
from alx.providers.look_image import LookError, look
from alx.providers.reference_file import ReferenceError, reference_path
from alx.safety import AuthorityPolicy
from alx.tools.look import DEFINITIONS, LOOK, build_look_executors

ALX_STORAGE = Path("~/Library/Mobile Documents/com~apple~CloudDocs/ALX_Storage")
PERMISSION = "files.look"


@dataclass(frozen=True)
class LookRuntime:
    definitions: tuple
    policies: dict
    executors: dict
    permissions: frozenset
    store: Path


def build_look_runtime(call_id_source, environment: Mapping[str, str], storage_root: Path,
                       design_roots: tuple[Path, ...], runner=None) -> LookRuntime:
    """`look` over his design folders and ALX_Storage; images kept in her storage."""
    # The same storage folder the KiCad tool saves datasheets into (review of #243).
    storage = Path(environment.get("ALX_STORAGE_ROOT", ALX_STORAGE))
    roots = tuple(dict.fromkeys(
        root.expanduser().resolve() for root in (*design_roots, storage)))
    store = storage_root / "looks"
    renderer = Path(environment.get("ALX_PDFTOPPM", "/opt/homebrew/bin/pdftoppm")).expanduser()
    converter = Path(environment.get("ALX_FFMPEG", "/opt/homebrew/bin/ffmpeg")).expanduser()
    execute = partial(run_confined_converter, runner=runner, timeout_seconds=60)

    def look_at(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise LookError("arguments_unusable", "path is a file path")
        try:
            path = reference_path(Path(raw).expanduser(), roots=roots)
        except ReferenceError as error:
            raise LookError("path_denied", "only his design folders and ALX_Storage") from error
        return look(path, arguments, store=store, scratch_root=storage_root / "look-scratch",
                    renderer=renderer, picture_converter=converter, execute=execute)

    return LookRuntime(DEFINITIONS, {LOOK: AuthorityPolicy(frozenset({PERMISSION}))},
                       dict(build_look_executors(look_at, call_id_source)),
                       frozenset({PERMISSION}), store)


__all__ = ["LookRuntime", "build_look_runtime"]
