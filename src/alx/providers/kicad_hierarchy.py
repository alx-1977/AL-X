"""Separate top-level sheets made into one hierarchical design (2026-11-13).

KiCad 10 imports each Altium sheet as a top-level sheet. The editor reads
them all, but KiCad's command-line check of the board against its
schematic reads only the first, so on P0020 a swapped antenna pin went
unseen. One top sheet that places every other sheet is KiCad's standard
structure and every KiCad tool reads it whole.

This works on a work copy only, on the files as text, in KiCad 10's own
format (taken from a hierarchical KiCad 10 project): a new top sheet with
one sheet box per former top-level sheet, keeping each sheet's identifier;
each placed symbol's instance path gains the new top sheet in front; each
board footprint's link gains its sheet. Connections are unchanged: labels
keep their scope and global labels connect across sheets as before. The
caller compares KiCad's netlist before and after.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

SHEET_WIDTH, SHEET_HEIGHT, SHEET_GAP = 45.72, 25.4, 12.7


class HierarchyError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _file_name(name: str, taken: set[str]) -> str:
    stem = re.sub(r"[^A-Za-z0-9_&-]+", "_", name).strip("_") or "Sheet"
    candidate, number = stem + ".kicad_sch", 2
    while candidate in taken:
        candidate, number = f"{stem}_{number}.kicad_sch", number + 1
    return candidate


def _top_sheet(root_uuid: str, project: str, sheets: list[dict[str, Any]], header: str) -> str:
    blocks = []
    for index, sheet in enumerate(sheets):
        x = 25.4 + index * (SHEET_WIDTH + SHEET_GAP)
        y = 50.8
        blocks.append(f"""	(sheet
		(at {x:.2f} {y:.2f})
		(size {SHEET_WIDTH} {SHEET_HEIGHT})
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(dnp no)
		(stroke (width 0.1524) (type solid))
		(fill (color 0 0 0 0.0000))
		(uuid {_quoted(sheet["uuid"])})
		(property "Sheetname" {_quoted(sheet["name"])}
			(at {x:.2f} {y - 0.71:.2f} 0)
			(effects (font (size 1.524 1.524) (bold yes)) (justify left bottom))
		)
		(property "Sheetfile" {_quoted(sheet["filename"])}
			(at {x:.2f} {y + SHEET_HEIGHT + 0.64:.2f} 0)
			(effects (font (size 1.27 1.27)) (justify left top))
		)
		(instances
			(project {_quoted(project)}
				(path {_quoted("/" + root_uuid)}
					(page {_quoted(str(index + 2))})
				)
			)
		)
	)
""")
    return (f"""(kicad_sch
	(version 20260306)
	(generator "eeschema")
	(generator_version "10.0")
	(uuid {_quoted(root_uuid)})
	(paper "A3")
{header}	(lib_symbols)
""" + "".join(blocks) + """	(sheet_instances
		(path "/"
			(page "1")
		)
	)
	(embedded_fonts no)
)
""")


def _title_block(schematic: str) -> str:
    """The former top file's title block, kept on the new top sheet."""
    match = re.search(r"\n(\t\(title_block\n.*?\n\t\)\n)", schematic, re.S)
    return match.group(1) if match else ""


def _without_sheet_instances(schematic: str) -> str:
    """A placed sheet has no sheet_instances of its own; only the top sheet does."""
    return re.sub(r"\n\t\(sheet_instances\n.*?\n\t\)(?=\n)", "", schematic, flags=re.S)


