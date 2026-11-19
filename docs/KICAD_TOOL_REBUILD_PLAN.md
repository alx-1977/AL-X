# Rebuilding AL/X's KiCad tool once

18 Nov 2026. A plan for Friedl's yes. It merges my review with Astra 6.2's independent review
(`.alx/reviews/2026-11-18-kicad-tool-review.md`). We agree on the direction. Where Astra
corrected me, its version is used.

## Verdict (both reviews)

- **Keep:** the foundation. Work copies, write-back only after your yes with backups, your
  three approval steps, checks (DRC, ERC, board against schematic), Gerbers, datasheet and
  layout checks.
- **Rebuild:** the editing layer. It grew one P0009 job at a time: 32 separate edit
  instructions (I wrote 37, which was wrong), plus a free-script route beside them.
- **No KiCad fork needed.** The only thing KiCad won't let a program drive is its own
  push-and-shove router. Routing is done by our own router instead.

## Found by Astra that needs fixing first

1. **P0009 silkscreen: the designator line thickness was changed.** Today's 0.5 mm job
   resized the six that needed it, but also changed the line thickness of all 206 designators
   to 0.075 mm. Most were 0.254 mm before. That's on the P0009 project now. It's very likely
   too thin to print, so it must be put back before ordering. The cause is in the tool,
   including the version I built today.
2. **A safety gap:** AL/X's general helper, not just the board agent, can still reach the
   free-script edit route.
3. **Approval can count a view that failed to open,** or a schematic view counted as a board
   view.
4. **A schematic check can miss a broken connection** when the same batch also removes
   something elsewhere.
5. **"Comparison unavailable" can look like "no differences".**

## Redundant code to delete (Astra's list, with file and line in its review)

- The free-script edit route and everything that serves it.
- Three or four different readers for the same schematic files, kept as one.
- Board and netlist facts extracted several times over for each job, built once instead.
- Duplicate copper and field setters, and duplicate cleanup and refill steps.
- Separate save and undo machinery in three places, moved onto one.
- Dead code: unused bookkeeping, an unused agent wrapper, an unused export.
- The instruction manual repeated in four places, generated from one source.

Each deletion goes in the same change as its replacement, with a test that fails if the old
path comes back.

## The new editing model

1. **Read:** filtered and short, never a flood. One address for every object, the same one
   used to select and change it.
2. **Select (Find Similar):** by kind, property, net, sheet, layer, area or any combination,
   with counts.
3. **Change (Inspector):** only the properties named, nothing else. The receipt says, for
   example, "206 matched, 200 already correct, 6 resized, 0 thickness changes".
4. **Engineering operations** built on top: wire-following moves, update PCB from schematic,
   annotation, routing, planes. AL/X makes the choices; code draws, measures and reports
   conflicts.
5. **KiCad's own commands** for checks and outputs: ERC, DRC, BOM, pick and place, PDF, STEP,
   Gerbers.

## Order

1. **Fix the five defects above,** including putting P0009's designator thickness back. Small,
   and can go in on its own.
2. **Shared read model and safe change underneath,** for board and schematic. Delete the
   separate save and undo machinery.
3. **Find Similar and Inspector,** proven on four real jobs:
   - P0009 designators;
   - hide values;
   - GND via size;
   - set a field on 0402 capacitors.

   Delete each old setter as it's replaced.
4. **Drawing, hierarchy, update-PCB and library work** moved over. The free-script route is
   deleted at the end of this step.
   Altium import as an AL/X capability follows `ALTIUM_TO_KICAD_PROCEDURE.md` (P0005).
   Detail below, under "Step 4 design".
5. **JLC outputs:** BOM and pick-and-place, checked against your last order. This can move to
   the front if the P0009 order is urgent.
6. **Hard engineering jobs, routing and RF, each proven separately** on P0009 and then on an
   unseen board.

**Time:** Astra rightly says my "2 + 1 days" covers a prototype, not a finished tool. The first
real milestone is this: you ask for the designator change, get exactly the six right edits
with a report that nothing else changed, and see it in KiCad. Each step after that is proven
before the next.

## Step 4 design (19 Nov 2026, with the Altium import added)

Code does the mechanics and AL/X decides meaning (Laws 1–3). Every step works on a work copy
and is proven by the connection check. Friedl approves the write-back to his folder and to the
library, as now.

**A. Altium import: one action, `convert_import`, in stages.** Each stage runs the library's
`convert/` scripts on the copy and reports what it found and what it changed.

1. **survey** (reads only) reports:
   - pages that came in, and the KiCad same-file bug;
   - the baseline netlist;
   - every difference between the schematic and the Altium board's pad nets, across all
     pages, each with its location (the import breaks);
   - every part classified: passive mapped to a library value, existing library part, new part
     needed, no LCSC number;
   - Altium facts from the PcbDoc: rules, neck width and outline;
   - the dates of the Altium Gerbers against the PcbDoc.
2. **repair_breaks:** AL/X names the breaks to repair. Code applies the fixes that each have
   one correct result: end a wire on the wire it misses, snap an off-grid symbol and its wires,
   move a label onto its wire. The board comparison proves each one.
3. **convert_parts:**
   - Passives are mapped and swapped.
   - AL/X gives the data for each new part: maker, MPN, description, datasheet, LCSC number,
     library, and shared footprint or rename.
   - Code adds the parts to a library work copy, then relinks, updates the board from the
     schematic and refreshes footprints.
   - It refuses if any connection changed.
   - The datasheet check uses the existing `fetch_datasheet` and `check_kicad_layout` on the
     library copy. A new part isn't finished until it passes.
4. **repair_board:** layers, outline from the PcbDoc, via tenting from Altium's mask, zones
   with Altium's neck width, text boxes to text, FireFli font, one hierarchical design with
   sheet pins, and the ALX title block. AL/X gives the project code, title and sheet names.
5. **write_rules:** Altium's Rules6 become the `.kicad_dru`. AL/X adds each as-built exception
   as data: kind, footprints, reason.
6. **prove:**
   - connections against the import and the board;
   - DRC, ERC and parity;
   - Gerbers against the import's (written to scratch, never the project folder);
   - a report.

   Then the existing `write_back` asks Friedl.

**B. Schematic edits as structured operations.** These are the same ones the import and the
ERC tidy-up need:
- add a no-connect mark;
- add a power flag on a short wire to its net's power symbol;
- move a label onto its wire;
- join a wire end;
- snap a symbol with its wires.

Hierarchy (`make_hierarchical`) stays. Update-PCB (link, nets, fields) becomes one operation.

**C. Library work as structured operations on a library copy:**
- add a part from data;
- set a footprint's pads from a `check_kicad_layout` measurement;
- set a catalogue note.

**D. Delete the free-script route** for board, schematic and library once A–C cover the jobs
that used it.

**E. Fourth Inspector job:** a field on the 0402 capacitors.

**Proof:** convert P0005's original import again with AL/X's actions alone. The result must
match today's conversion, apart from file ids.

## What I need from you

1. A yes to this plan.
2. Is the P0009 order urgent? If yes: step 1, then step 5, then the rest.
