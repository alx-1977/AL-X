# Codex brief: fixes from the first live D-084a test

**Branch:** `board-edit-capability` (HEAD a468c72 holds the D-084a work you built).

**Read first:**
- `AGENTS.md` and the documents it names;
- D-084a in `governance/DECISIONS.md`;
- `docs/CODEX_REPORT_D084a.md`.

**Ground rules:**
- Don't commit, push, open PRs or request reviews.
- Leave your changes in the working tree and write `docs/CODEX_REPORT_D084a_FIXES.md`.

## Evidence

The live board agent ran job `8ab320b33638`: P0005-TEST-original, "move RS485 to a new sheet and fix the LM393 activity circuit". The files:
- `.alx/runtime/board-jobs/8ab320b33638.json`: journal and reports;
- `.alx/runtime/board-jobs/8ab320b33638.calls.jsonl`: every call, with what was sent and the outcome.

**What happened:**
- It used about 190 calls; the first stretch hit the 150-call limit.
- It ended **blocked**.
- Its reasoning was good. The failures are application issues.

## Fixes

### 1. ERC "only new" comparison is wrong after a sheet changes (this blocked the job)
- **Symptom:** after a correct sheet move (pin connectivity preserved), the gate refused the edit. It counted two existing ERC errors as new: U1/U3 pin 4 (PG, open collector) connected to a power output, from the WEG_RS485_2 sheet. The only change was that the sheet path or sheet structure changed. See call `8ab320b33638-150` and the final report.
- **Fix:** match ERC findings between before and after by what they are: check type, description, and the symbol references and pin numbers involved, as a multiset. Don't match by sheet path, sheet name or coordinates.
  - Keep the existing undriven-power-net handling.
  - Add a test: move a region carrying a known ERC error to a new sheet. No new ERC finding may be reported.

### 2. One tested helper to split a sheet, keeping identities
The agent hand-edited the root's `Sheetfile` strings and the instance-path UUIDs to keep the moved section's identity. That broke the hierarchy, and the gate correctly refused it. Add:
- `split_sheet(source_file, region, new_name, new_file, keep="moved" | "remaining")`. It moves the items in `region` to a new sheet.
- With `keep="moved"`, the **moved** items keep the source sheet's instance path and the source sheet symbol (so PCB footprint links stay valid), and the **remaining** items move to the new sheet path instead.
- `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/reference_p0005/build.py` does exactly this for P0005 (`RS_SHEET` kept, `PWR_SHEET` new).
- It reports boundary-crossing wires and labels as `move_region` does.
- Test: the netlist is identical before and after (pin groups), the hierarchy exports cleanly, and every symbol's instance path is valid.

### 3. Find the KiCad project inside a project folder
- **Symptom:** 8 failed calls. The agent passed `project_dir` as `P0005-TEST-original` (the project folder under `ALX_Storage/Projects`) and got "no KiCad board in this folder", then guessed paths and hit `path_denied`.
- Every FireFli project keeps KiCad files in `<project>/hardware/kicad` (see `ALX_Storage/Projects/README.md`).
- **Fix:** when `project_dir` names a project folder, or a bare project name under the Projects root, resolve it to `hardware/kicad` if that holds exactly one board. If not, the error lists the candidate paths it found.

### 4. Helper descriptions say what each helper takes and returns
- **Symptom:** several calls were dry-run scripts that printed `inspect.signature` / `inspect.getsource` of the new helpers. Some poked private attributes (`r._tree`), and some failed on wrong guesses ("'list' object has no attribute 'items'": `s.symbols()` returns a list).
- **Fix:** in the `kicad edit` description, give each helper's signature and return shape in one line. That covers new_sheet, move_region, split_sheet, copy_layout, sheet, sheets, and the Sheet methods symbols/symbol/wires/labels.
  - Keep it compact; it's part of every request.
  - Add a `help(name)` helper the script can print for the full docstring, instead of reading source.

### 5. Fewer, cheaper reads
- About 85 of the calls were `read_reference`, many paging through the same large schematic files.
- **Fix:** give the agent compact reads of a schematic.
  - `inspect_schematic` should be able to return one region, or one net's members, or one symbol's pins and connections, without the whole file.
  - Document that the schematic should be read through these, not by paging the raw file.
- Keep raw reading available for evidence files.
- Note the calls this would have saved in the report. Don't remove capability.

## Tests and gates
- Add tests for each fix.
- Run `python3 scripts/check_governance.py`, `python3 scripts/check_architecture.py` and the full suite (`python3 -m pytest -n 6 -q`; it was 5,701 passed, 2 skipped).
- Report results and anything not done.
