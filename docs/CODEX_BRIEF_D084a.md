# Codex brief: board edits as checked scripts (D-084a)

You are implementing an approved decision in the AL/X repository, on branch `board-edit-capability`.

**Before anything else**, read `AGENTS.md` and the documents it names, then:
- `governance/DECISIONS.md`: D-061, D-082, D-082b, D-083, D-083a, D-084, and the new **D-084a**;
- `docs/BOARD_SCRIPT_PATH_PLAN.md`: the plan Friedl approved;
- `docs/HANDOVER_2026-11-20.md`: why this change is being made.

**Ground rules:**
- Don't commit, push, open PRs or request any review.
- Leave your changes in the working tree; Claude reviews them and commits.

## Goal

The board agent (`src/alx/specialists/board_agent.py`, `src/alx/tools/board_agent.py`) edits a work copy through **one path: a script** run by `kicad edit` (`src/alx/providers/kicad.py`, `def edit`, about line 1444). The script runs against the existing tested helpers; their description is in `src/alx/tools/kicad.py` around lines 160–240. Code checks every script's result with a gate and undoes it if the gate fails. The D-084 `change` input disappears as a separate route (Law 0). Its connection-checking logic is reused as the gate's intent check.

## Work items

### 1. Let the board agent use `script`
- Remove the refusal in `src/alx/tools/board_agent.py` around line 40 (`board_agent_script_not_allowed`), and the matching text in the kicad tool description ("for you alone, not your board agent").
- Update the board agent's instructions in `src/alx/specialists/board_agent.py` to match.

### 2. One path: remove `change` as an input, keep its checks
- `kicad edit` no longer accepts `change`. Remove `validate_change`/`apply_change` as a public route, and the `change` parameter from the schema and description.
- **Every D-084 operation must have a script-helper equivalent.** Most already exist (`s.swap_symbol`, `s.remove_symbol`, `s.wire_pin_to`, `s.label_pin`, `s.ground_pin`, `s.power_pin`, `s.add_no_connect`, `s.tidy_fields`, `replace_part`, `add_part`, `move_part`, `add_track`, `add_via`, `remove`).
  - List the ops in `src/alx/providers/kicad_change.py` (`apply_change`) and map each to a helper.
  - Add any missing helper, with a test.
  - Keep the useful code from `kicad_change.py` (`unmet`, `_unasked`, `set_check`, the netlist comparison) for item 3.
- `revert_to` and `dry_run` stay.

### 3. Intent declaration and the gate
- `kicad edit` with `script` takes an **`intent`**: the connections the script means to make and break, and the parts it adds, removes or swaps. Reuse the D-084 operation vocabulary for describing connections if that fits; keep it simple and validated.
  - Required for every non-dry-run edit that changes a schematic or board.
  - A pure field or text edit may declare `"intent": []`.
- **After the script, before the result is kept**, run the gate:
  1. **Intent check.** Compare KiCad's netlist before and after (the D-084 machinery). Every declared change must hold, and **no pin may change connectivity that the intent does not name**. A part swapped or removed counts as declared for its own pins.
  2. **Style check.** Port `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/stylecheck.py` (with its `sx.py` reader) into a module such as `src/alx/providers/kicad_style.py`.
     - Run it on every schematic sheet the script changed.
     - Fail only on violations that are **new** compared with the sheet before the edit. Friedl's existing sheets carry old ones.
  3. **ERC: no new errors.** Use the existing "only new" ERC. New warnings are reported but don't fail.
  4. **DRC: no new errors** for edits that change the board (the existing `compared_to_original` / DRC before-after).
- A gate failure undoes the edit, exactly as a failed edit does today. It returns a refusal naming each failing item: unmet intent, unexpected connectivity change with pins, style violation with position, new ERC/DRC error. The refusal must be specific enough for the agent to fix the script, not guess.
- `dry_run` runs the gate too and reports it, then restores as now.

### 4. The agent sees its own drawing
- After a kept edit that changed a schematic sheet or the board, render the changed sheet (or the board placement) to PNG and **crop it to the changed area plus a margin**. The tooling already renders to PDF; `pdftoppm` is available on this Mac at `/opt/homebrew/bin`. Check that and fall back gracefully if it's missing.
- Return the crop as a tool-result image, so the model actually sees it. The Codex provider already passes `tool_images` (`session.answer(request.tool_result, request.tool_images)`); check the Claude provider path too.
  - If D-083a's disabled `view_image` blocks this, enable image viewing **only** for these tool-result images, and record the amendment in D-084a's text.
- Keep the image small: crop, max about 1600 px on the long side.

### 5. Missing sheet operations (the reason P0005 failed)
Add tested helpers, using `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/reference_p0005/` as reference. Those are one-off scripts that did this on P0005 today; read their README.
- `new_sheet(name, file)`: create a hierarchical sheet. The sheet file, the root sheet symbol and its instances block.
- Move items in a region of one sheet to another sheet (`build.py` shows how), keeping symbol UUIDs. Fix every moved symbol's instance path to the new sheet. Report anything that crosses the boundary so the script can relabel it.
- Copy a block's layout onto its twin block by a reference map (`copy_ldo.py`), the parts keeping their own values.
- Swap a symbol to a library MPN whose library symbol is derived (`extends`): flatten it into `lib_symbols` (`power_restyle.py`, `ensure_res`).

### 6. The standard
- In the board agent's instructions, add **one short paragraph**:
  - schematics follow `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/SCHEMATIC_STANDARD.md` (read it at the start of a schematic job);
  - reusable circuits are in `FireFli-KiCad-Lib/blocks/`.
- Don't paste the standard into the prompt.

## Tests and gates
- Add tests for:
  - an intent that holds passes;
  - a missing declared connection is undone and named;
  - an undeclared connectivity change is undone and names the pins;
  - a new style violation is undone, while an old one is ignored;
  - a new ERC error is undone;
  - the board agent may call `script`;
  - `change` is refused as an unknown argument;
  - `new_sheet`, the region move (instance paths correct, netlist unchanged), the layout copy, and the derived-symbol swap.
- Build schematic fixtures small. You may copy a sheet from P0005 as a fixture: `/Users/friedlbasson/Library/Mobile Documents/com~apple~CloudDocs/ALX_Storage/Projects/P0005-teralec-rs485/hardware/kicad` (read only, never write there).
- **Run the law gates and the full test suite** (see `AGENTS.md`; the full suite was 5,697 passing). Report what passed, and what couldn't be verified.

## Report back
- Write a short summary to `docs/CODEX_REPORT_D084a.md`:
  - what changed, file by file;
  - the D-084 op → helper mapping;
  - test results;
  - anything not done, or any judgment call you made.