def make_hierarchical(project: Path) -> dict[str, Any]:
    """Convert the project in `project` (a work copy folder) in place; report what changed."""
    pro_files = sorted(project.glob("*.kicad_pro"))
    if len(pro_files) != 1:
        raise HierarchyError("unsupported_project", "the copy needs exactly one .kicad_pro")
    pro_path = pro_files[0]
    settings = json.loads(pro_path.read_text())
    sheets = [dict(item) for item in (settings.get("schematic", {}).get("top_level_sheets") or ())]
    if len(sheets) < 2:
        return {"converted": False, "reason": "already one top sheet", "sheets": [s.get("name", "") for s in sheets],
                "renamed": {}, "footprints_relinked": 0, "footprints_not_found": []}
    project_name = pro_path.stem
    root_file = project_name + ".kicad_sch"
    taken = {path.name for path in project.glob("*.kicad_sch")}
    renamed = {}
    for sheet in sheets:
        name = sheet.get("filename")
        # Only a file of this folder: never a path that leads outside the copy (review of #239).
        if (not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..")
                or not name.endswith(".kicad_sch")):
            raise HierarchyError("unsupported_project", f"sheet file {name!r} is not a schematic in this folder")
        target = project / name
        if target.is_symlink() or not target.is_file() or target.resolve().parent != project.resolve():
            raise HierarchyError("unsupported_project", f"sheet file {name} is missing")
        if not isinstance(sheet.get("uuid"), str) or not re.fullmatch(r"[0-9A-Za-z-]{1,64}", sheet["uuid"]):
            raise HierarchyError("unsupported_project", f"sheet {name} has no usable identifier")
    old_root = (project / root_file).read_text() if (project / root_file).is_file() else ""
    for sheet in sheets:
        if sheet["filename"] == root_file:
            # The top file becomes the new top sheet; its own content moves out.
            new_name = _file_name(sheet["name"], taken)
            taken.add(new_name)
            renamed[root_file] = new_name
            sheet["filename"] = new_name
    root_uuid = str(uuid.uuid4())
    symbol_sheet = {}
    for sheet in sheets:
        source_name = next((old for old, new in renamed.items() if new == sheet["filename"]), sheet["filename"])
        schematic = (project / source_name).read_text()
        before = '(path "/%s"' % sheet["uuid"]
        schematic = schematic.replace(before, '(path "/%s/%s"' % (root_uuid, sheet["uuid"]))
        schematic = _without_sheet_instances(schematic)
        (project / sheet["filename"]).write_text(schematic)
        for symbol in re.finditer(r'\n\t\(symbol\n\t\t\(lib_id "[^"]*"\).*?\n\t\t\(uuid "([^"]+)"\)', schematic, re.S):
            symbol_sheet[symbol.group(1)] = sheet
    (project / root_file).write_text(_top_sheet(root_uuid, project_name, sheets, _title_block(old_root)))
    settings.setdefault("schematic", {})["top_level_sheets"] = [
        {"filename": root_file, "name": "Root", "uuid": root_uuid}]
    settings["sheets"] = [[root_uuid, "Root"], *[[sheet["uuid"], sheet["name"]] for sheet in sheets]]
    pro_path.write_text(json.dumps(settings, indent=2) + "\n")
    relinked, not_found = 0, []
    for board_path in project.glob("*.kicad_pcb"):
        board = board_path.read_text()

        def relink(match: re.Match) -> str:
            nonlocal relinked
            block = match.group(0)
            path = re.search(r'\n\t\t\(path "/([^"/]+)"\)', block)
            if not path:
                return block
            sheet = symbol_sheet.get(path.group(1))
            if sheet is None:
                reference = re.search(r'\(property "Reference" "([^"]+)"', block)
                not_found.append(reference.group(1) if reference else path.group(1))
                return block
            relinked += 1
            block = block.replace(path.group(0), '\n\t\t(path "/%s/%s")' % (sheet["uuid"], path.group(1)), 1)
            block = re.sub(r'\n\t\t\(sheetname "[^"]*"\)', '\n\t\t(sheetname %s)' % _quoted("/" + sheet["name"] + "/"),
                           block, count=1)
            return re.sub(r'\n\t\t\(sheetfile "[^"]*"\)', '\n\t\t(sheetfile %s)' % _quoted(sheet["filename"]),
                          block, count=1)
        board = re.sub(r"\n\t\(footprint .*?\n\t\)(?=\n)", relink, board, flags=re.S)
        board_path.write_text(board)
    return {"converted": True, "reason": "", "sheets": [sheet["name"] for sheet in sheets], "renamed": renamed,
            "footprints_relinked": relinked, "footprints_not_found": not_found[:40]}


PIN_PITCH, TRACK_GAP, BLOCK_GAP = 2.54, 2.54, 12.7


def _global_labels(schematic: str) -> list[tuple[str, str, str]]:
    """Each global label on a sheet: its whole block, its name and its shape."""
    found = []
    for block in re.finditer(r'\n\t\(global_label "((?:[^"\\]|\\.)*)"\n\t\t\(shape ([a-z_]+)\).*?\n\t\)(?=\n)',
                             schematic, re.S):
        found.append((block.group(0), block.group(1), block.group(2)))
    return found


def _as_hierarchical(block: str) -> str:
    """A global label as a hierarchical label: same name, shape, place and identifier."""
    block = block.replace("\n\t(global_label ", "\n\t(hierarchical_label ", 1)
    block = re.sub(r'\n\t\t\(property "Intersheetrefs".*?\n\t\t\)(?=\n)', "", block, flags=re.S)
    return re.sub(r"\n\t\t\(fields_autoplaced [a-z]+\)", "", block)


def _segment(kind: str, x1: float, y1: float, x2: float, y2: float) -> str:
    return (f"\t({kind}\n\t\t(pts\n\t\t\t(xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f})\n\t\t)\n"
            f"\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
            f"\t\t(uuid {_quoted(str(uuid.uuid4()))})\n\t)\n")


