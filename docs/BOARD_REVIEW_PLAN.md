# Board review: plan for PR 1

Draft for Friedl, 13 Nov 2026. Nothing is built yet.

## Goal

AL/X can't show you a layout until the tool has checked it. The tool does
the measuring and comparing. She can't skip a step, and she can't type in a
number she didn't measure.

## The five checks

### 1. Layout against the datasheet drawing

The numbers on a manufacturer's drawing are real text in the PDF, with
their position on the page. On Würth's AE2 drawing (page 2), that is every
value from 0.4 to 4.9.

1. **The tool lists every number on the drawing page.** None can be left
   out.
2. **AL/X says what each number measures on the board**, using names such
   as *AE2 pad 3 left edge → C64 pad 1 centre, along x*, or *feed track
   width*. A number that doesn't apply (a part value such as 1.8 pF, or a
   tolerance) gets a short reason.
3. **The tool measures the board itself** and compares it with the drawing.
   The allowance is 0.05 mm.

The result for each number is one of:

- matches (green)
- differs (red, with both values)
- not yet accounted for (amber)

There is no green verdict while anything is red or amber.

What she says each number measures is saved in the project folder, so it
is worked out once per part and reused on every later review. **You check
it once**, on screen against the drawing, the first time.

### 2. Footprint against the land pattern

This is the same method, applied to the land-pattern drawing (usually page
1). The tool measures the footprint's own pads: sizes, pitch and pad
count. This would have caught AE2's two missing pads.

### 3. Your taper rule

The automatic check from last week, finished. It also covers the three
cases the review found it missed:

- a wide track overlapping a pad without ending on it;
- a step hidden by a wide strip on another side of the pad;
- copper covering only part of a pad's edge.

The check runs after every edit, and the screen shows any steps it finds.

### 4. Schematic check (ERC)

KiCad's own electrical-rules check on the schematic, run on her copy.
Findings are grouped the same way as the board DRC.

### 5. Reply limit

The 50-word limit applies only when you haven't asked for detail.

## How it looks on screen

This keeps today's layout: your board on the left, her copy on the right.
The side column gains one block per datasheet:

```
CHECKED AGAINST 7488912455 p.2
  AE2 pad 3 → C64           1.94 / 1.94
  C62 → C64 pitch           0.75 / 1.35   ← red
  feed width                0.50 / 0.50
  2 numbers not accounted for             ← amber
```

Clicking the block opens the datasheet page. Each number is boxed: green
where it matches, red where it differs, amber where it isn't yet accounted
for. Nothing else is added.

## Decisions for you

1. **Match allowance.** This is how far a measurement on the board may be
   from the datasheet number and still count as matching. For example, the
   datasheet says 1.94 mm and the board measures 1.96 mm, which is 0.02 mm
   off. Within 0.05 mm is green; further off is red. *Decided.*
2. **Who confirms what each number means.** For each number on a drawing,
   AL/X decides what it measures. For example, "1.94 is the distance from
   AE2 pad 3 to C64 pad 1". If she gets that wrong, the tool measures the
   wrong thing and can show green on a real error. So the first time a part
   is checked, she shows the drawing with each number labelled with her
   reading, and you glance at it and say yes or correct it. The open
   question was whether to do that for every part with a manufacturer
   layout, or only for antennas and RF parts. *Decided:* you confirm only
   for RF and high-speed analog parts. Other parts are checked on her
   reading alone. The footprint check (2) still runs on every part.
3. **ERC.** Errors and warnings both, aiming for a clean result like the
   DRC. *Decided.*
4. **How a review ends.** On screen, plus a short written report saved in
   the project folder. *Decided.*

## Order of work

1. You answer the four decisions above. *Done, 13 Nov.*
2. I build all five checks on one branch.
3. I test them on the scanner board, where we know the real errors:
   - the AE1 step;
   - AE2's missing pads;
   - C62 and C66 placed off Würth's drawing;
   - the AE2 pin swap.

   Every one must be caught before any review is spent.
4. One PR, one review request.

Library work through the tool (PR 2) and thermal simulation stay parked
until this is done.
