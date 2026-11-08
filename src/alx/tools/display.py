"""AL/X shows Friedl something on her screen (D-073).

One capability, `show_on_screen`: a title, an information column and up to two
board views, drawn over the moving wave the reader screen uses. She decides
what to show and what each mark means; the viewer only draws it. Board views
are read from KiCad files, his (read only, through a copy) or her own work
copies, so what he sees is his actual copper.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)

SHOW_ON_SCREEN = "show_on_screen"
TONES = ("ok", "bad", "info")
MAX_ITEMS = 12
MAX_VIEWS = 2
_FAILURES = ("arguments_unusable", "board_unavailable", "path_denied", "section_failed",
             "section_too_large", "engine_unavailable", "unavailable")

_STRING = StructuredSchema(ValueKind.STRING)
_NUMBER = StructuredSchema(ValueKind.NUMBER)
_NAMES = StructuredSchema(ValueKind.ARRAY, items=_STRING)
_POINT = StructuredSchema(ValueKind.ARRAY, items=_NUMBER)
_TONED = StructuredSchema(ValueKind.OBJECT, {"text": _STRING, "tone": _STRING}, ("text",),
                          extra_properties=False)
_ITEM = StructuredSchema(ValueKind.OBJECT, {"title": _STRING, "detail": _STRING, "tone": _STRING},
                         ("title",), extra_properties=False)
_DIM = StructuredSchema(ValueKind.OBJECT, {"from": _POINT, "to": _POINT, "text": _STRING,
                                           "tone": _STRING}, ("from", "to", "text"),
                        extra_properties=False)
_VIEW = StructuredSchema(
    ValueKind.OBJECT,
    {"label": _STRING, "sublabel": _STRING, "tone": _STRING,
     "board": _STRING, "work_copy_id": _STRING, "window": _POINT,
     "refs": _NAMES, "nets": _NAMES, "planes": _NAMES,
     "marks": StructuredSchema(ValueKind.OBJECT), "rotate": StructuredSchema(ValueKind.BOOLEAN),
     "dims": StructuredSchema(ValueKind.ARRAY, items=_DIM)},
    ("label", "window"), extra_properties=False)

DEFINITION = CapabilityDefinition(
    SHOW_ON_SCREEN,
    "Show Friedl something on your screen, over the moving wave: a title, a verdict, a short "
    "column of items and up to two board views side by side (yours/suggested). Say one line "
    "and let the screen carry the detail. Tones: ok (green, correct), bad (red, needs "
    "attention), info (cyan). Lead the items with what is wrong; what is correct gets one "
    "quiet item. A view reads a window [x0, y0, x1, y1] in mm of a KiCad board - board is a "
    "path in his design folders (read through a copy, never changed), work_copy_id one of "
    "your own copies (for a suggested layout). refs are the parts to draw (labelled like "
    "silkscreen), nets the tracks and vias, planes the zone nets to show faintly. marks "
    "colours a part, 'net:<name>' or 'keepout' by tone; unmarked copper is blue. rotate turns "
    "the view 90 degrees. dims draws dimension lines between two points in mm. Showing "
    "replaces what is on screen; he closes it.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"title": _STRING, "subtitle": _STRING, "context": _STRING, "verdict": _TONED,
         "items": StructuredSchema(ValueKind.ARRAY, items=_ITEM), "note": _STRING,
         "said": _STRING, "views": StructuredSchema(ValueKind.ARRAY, items=_VIEW)},
        ("title",), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT, {"shown": StructuredSchema(ValueKind.BOOLEAN),
                           "version": StructuredSchema(ValueKind.INTEGER),
                           "views": StructuredSchema(ValueKind.ARRAY,
                                                     items=StructuredSchema(ValueKind.OBJECT))},
        ("shown", "version"), extra_properties=False),
    SideEffect.NONE,
    _FAILURES,
)
DEFINITIONS = (DEFINITION,)


class DisplayBoard:
    """What is on the viewer now; the screen polls it and opens on a new version."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Each run has its own id, so a screen left open across a restart
        # never mistakes a new display for one it has already seen.
        self._id = uuid.uuid4().hex
        self._version = 0
        self._display: dict[str, Any] | None = None

    def publish(self, display: Mapping[str, Any]) -> int:
        with self._lock:
            self._version += 1
            self._display = dict(display)
            return self._version

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self._id, "version": self._version, "display": self._display}


