"""Part facts read once from a datasheet and kept with the FireFli library (27 Nov 2026).

P0009's B-SoM to M-SoM job made 808 calls; 335 were reads, the same M-SoM datasheet
26 times and Particle's pin maps about 60, because nothing learnt was kept from one
call to the next. Here what she finds (each pin's name and function, layout and
design notes) is saved in the library's facts/ folder, one JSON file per part,
every fact naming the document and page it came from, and handed back whenever
that part comes up again. Friedl, 27 Nov: saved without asking him; facts are
notes, never the symbols or footprints.

What a fact says is AL/X's reading (Law 1); keeping, merging and finding it again
is mechanical (Law 2).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

FOLDER = "facts"
MAX_PINS = 400
MAX_NOTES = 40
MAX_ALIASES = 20
MAX_TEXT = 600
# Pins handed over per call, by their text: a 400-pin part comes in pieces (start, next_start).
PIN_PAGE_CHARACTERS = 15_000
MAX_SHORT = 300


class FactsError(ValueError):
    """Facts that cannot be kept as given; the message says why."""


def key_of(name: str) -> str:
    """A part name compared the way people write it: case, dashes and spaces ignored."""
    return re.sub(r"[^0-9a-z]", "", str(name).casefold())


def part_of_document(path: str) -> str:
    """The part a datasheet file names: fetch_datasheet adds -<hash> after the part number."""
    stem = Path(str(path)).stem
    return re.sub(r"-[0-9a-f]{6,}$", "", stem, flags=re.IGNORECASE)


def file_name(part: str) -> str:
    name = re.sub(r"[^0-9A-Za-z._+-]", "_", part.strip()).strip("._")
    if not name:
        raise FactsError("part must name the part (its part number)")
    return name + ".json"


def _entries(library: Path):
    folder = library / FOLDER
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield path, data


def find(library: Path, name: str) -> tuple[Path, dict] | None:
    """The facts held for a part, by its name or any alias, or None."""
    wanted = key_of(name)
    if not wanted:
        return None
    for path, data in _entries(library):
        names = [path.stem, data.get("part", "")] + list(data.get("aliases") or [])
        if wanted in {key_of(item) for item in names if isinstance(item, str)}:
            return path, data
    return None


def held(library: Path) -> dict[str, str]:
    """Every name facts are held under (part and aliases, compared form) -> the part."""
    out: dict[str, str] = {}
    for path, data in _entries(library):
        part = str(data.get("part") or path.stem)
        for item in [path.stem, part] + list(data.get("aliases") or []):
            if isinstance(item, str) and key_of(item):
                out[key_of(item)] = part
    return out


def view(path: Path, data: dict, start: int = 0) -> dict:
    """The facts as she reads them: pins as one line each, keyed by pin number, from pin
    index start for about PIN_PAGE_CHARACTERS (next_start says where the rest begins);
    the notes come with the first piece."""
    ordered = sorted((data.get("pins") or {}).items(), key=lambda kv: _pin_order(kv[0]))
    pins, used, index = {}, 0, max(0, start)
    while index < len(ordered):
        number, pin = ordered[index]
        text = " | ".join(str(pin[k]) for k in ("name", "function", "note") if pin.get(k))
        line = "%s (%s p.%s)" % (text, pin.get("source", "?"), pin.get("page", "?"))
        if pins and used + len(line) > PIN_PAGE_CHARACTERS:
            break
        pins[number], used, index = line, used + len(line), index + 1
    notes = [] if start else [
        "%s%s (%s p.%s)" % ((note["topic"] + ": ") if note.get("topic") else "", note["text"],
                            note.get("source", "?"), note.get("page", "?"))
        for note in data.get("notes") or []]
    out = {"part": data.get("part") or path.stem, "aliases": list(data.get("aliases") or []),
           "description": data.get("description") or "", "sources": list(data.get("sources") or []),
           "pins": pins, "pin_count": len(ordered), "notes": notes, "updated": data.get("updated") or "",
           "file": str(path)}
    if index < len(ordered):
        out["next_start"] = index
    return out


def brief(path: Path, data: dict) -> dict:
    """What a datasheet read carries: that facts are kept and how many, never the facts
    themselves, so the document's own text always arrives whole."""
    return {"part": data.get("part") or path.stem, "pins": len(data.get("pins") or {}),
            "notes": len(data.get("notes") or []),
            "hint": "kept facts: part_facts gives them; read on here only for something they lack"}


def _same_words(text: str) -> str:
    # Only spacing and case: a decimal point or sign changes a value (1.8 V is not 18 V).
    return " ".join(str(text).split()).casefold()


