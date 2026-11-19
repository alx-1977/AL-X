# Board edits — build plan

For the job in BOARD_EDIT_DONE.md. Draft, 14 Nov, for Friedl to agree before I build.

## What she already has

- Her own copy of the project, edited with KiCad's own Python.
- Her screen shows your board on the left and her copy on the right, and she gets that picture back to look at.
- DRC on her copy, datasheet fetch, and a land-pattern check against the datasheet.
- Write-back only with your yes.

## Why she failed yesterday

1. **She never changed the placement.** She kept the parts where they were and routed around them by coordinates.
2. **Nothing made her stop after placement.** She went straight from swapping parts to routing.
3. **She trusted the DRC number.** She didn't judge the picture.
4. **The tools she was missing:** no tool to swap a part, no curved tracks, no RF length check, and nothing for the schematic.

## What I'll build (one PR)

1. **The steps, in a fixed order, enforced in code, not left to instructions** (agreed 15 Nov):
   1. Schematic. Nothing on the board changes until you approve it.
   2. Placement. Parts only: her copy refuses any new track or via until you approve it.
   3. Routing, until you approve it.
   4. Write-back, only after your yes, and only if the board matches the approved schematic pin for pin.
   - If she changes something you already approved, that step and every step after it go back to "not approved".
   - A new board takes the same steps, starting from an empty board.
2. **The placement view.** Parts only, no copper, pin 1 and the pin names visible, so you both judge from where the pins point.
3. **DRC compared to your original.** Every check reports only what is new against your board: new problems, anything unconnected. Not just a total.
4. **The editing tools I used yesterday:**
   - swap a part for one from the library, keeping its connections;
   - curved tracks (bend radius in mm);
   - track length per net, with P/N match for RF pairs;
   - clearing stale teardrops, and finding vias by their real net.
5. **Schematic tools.**
   - Swap, add or remove symbols on her copy.
   - A pin-by-pin comparison of the schematic against the board, across all sheets. This replaces KiCad's own check, which only reads the first sheet.
6. **The DHL freight option** (Freight & Courier, no VAT), added to her DHL importer. It's small, so it rides along in this PR.

## How I'll test it

- I give her your sentence from yesterday on a fresh copy of the original P0009.
- I run it myself until the result is as clean as yesterday's reference, measuring her usage as I go.
- Then you do the same job with her, in your own words.
- Greptile reviews it only after that.

## Not in this round

- Teardrops on the new joints (the reference doesn't have them either).
- Stopping cleanly when her 5-hour allowance runs out. That's a separate fix; until then I'll watch her usage during the test.