def _tone(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if value not in TONES:
        raise ValueError(f"tone must be one of {', '.join(TONES)}")
    return value


def _text(value: Any, limit: int, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} is text of at most {limit} characters")
    return value


def build_display_executors(
    board: DisplayBoard,
    read_section: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def failed(code: str, reason: str = "") -> CapabilityResult:
        failure = {"code": code, **({"reason": reason[:300]} if reason else {})}
        return CapabilityResult(call_id_source(), SHOW_ON_SCREEN, CapabilityResultState.FAILED,
                                failure=failure)

    def show(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            display: dict[str, Any] = {
                "title": _text(arguments.get("title"), 60, "title"),
                "subtitle": _text(arguments.get("subtitle"), 40, "subtitle"),
                "context": _text(arguments.get("context"), 80, "context"),
                "note": _text(arguments.get("note"), 400, "note"),
                "said": _text(arguments.get("said"), 300, "said"),
            }
            if not display["title"].strip():
                raise ValueError("title is required")
            verdict = arguments.get("verdict")
            display["verdict"] = None if not verdict else {
                "text": _text(verdict.get("text"), 120, "verdict.text"),
                "tone": _tone(verdict.get("tone"))}
            items = list(arguments.get("items") or [])
            if len(items) > MAX_ITEMS:
                raise ValueError(f"at most {MAX_ITEMS} items")
            display["items"] = [{"title": _text(item.get("title"), 80, "item.title"),
                                 "detail": _text(item.get("detail"), 200, "item.detail"),
                                 "tone": _tone(item.get("tone"))} for item in items]
            views = list(arguments.get("views") or [])
            if len(views) > MAX_VIEWS:
                raise ValueError(f"at most {MAX_VIEWS} views")
            for view in views:
                _text(view.get("label"), 40, "label")
                _text(view.get("sublabel"), 60, "sublabel")
                if bool(view.get("board")) == bool(view.get("work_copy_id")):
                    raise ValueError("each view names a board or a work_copy_id, not both")
                for key, value in dict(view.get("marks") or {}).items():
                    _tone(value)
                for dim in view.get("dims") or ():
                    _tone(dim.get("tone"))
                    if len(dim["from"]) != 2 or len(dim["to"]) != 2:
                        raise ValueError("a dimension runs between two [x, y] points")
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            return failed("arguments_unusable", str(error))
        if views and read_section is None:
            return failed("unavailable", "KiCad is not available to read boards")
        drawn = []
        for view in views:
            try:
                geometry = read_section({key: view.get(key) for key in
                                         ("board", "work_copy_id", "window", "refs", "nets", "planes")})
            except Exception as error:  # noqa: BLE001 - reported as her evidence
                code = getattr(error, "code", None) or (error.args[0] if error.args else "")
                code = code if code in _FAILURES else "section_failed"
                detail = getattr(error, "recovery", None) or {}
                return failed(code, str(detail.get("detail", "") if isinstance(detail, Mapping) else ""))
            drawn.append({
                "label": _text(view.get("label"), 40, "label"),
                "sublabel": _text(view.get("sublabel"), 60, "sublabel"),
                "tone": view.get("tone"), "rotate": bool(view.get("rotate")),
                "marks": dict(view.get("marks") or {}), "dims": list(view.get("dims") or ()),
                "geometry": geometry,
            })
        display["views"] = drawn
        version = board.publish(display)
        return CapabilityResult(
            call_id_source(), SHOW_ON_SCREEN, CapabilityResultState.SUCCEEDED,
            {"shown": True, "version": version,
             "views": [{"label": item["label"],
                        "parts": sorted(part["ref"] for part in item["geometry"].get("parts", ())),
                        "tracks": len(item["geometry"].get("tracks", ()))} for item in drawn]})

    return {SHOW_ON_SCREEN: show}


__all__ = ["DEFINITIONS", "DisplayBoard", "SHOW_ON_SCREEN", "build_display_executors"]
