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
5. **JLC outputs:** BOM and pick-and-place, checked against your last order. This can move to
   the front if the P0009 order is urgent.
6. **Hard engineering jobs, routing and RF, each proven separately** on P0009 and then on an
   unseen board.

**Time:** Astra rightly says my "2 + 1 days" covers a prototype, not a finished tool. The first
real milestone is this: you ask for the designator change, get exactly the six right edits
with a report that nothing else changed, and see it in KiCad. Each step after that is proven
before the next.

## What I need from you

1. A yes to this plan.
2. Is the P0009 order urgent? If yes: step 1, then step 5, then the rest.