def connect_with_sheet_pins(project: Path) -> dict[str, Any]:
    """Show how the sheets connect, as Altium's sheet entries did (Friedl, 2026-11-13).

    A global label used on more than one sheet becomes a hierarchical label
    on each, a pin on each sheet's block on the top sheet, and a wire (or a
    bus, for a bus label) joining those pins there, carrying one global label
    of the same name so the net keeps its name. Only a top sheet that holds
    nothing but its sheet blocks is changed. The caller compares netlists.
    """
    pro_files = sorted(project.glob("*.kicad_pro"))
    if len(pro_files) != 1:
        raise HierarchyError("unsupported_project", "the copy needs exactly one .kicad_pro")
    root_path = project / (pro_files[0].stem + ".kicad_sch")
    top = root_path.read_text() if root_path.is_file() else ""
    blocks = list(re.finditer(r"\n\t\(sheet\n.*?\n\t\)(?=\n)", top, re.S))
    body = re.sub(r"\n\t\(sheet\n.*?\n\t\)(?=\n)", "", top, flags=re.S)
    # Only a top sheet holding nothing but plain sheet blocks is rebuilt: notes,
    # drawings, sheet settings or extra fields of his would be lost (review of #240).
    elements = set(re.findall(r"\n\t\(([a-z_]+)", body))
    plain = {"version", "generator", "generator_version", "uuid", "paper", "title_block", "lib_symbols",
             "sheet_instances", "embedded_fonts"}
    if not blocks or elements - plain:
        return {"connected": [], "reason": "the top sheet holds more than its sheet blocks; nothing changed"}
    for block in blocks:
        fields = re.findall(r'\(property "([^"]+)"', block.group(0))
        flags = re.findall(r"\n\t\t\((exclude_from_sim|in_bom|on_board|dnp) ([a-z]+)\)", block.group(0))
        if sorted(fields) != ["Sheetfile", "Sheetname"] or dict(flags) != {
                "exclude_from_sim": "no", "in_bom": "yes", "on_board": "yes", "dnp": "no"}:
            return {"connected": [], "reason": "a sheet block has its own settings or fields; nothing changed"}
    sheets = []
    for block in blocks:
        text_of = block.group(0)
        name = re.search(r'\(property "Sheetname" "((?:[^"\\]|\\.)*)"', text_of)
        file_name = re.search(r'\(property "Sheetfile" "((?:[^"\\]|\\.)*)"', text_of)
        sheet_uuid = re.search(r'\n\t\t\(uuid "([^"]+)"\)', text_of)
        page = re.search(r'\(page "([^"]+)"\)', text_of)
        if not (name and file_name and sheet_uuid) or "\n\t\t(pin " in text_of:
            return {"connected": [], "reason": "a sheet block already has pins or is incomplete; nothing changed"}
        target = project / file_name.group(1)
        if Path(file_name.group(1)).name != file_name.group(1) or not target.is_file():
            raise HierarchyError("unsupported_project", f"sheet file {file_name.group(1)!r} is not in this folder")
        sheets.append({"name": name.group(1), "filename": file_name.group(1), "uuid": sheet_uuid.group(1),
                       "page": page.group(1) if page else str(len(sheets) + 2),
                       "labels": _global_labels(target.read_text())})
    uses: dict[str, list[int]] = {}
    shapes: dict[str, str] = {}
    for index, sheet in enumerate(sheets):
        sheet["shapes"] = {}
        for _, label, shape in sheet["labels"]:
            if index not in uses.setdefault(label, []):
                uses[label].append(index)
            shapes.setdefault(label, shape)
            # Each block's pin shows that sheet's own direction (review of #240).
            sheet["shapes"].setdefault(label, shape)
    shared = sorted(label for label, where in uses.items() if len(where) > 1)
    if not shared:
        return {"connected": [], "reason": "no global label is used on more than one sheet"}
    for sheet in sheets:
        target = project / sheet["filename"]
        schematic = target.read_text()
        for whole, label, _ in sheet["labels"]:
            if label in shared:
                schematic = schematic.replace(whole, _as_hierarchical(whole), 1)
        target.write_text(schematic)
    # Two columns of blocks facing each other, as an Altium top sheet: pins
    # on the facing edges, and between the columns one vertical track per
    # connection, joined to each pin by a short stub. Right-hand pins sit
    # half a step lower than left-hand ones, so no two stubs share a height:
    # a stub crosses other tracks without touching their ends and connects
    # only where it ends. Everything stays on KiCad's 2.54 mm grid.
    pitch, width = 2 * PIN_PITCH, 50.8
    lane = (len(shared) + 3) * 2 * TRACK_GAP
    left_x = 50.8
    right_x = left_x + width + lane
    tracks = {label: left_x + width + 2 * TRACK_GAP * (index + 2) for index, label in enumerate(shared)}
    pins: dict[str, list[float]] = {label: [] for label in shared}
    stubs: list[tuple[str, float, float]] = []
    parts, drawing = [], []
    row_y, row_height = 38.1, 0.0
    for index, sheet in enumerate(sheets):
        column = index % 2
        if column == 0 and index:
            row_y += row_height + BLOCK_GAP * 2
            row_height = 0.0
        x0, y = (left_x, row_y) if column == 0 else (right_x, row_y)
        mine = [label for label in shared if label in {item[1] for item in sheet["labels"]}]
        height = max(SHEET_HEIGHT, pitch * (len(mine) + 1))
        row_height = max(row_height, height)
        edge = x0 + width if column == 0 else x0
        pin_blocks = []
        for slot, label in enumerate(mine):
            py = y + pitch * (slot + 1) + (PIN_PITCH if column else 0)
            pins[label].append(py)
            stubs.append((label, edge, py))
            pin_blocks.append(f"""		(pin {_quoted(label)} {sheet["shapes"][label]}
			(at {edge:.2f} {py:.2f} {0 if column == 0 else 180})
			(uuid {_quoted(str(uuid.uuid4()))})
			(effects (font (size 1.27 1.27)) (justify {'right' if column == 0 else 'left'}))
		)
""")
        parts.append(f"""	(sheet
		(at {x0:.2f} {y:.2f})
		(size {width:.2f} {height:.2f})
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(dnp no)
		(stroke (width 0.1524) (type solid))
		(fill (color 0 0 0 0.0000))
		(uuid {_quoted(sheet["uuid"])})
		(property "Sheetname" {_quoted(sheet["name"])}
			(at {x0:.2f} {y - 0.71:.2f} 0)
			(effects (font (size 1.524 1.524) (bold yes)) (justify left bottom))
		)
		(property "Sheetfile" {_quoted(sheet["filename"])}
			(at {x0:.2f} {y + height + 0.64:.2f} 0)
			(effects (font (size 1.27 1.27)) (justify left top))
		)
""" + "".join(pin_blocks) + f"""		(instances
			(project {_quoted(pro_files[0].stem)}
				(path {_quoted("/" + re.search(r'\(uuid "([^"]+)"\)', top).group(1))}
					(page {_quoted(sheet["page"])})
				)
			)
		)
	)
""")
    for label, edge, py in stubs:
        kind = "bus" if "{" in label or "[" in label else "wire"
        drawing.append(_segment(kind, edge, py, tracks[label], py))
    for label in shared:
        kind = "bus" if "{" in label or "[" in label else "wire"
        x = tracks[label]
        heights = sorted(pins[label])
        # Every track starts above the first row of blocks, where no stub
        # runs: a track's end on another connection's stub would join them.
        top_y = 38.1 - 2 * pitch
        drawing.append(_segment(kind, x, top_y, x, heights[0]))
        for upper, lower in zip(heights, heights[1:]):
            drawing.append(_segment(kind, x, upper, x, lower))
        for py in heights[:-1]:
            drawing.append(f"\t(junction\n\t\t(at {x:.2f} {py:.2f})\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n"
                           f"\t\t(uuid {_quoted(str(uuid.uuid4()))})\n\t)\n")
        drawing.append(f"""	(global_label {_quoted(label)}
		(shape {shapes[label] if len({s["shapes"].get(label) for s in sheets} - {None}) == 1 else "bidirectional"})
		(at {x:.2f} {top_y:.2f} 90)
		(effects (font (size 1.27 1.27)) (justify left))
		(uuid {_quoted(str(uuid.uuid4()))})
		(property "Intersheetrefs" "${{INTERSHEET_REFS}}"
			(at {x:.2f} {top_y:.2f} 0)
			(hide yes)
			(effects (font (size 1.27 1.27)))
		)
	)
""")
    head = top[:blocks[0].start()] + "\n"
    tail = top[blocks[-1].end():]
    root_path.write_text(head + "".join(parts) + "".join(drawing) + tail.lstrip("\n"))
    return {"connected": shared, "reason": ""}


def netlist_nets(netlist: str) -> dict[frozenset, str]:
    """Each net of a KiCad netlist as its set of (reference, pin), with its name."""
    nets = {}
    start = netlist.find("(nets")
    for block in netlist[start:].split("(net\n")[1:] if start >= 0 else ():
        name = re.search(r'\(name "((?:[^"\\]|\\.)*)"\)', block)
        nodes = frozenset(re.findall(r'\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', block))
        if nodes:
            nets[nodes] = name.group(1) if name else ""
    return nets


__all__ = ["HierarchyError", "connect_with_sheet_pins", "make_hierarchical", "netlist_nets"]