def _pin_order(number: str):
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.findall(r"\d+|\D+", str(number))]


def _text(value, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FactsError("%s must be text" % field)
    value = " ".join(value.split())
    if required and not value:
        raise FactsError("%s is needed" % field)
    if len(value) > limit:
        raise FactsError("%s is longer than %d characters; keep each fact short" % (field, limit))
    return value


def _page(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FactsError("%s needs page: the datasheet page it is read from (a whole number)" % where)
    return value


def save(library: Path, part: str, *, source: str = "", aliases=(), description: str = "",
         pins=(), notes=()) -> tuple[Path, dict, dict]:
    """Merge new facts into the part's file; returns (path, data, counts saved).

    A pin given again replaces what was held for that pin; a note already held
    (same words) is not added twice. Every pin and note names its page in source.
    """
    part = _text(part, "part", 100, required=True)
    pins, notes = list(pins or ()), list(notes or ())
    if (pins or notes) and not str(source or "").strip():
        raise FactsError("source is needed with pins or notes: the datasheet (or document) path they are read from")
    label = Path(str(source)).name if source else ""
    folder = library / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    lock = os.open(folder, os.O_RDONLY)  # the folder itself: no lock file left in his library
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        found = find(library, part)
        if found is None:
            path, data = folder / file_name(part), {"part": part}
        else:
            path, data = found
        counts = {"pins": 0, "notes": 0, "aliases": 0}
        stored_pins = dict(data.get("pins") or {})
        for index, pin in enumerate(pins):
            if not isinstance(pin, dict):
                raise FactsError("pins[%d] must be an object" % index)
            number = _text(pin.get("number"), "pins[%d].number" % index, 20, required=True)
            entry = {"name": _text(pin.get("name"), "pins[%d].name" % index, 100, required=True),
                     "function": _text(pin.get("function"), "pins[%d].function" % index, MAX_SHORT),
                     "note": _text(pin.get("note"), "pins[%d].note" % index, MAX_SHORT),
                     "source": label, "page": _page(pin.get("page"), "pins[%d]" % index)}
            stored_pins[number] = {k: v for k, v in entry.items() if v != ""}
            counts["pins"] += 1
        if len(stored_pins) > MAX_PINS:
            raise FactsError("more than %d pins for one part" % MAX_PINS)
        stored_notes = list(data.get("notes") or [])
        known = {_same_words(note.get("text", "")) for note in stored_notes}
        for index, note in enumerate(notes):
            if not isinstance(note, dict):
                raise FactsError("notes[%d] must be an object" % index)
            text = _text(note.get("text"), "notes[%d].text" % index, MAX_TEXT, required=True)
            entry = {"topic": _text(note.get("topic"), "notes[%d].topic" % index, 60), "text": text,
                     "source": label, "page": _page(note.get("page"), "notes[%d]" % index)}
            if _same_words(text) in known:
                continue
            known.add(_same_words(text))
            stored_notes.append({k: v for k, v in entry.items() if v != ""})
            counts["notes"] += 1
        if len(stored_notes) > MAX_NOTES:
            raise FactsError("more than %d notes for one part: keep the ones a later job needs" % MAX_NOTES)
        stored_aliases = list(data.get("aliases") or [])
        names = {key_of(item) for item in stored_aliases + [data.get("part", ""), path.stem]}
        for item in aliases or ():
            item = _text(item, "aliases", 100)
            if item and key_of(item) not in names:
                owner = find(library, item)
                if owner is not None and owner[0] != path:
                    raise FactsError("%s already names another part's facts (%s)" % (item, owner[0].name))
                names.add(key_of(item))
                stored_aliases.append(item)
                counts["aliases"] += 1
        if len(stored_aliases) > MAX_ALIASES:
            raise FactsError("more than %d aliases" % MAX_ALIASES)
        description = _text(description, "description", MAX_SHORT)
        sources = list(data.get("sources") or [])
        if source and str(source) not in sources:
            sources.append(str(source))
        data = dict(data, aliases=stored_aliases, sources=sources, pins=stored_pins, notes=stored_notes,
                    updated=date.today().isoformat())
        if description:
            data["description"] = description
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".facts-", suffix=".json")
        try:
            with os.fdopen(handle, "w") as out:
                json.dump(data, out, indent=1, ensure_ascii=False, sort_keys=True)
                out.write("\n")
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    finally:
        os.close(lock)
    return path, data, counts


__all__ = ["FactsError", "brief", "find", "held", "key_of", "part_of_document", "save", "view"]
