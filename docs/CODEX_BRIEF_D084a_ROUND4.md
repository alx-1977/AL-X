# Codex brief: round 4 fixes from the Astra board-agent test (D-084a)

**Branch:** `board-edit-capability` (HEAD 5fb4365 holds rounds 1–3).

**Read first:**
- `AGENTS.md` and the documents it names;
- D-084a;
- `docs/CODEX_REPORT_D084a_READABLE.md`;
- `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/SCHEMATIC_STANDARD.md`.

**Ground rules:**
- Don't commit, push, open PRs or request reviews.
- Write `docs/CODEX_REPORT_D084a_ROUND4.md`.

## Evidence

- **Job** `.alx/runtime/board-jobs/9c2c0b26b660.*`.
- **Work copy** `.alx/runtime/kicad/be3f90e5700e443ba23f871e390bce57/project` (sheets `WEG_RS485.kicad_sch` root, `WEG_RS485_2.kicad_sch` RS485, `Power.kicad_sch`).
- **Friedl:** "a whole lot better". Three problems remain.

## 1. The label rule must apply inside a block only

- **The standard:** *wires inside a block, labels or ports between blocks.*
- **The round-3 rule** refuses any same-sheet label join. So the agent ran long wires between the "RS485 Activity LEDs" block and the "RS485 Isolated Port" block (RS485_D/RS485_R). Friedl: "I would not have connected the two sub-circuits with traces. This is one area where I would use ports or net labels."
- **Fix:** a label net is a violation only when all the pins it joins lie inside **one** block.
  - **Block regions:** use `block_grid` regions where defined. Otherwise, each underlined block title (bold text with a solid polyline under it, as `info_table`/title helpers draw) opens a block. Its region is the area below and to the right of the title, up to the next title or the sheet edge. Choose a clear rule, document it, and test it on the evidence sheet: RS485_D/RS485_R between the two blocks must be allowed.
  - If no blocks can be determined on a sheet, report the rule as a warning only.
- **Keep the `label_net` declaration** for exceptions inside a block.

## 2. No symbol drawn upside down

**Friedl, twice:** power symbols drawn upside down; and in this job U2 (18024115401L) is drawn upside down (GND1/GND2 pins at the top, VCC at the bottom). Add style rules; the gate refuses NEW violations:

- **Power symbols:** a supply symbol (a power symbol whose net isn't a ground) must have its graphic body **above** its pin on the sheet. A ground symbol (net name contains GND) must have its body **below** its pin.
  - Decide from the library symbol's graphics, transformed by the instance's rotation and mirror. Don't decide by rotation number, because imported symbols differ.
  - `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/stylecheck.py` has the pin transform (math: rotate by the angle, then mirror; screen y is inverted).
- **ICs and multi-pin symbols** (more than 3 pins, references U, IC, Q are 3-pin and exempt): flag an instance that is mirrored about x, or rotated 180°, so that its library "top" ends up at the bottom.
  - A deliberate exception can be declared in `intent` as `{"orientation": "<ref>", "reason": "..."}`.
- **Fix helpers:** `s.place_power(net, at)` (correct orientation for the net type) and `s.upright(ref)` (undo a vertical flip, keeping connections through `route` or reporting the pins that need rewiring).

## 3. A tidy top sheet

**Friedl** didn't like where the new Power sheet symbol was placed: a small box alone in the top-left corner, a different size from the MCU and RS485 sheet symbols, and not aligned.

- **Helper `arrange_root(order, size=None, gap=25.4)`.** It places the root's sheet symbols in one row, in the given order (for P0005: Power → Microcontroller → RS485), all the same size (the largest existing, or `size`), on one horizontal line, centred on the page above the title block.
  - It moves each sheet's pins with it, and redraws the wires and buses between sheet pins (use `route`).
  - It keeps every sheet UUID and instance path.
  - The netlist must be unchanged.
- **Add to the board agent's instructions:** after adding or moving a sheet, arrange the root in signal order.

## Tests and gates
- Tests for:
  - the block-scoped label rule, on the evidence sheet: between-block labels allowed, inside-block refused;
  - each orientation rule, using the real P0005 symbols (FireFli_Power 3V3/5V/GND/GND_ISO/V_ISO and U2);
  - `place_power` and `upright`;
  - `arrange_root`: an unchanged netlist, equal sizes, one row.
- Run both law gates and the full suite; it was 5,717 passed, 2 skipped.
- Report results and judgment calls.
