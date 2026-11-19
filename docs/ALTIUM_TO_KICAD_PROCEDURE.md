# Altium project to FireFli KiCad: the procedure

Proven on P0005 (Teralec WEG_RS485), 19 Nov 2026. This is the order the work was done in.

- **Who does what:** Friedl's only part is the KiCad import (step 0). AL/X does everything
  after it.
- **Scripts:** they live in `FireFli-KiCad-Lib/convert/`. Their README section "Converting an
  Altium project" gives each one's arguments.
- **KiCad's Python:** scripts that open a board run with it:
  `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3`
- **Status:** this is the specification for AL/X's import capability (step 4 of
  `KICAD_TOOL_REBUILD_PLAN.md`).

## Rules for the whole job

- **Check outcomes, never files.** Never compare symbols or files byte by byte. A full
  `check.py` run takes about 5 seconds.
- **Check connections after every stage, two ways:**
  - against the import's netlist;
  - against the Altium board's pad nets, on **every page**. `check.py` reads only the first
    page.

  The board is the truth for connections. A difference you can't explain stops the job.
- **Fix library parts in the library, never on the board.**
- **Leave DRC until the parts, layers and outline are right.** Earlier steps change the errors.
- **Work on a copy.** Write back only at the end, after a backup, and with no KiCad lock files
  (`~*.lck`) in the folder.
- **Script every step** in a run script, so the whole conversion can be rerun from the import
  in a minute after any fix.

## 0. Import (Friedl)

In KiCad: File → Import → Non-KiCad Project, choose the `.PrjPcb`, and save into
`hardware/kicad/`.

## 1. Check every page came in

KiCad 10.0.8 bug: an Altium project whose sheets sit side by side, joined by ports and
harnesses rather than sheet symbols, imports as several top-level sheets that all get **the same
file name**. On save, the last page overwrites the others.

Detect it with any of these:
- two `top_level_sheets` in the `.kicad_pro` with the same `filename`;
- the netlist has the same parts under two sheet names;
- board footprints have no symbol. On P0005, 30 of 90 had none.

Fix it with the import still open in KiCad:
1. For each page except one: select it, then File → **Save Current Sheet Copy As…**
   `<page>.kicad_sch`.
2. Close without saving.
3. Point each `top_level_sheets` entry at its own file. Use the uuid from that file's symbol
   `instances` paths.
4. Check that the schematic part count equals the board's footprint count.

kicad-cli can't read `.SchDoc` files, so this step needs the KiCad window.

## 2. Baseline

1. Copy the import to a work folder.
2. Run `check.py` on it.
3. Export its netlist.
4. Compare its schematic against the board's pad nets across all pages. Every difference is an
   **import break**.

## 3. Repair import breaks

Altium coordinates that are 0.01 mm off leave KiCad connections open. P0005 had three:
- R27 sat 0.15 mm off grid, and its wire stopped 0.01 mm short of the switch line;
- J14 pin 60's wire sloped 0.01 mm, so its label didn't sit on it;
- the D_P label sat 0.01 mm off its wire.

For each break:
- repair it in a small script: snap to the grid, end the wire on the line, add the junction;
- prove it with the board comparison;
- record it in the report.

A difference that is a numbering quirk is handled with the parts instead. Example: an M.2
screw pad numbered "8", like a signal pin.

## 4. Parts to FireFli parts

1. **Resistors and capacitors:** run `map.py`, then `swap.py` (board) and `schswap.py`
   (schematic).
   - Resistors share one symbol drawing (R), capacitors one (C), with one footprint per size.
   - A missing value is added through `generator/resistors.py` (or the capacitor generator).
2. **Other parts:**
   1. Add each to `parts.py`: maker, MPN, description, datasheet and **LCSC number**.
   2. A reel variant that uses an existing part's footprint goes in `SHARED_FOOTPRINT`.
   3. A project name that differs from the library's goes in `RENAMED`.
   4. Run `extract.py <project>` to create the parts from the import.
   5. Run `relink.py <project>` and `relink_pcb.py <board>`.
3. **Parts found in a design later (not at import)** are built from the datasheet only, with
   `add_part` on a `copy_library` work copy:
   - the symbol from the pin table (one unit);
   - the footprint from the land-pattern figures;
   - never copied from KiCad's or anyone's library.

   `find_part` comes first, to avoid duplicates. A write-back refuses any part new to his project
   that is not a FireFli library part, and every edit lists such parts as it happens.
4. **Datasheet check: a new part is not done until this is.**
   - Compare each new footprint with the datasheet's **recommended land pattern**: pad size,
     pitch, pin 1, exposed pad and its net, holes, polarity.
   - Change it in the library where it differs.
   - Where the datasheet has no land pattern, check against the package drawing.
   - Where we keep a footprint different from the datasheet on purpose, the reason goes in the
     catalogue Note. Example: 18024115401L keeps 8.1 mm across its isolation barrier.
