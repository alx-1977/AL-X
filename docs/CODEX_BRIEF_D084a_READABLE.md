# Codex brief: readable schematics and the remaining live-test fixes (D-084a, round 3)

**Branch:** `board-edit-capability` (HEAD 4347986 holds rounds 1–2).

**Read first:**
- `AGENTS.md` and the documents it names;
- D-084a in `governance/DECISIONS.md`;
- `docs/CODEX_REPORT_D084a.md` and `docs/CODEX_REPORT_D084a_FIXES.md`;
- the FireFli standard `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/SCHEMATIC_STANDARD.md`, especially sections 2, 3 and 3a.

**Ground rules:**
- Don't commit, push, open PRs or request reviews.
- Leave changes in the working tree and write `docs/CODEX_REPORT_D084a_READABLE.md`.

## Why

The board agent (Sol) now finishes a job start to finish with clean checks. Job `8ab320b33638`, in `.alx/runtime/board-jobs/`, and its work copy `.alx/runtime/kicad/3822ff907d4c4012aa9a68dddebc977c/project/RS485_Port.kicad_sch`. But Friedl judged the drawing unreadable: "like someone turned a box of toys upside down".
- **Labels everywhere:** it cut the port into small islands joined by local net labels (RS485_A/RS485_B appear about six times; bias resistors, termination, TVS and test points all float).
- **Parts not lined up;** the twin TX/RX blocks aren't mirrored.

Friedl's direction:
- **Don't restrict capability.** Guide placement with structure: "almost like a grid where components can go, and then connect the pins".
- Crossings are allowed, shown with hops; avoid them only where easy.
- The guidance must be **mechanical** (helpers and gate rules). Text in the prompt has repeatedly failed.

## Work items

### 1. No local labels where a wire would do (gate rule)
- **New style rule:** a local net label (`label`, not `hierarchical_label` / `global_label`) whose net connects only pins on the same sheet, and doesn't reach a hierarchical label, sheet pin, bus or bus entry, is a violation: "label-joined pins".
  - Report the net, the pins it joins and the label positions.
  - Power symbols aren't labels.
- **The gate refuses NEW violations** of this rule, like the other style rules: old drawings keep theirs.
- **Exception:** the edit's `intent` may list `{"label_net": "<name>", "reason": "..."}`. A declared label net is allowed and is listed in the edit result so Friedl sees it.

### 2. Slot-grid placement (guidance, not restriction)
- **Helper `block_grid(sheet, name, origin, columns, rows, col_pitch=12.7, row_pitch=7.62)`.** It defines a named placement grid for one block. Columns follow the signal flow left to right (`input`, `protect`, `ic`, `output_net`, `output` or any names given). Rows: `rail` on top, numbered signal rows, `ground` at the bottom.
  - Store the grid in the sheet, for example as a hidden text or a sidecar JSON next to the sheet in the work copy, so later edits and checks know it.
- **Helper `place_in_slot(sheet, ref, block, column, row, rotation=0)`.** It moves a symbol so its anchor sits exactly on the slot point, on the 1.27 grid, and tidies its fields (`tidy_fields`).
- **Helper `mirror_block(sheet, source_block, target_block, ref_map, axis='y')`.** It copies slot assignments from one block to its twin, mirrored or identical. Reuse `copy_layout` where it fits.
- **Free placement remains.** The style check *warns* (not refuses) when a symbol inside a defined block sits off its slot points, and when two parts on one signal row are off by less than one grid step ("almost aligned").

### 3. Wiring helper that routes
- **Helper `route(sheet, pin_a, pin_b)`** (pins as `"R7.2"` style). It draws an orthogonal wire from one pin to another:
  - leave each pin straight for at least one grid step;
  - prefer the channels between slot rows and columns;
  - never pass through a symbol body or over an unconnected pin;
  - crossings with other wires are allowed;
  - add junctions where it tees into an existing wire of the same net.
- If no route exists, say why, without falling back to a label.
- Keep `s.wire_pin_to` and the existing drawing helpers.

### 4. Ruled tables
- **Helper `info_table(sheet, origin, columns_mm, rows)`.** It draws a ruled table (lines plus cell text, header row bold, 1.016 mm text) under a block. `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/reference_p0005/restyle.py` (`table()`) is the reference.
- **Style rule:** loose `text` lines of the "Key: value" table kind under a block, more than two in a column, are a warning: "use info_table".

### 5. Use a design block when one fits
- **Helper `place_design_block(sheet, library_block, at, ref_map=None)`.** It places a FireFli design block from `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/blocks/FireFli_Blocks.kicad_blocks/<name>.kicad_block/<name>.kicad_sch`, keeping its wiring, tables and layout.
  - Existing parts can be bound to the block's references through `ref_map`, which keeps their UUIDs and values.
  - Report nets the block expects that the sheet doesn't have.
- **List the available blocks** (name and the description from the `.json`) in the board agent's instructions, in one line each.

### 6. Goal deadlock after restart
- **Symptom:** after a restart, with the board goal `awaiting_input`, Core asked to run a capability. The result was `Dispatch blocked before approval: active_goal_required`, then `select_goal` was rejected as `goal_selection_redundant` (current == requested). Repeated three times; see `.alx/runtime/live_voice.log` around 2026-11-20 13:42.
- **Fix:** selecting the current goal when it's `awaiting_input` makes it active, or the dispatch accepts the current goal. No new goal machinery; the smallest correct change.
- Add a test.

### 7. Allow deleting an unreferenced sheet file
- **Symptom:** the gate refused removing an empty `RS485_Interface.kicad_sch` that was outside the hierarchy ("outside the exported schematic hierarchy").
- **Fix:** deleting a `.kicad_sch` that no sheet references is allowed, with an empty intent.
- **And:** rollback of a failed edit must remove files that edit created.

### 8. A plain "what she's doing now" line
- **For Friedl:** while a board job runs, show one plain line in AL/X's interface. For example: "Board job P0005: editing RS485_Port (call 42), last check clean". Update it per call, from the job journal (`board-jobs/<job>.json` and `.calls.jsonl`).
- Keep it to the existing interface and status plumbing. No new frontend business logic (AGENTS.md).

## Agent instructions
- Update the board agent's instructions in **two or three short lines:**
  - define a block grid and place by slots;
  - wire with `route`;
  - put information in `info_table`;
  - mirror twin blocks;
  - use a design block when one fits.
- Keep them lean. The standard file stays the reference.

## Tests and gates
- Add tests for each item. Use small fixtures, and the P0005 test copy (read-only) where useful: `/Users/friedlbasson/Library/Mobile Documents/com~apple~CloudDocs/ALX_Storage/Projects/P0005-TEST-original/hardware/kicad`.
- Run both law gates and the full suite; it was 5,707 passed, 2 skipped.
- Report results, anything not done, and any judgment calls.
