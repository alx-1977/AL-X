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

1. **The steps, enforced in code, not left to instructions.**
   - While you haven't approved the placement, her copy refuses any new track or via. She can move, turn, add and remove parts only.
   - Routing opens only after you say yes to the placement. The schematic step opens only after you say yes to the routing.
   - Write-back is refused until you've seen all three: placement, routing and schematic.
   - If she changes something you already approved, that step goes back to "not approved".
2. **The placement view.** Parts only, no copper, pin 1 and the pin names visible, so you both judge from where the pins point.
3. **DRC compared to your original.** Every check reports only what is new against your board: new problems, anything unconnected. Not just a total.
4. **The editing tools I used yesterday:**
   - swap a part for one from the library, keeping its connections;
   - curved tracks (bend radius in mm);
   - track length per net, with P/N match for RF pairs;
   - clearing stale teardrops, and finding vias by their real net.
5. **The schematic step.**
   - Swap or remove symbols on her copy.
   - A pin-by-pin comparison of the schematic against the board, across all sheets. This replaces KiCad's own check, which only reads the first sheet.
6. **The same steps for a new board.** Schematic first, then placement, then routing, with nothing separate to build later.

## How I'll test it

- I give her your sentence from yesterday on a fresh copy of the original P0009.
- I run it myself until the result is as clean as yesterday's reference, measuring her usage as I go.
- Then you do the same job with her, in your own words.
- Greptile reviews it only after that.

## Not in this round

- Teardrops on the new joints (the reference doesn't have them either).
- Stopping cleanly when her 5-hour allowance runs out. That's a separate fix; until then I'll watch her usage during the test.