5. **Check connections now.** If a library symbol's pin order differs from the project's,
   relink's joining wires can short pins: P0005's regulators had ENABLE shorted to 5V. The fix
   is a library part that uses the project's own symbol.
6. **Report gaps:** list every part without an LCSC number for Friedl. Don't choose substitutes.

## 5. Board follows schematic

1. Run `link_pcb.py`, `sync_nets.py` and `sync_fields.py`. Together they do KiCad's Update PCB.
2. Refresh the board footprints of relinked parts from the library with `swap.py`, using a map
   of those parts.
   - `swap.py` checks pad numbers.
   - It ignores the unnumbered fragments Altium leaves in custom pads.
   - A renumbered pad (Altium "8" to library "MP") is renumbered on the board first, keeping its
     net, and its pin is wired on the schematic.

## 6. Board: layers, outline, mask, copper

1. `fix_layers.py`: KiCad's standard layer names.
2. `outline_from_altium.py <board> <PcbDoc>`: rebuilds the outline and cut-outs.
3. `via_tenting.py <board> <Altium outline gbr> <top mask gbr> <bottom mask gbr>`.
4. `fix_zones.py <board> --neck <mm>`, where `<mm>` is Altium's NECKWIDTHTHRESHOLD from the
   PcbDoc's Polygons6 stream (P0005: 0.15). Without it, KiCad's 0.25 mm minimum cuts thin copper
   off DFN pins.
5. `clip_pours.py`: only if Altium kept pours further from the outer edge than from cut-outs.
6. `fix_courtyards.py` for malformed courtyards. Footprints refreshed from the library usually
   need none.

## 7. Silkscreen and FireFli standards

1. `textbox_to_text.py`.
2. `designator_font.py`: Roboto SemiBold Italic, 0.5 mm minimum.

## 8. One design

1. Run AL/X's `make_hierarchical`, then `connect_with_sheet_pins`.
2. Check that the netlist is identical.
3. Re-run `sync_nets.py` and `sync_fields.py`.

Only after this does DRC's schematic parity check work.

## 9. Rules file

Write the PcbDoc's Rules6 as `<project>.kicad_dru`, with a comment giving the reason for every
rule (pattern: P0005 and P0020).
- Clearance ignores pads within one footprint when Altium's rule did. That covers USB-C pads
  next to their own locating holes.
- No hole-to-copper rule unless Altium had one.
- Copper-to-edge clearance comes from Altium's BoardOutlineClearance.
- Mask bridges inside fine-pitch parts are kept as built, after checking that Altium's mask
  Gerber had them too.
- Silkscreen drawn on purpose is kept as drawn, naming the footprints.

## 10. DRC and ERC clean-up

**DRC:** every error is fixed or covered by a rule with its reason.
- Move overlapping designators with `nudge_designators.py`.
- Remove a "dangling" via or track only if it truly touches nothing: no track, pad or fill on
  any layer.
  - P0005's three 4V2 vias touched top-layer copper, so they stayed.
  - Its zero-length track lay inside the tracks meeting there, so it went.

**ERC:**
- add no-connect marks on the pins the board leaves unconnected;
- add power flags in an empty corner, each on a short wire to its net's own power symbol;
- fix labels that sit 0.01 mm off their wire.

Any remaining ERC error is reported as a design question, not hidden. P0005: U1/U3 PG pins tied
to GND.

## 11. ALX title block

Run `title_block.py <project> <Pxxxx> "<Title>" [<sheet file>=<name> ...]`. It:
- removes Altium's drawn title box;
- points the project at the master `${FIREFLI_KICAD_LIB}/templates/ALX-Precision-A3.kicad_wks`;
- fills the fields as P0020's: `Pxxxx-SCH-<SHEET>`, `Pxxxx-PCB`, F. Basson, DRAFT, and the sheet
  names;
- sets the board page to A3.

Then look at every page for drawing the block now covers. Friedl decides how to re-lay a page.

## 12. Proof

- **Final `check.py`:** DRC with no errors, nothing unconnected, parity clean.
- **Netlist:** identical to the baseline, except for the recorded import repairs and pin changes.
- **Gerbers:**
  - Export KiCad's Gerbers to a scratch folder, **never into the project folder**.
  - Compare them with the import's own Gerbers. Every difference must be a replaced footprint.
  - Compare with Altium's `Project Outputs` only if they are newer than the PcbDoc. P0005's
    were two months older.

## 13. Deliver

1. Back up the import to `hardware/kicad/backups/<date>-import/`.
2. Write the result into `hardware/kicad/`, with no production files there.
3. Run DRC on the folder itself.
4. Commit the library parts in FireFli-KiCad-Lib. Ask Friedl before pushing.
5. Give Friedl a short report: what was repaired, the questions, the warnings kept and why.

Design validation is a separate job afterwards: checking the schematic, then the board, against
datasheets. Passing ERC is not enough.
