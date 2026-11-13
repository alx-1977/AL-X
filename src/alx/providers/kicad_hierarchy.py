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


__all__ = ["HierarchyError", "make_hierarchical", "netlist_nets"]
