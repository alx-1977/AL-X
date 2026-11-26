# Board jobs: Altium to KiCad with AL/X

18 Nov 2026. Draft for Friedl. Cross out what you never do and add what's missing.

Each job is in Altium's words. The status says whether AL/X can do it in KiCad:

- **Yes:** she can do it today.
- **Partly:** she can do some of it.
- **Build:** KiCad can do it, but her tool can't yet.
- **KiCad limit:** KiCad can't be driven to do it from outside. Getting it would mean a fork
  or a workaround.

## 1. Schematic

| Job (Altium words) | Status | Notes |
|---|---|---|
| Place a part from the library | Partly | Copies an existing part. Placing by part number or value straight from the FireFli library needs building. |
| Swap a part (change component) | Yes | |
| Delete parts | Yes | |
| Wire, net labels, ports, power ports | Yes | In your style: wires for local nets, GND down, power up. |
| No-ERC markers | Yes | |
| Move or align parts on the sheet | Build | Today she only places new parts. |
| Edit parameters (MPN, LCSC, value) on one or many parts | Build | Needs the select-similar-then-change ability (section 4). |
| Hierarchical sheets and buses | Partly | She converted P0009. She can't yet add a new sheet. |
| Annotate | Build | KiCad does it. Not wired in. |
| Run ERC and read it | Yes | |
| Update PCB from schematic (ECO) | Yes | |
| Check a placed circuit against the manufacturer's application diagram | Build | She can fetch and read datasheets, and she checks layouts against reference layouts already. Comparing the schematic part by part and value by value against the app circuit needs building. She shows the datasheet page beside your schematic. |
| Draw a new circuit from the application diagram | Build | Same reading, then she places it with the schematic tools. Values come from the datasheet, and anything unclear goes back to you as a question. |
| SPICE simulation on the schematic | Partly | ngspice runs on a work copy: AC, transient and operating point, plus tuning of R, L and C values. Missing manufacturer models must be supplied. Fetching them and showing plots on screen needs building. |
| Net classes with their rules: impedance, differential pairs, widths, clearances | Build | KiCad has net classes and diff pairs (_P/_N names). Working out the width for an impedance from your stack-up (like the 0.37 mm, about 48 Ω on P0009) is our own calculation, because KiCad's calculator can't be called. The classes then drive routing and DRC. |
| Show wire crossings as a half-circle hop | Build (KiCad setting) | KiCad 10 has it: "Hop-over size" in Schematic Setup. P0009 has it off. In a test, the PDF output didn't draw the hop at any size, so it may show only in the editor. To be checked on screen. |
| Tidy up the schematic: no overlapping text, nothing in the title block | Partly | Text tidy for parts she touches, and new wiring keeps off the title block. A whole-sheet tidy that moves parts and checks the title block area needs building. |


## 2. PCB: parts and placement

| Job | Status | Notes |
|---|---|---|
| Move and rotate parts, flip side | Yes | |
| Align or distribute parts | Build | |
| Swap a footprint, update from library | Yes | Done today on R17, R27 and J2. |
| Add or remove parts on the board only (fiducials, holes, logos) | Partly | Adding works. Logos and pictures can't be done yet. |
| Rooms and component classes | Build | KiCad uses rule areas instead. |

## 3. PCB: copper

| Job | Status | Notes |
|---|---|---|
| Route a connection | Partly | Her own router, in your style: 45-degree bends, RF arcs, bundling. It works for small jobs but can't push and shove like Altium's. |
| Interactive push-and-shove routing | KiCad limit | KiCad's router can't be driven by a program. A fork, or improving her own router, would be the way. |
| Differential pairs and length tuning | Build, or KiCad limit | Measuring length works. Tuning wiggles would have to be our own code. |
| Track width, via size changes, many at once | Build | Select similar, then change (section 4). |
| Vias and stitching vias | Partly | Single vias and stub-plus-via to a plane work. Via fences and stitching rows need building. |
| Polygons: draw, pour, repour | Partly | She can fill solid copper shapes and refill pours. Drawing a new pour needs building. |
| Keep-outs | Yes | |
| Teardrops | Yes | |
| Remove dangling tracks and vias | Yes | |
| Net classes and design rules | Build | She reads them. Changing them needs building. |

## 4. "Find Similar Objects", then the Inspector

This is the single biggest gap. Most of your one-minute Altium jobs are this one move.

| Job | Status |
|---|---|
| All designators to 0.5 × 0.5 mm | Partly: built today, live after the next restart |
| Hide all values | Partly: built today, live after the next restart |
| All GND vias 0.45/0.2 | Build |
| All tracks of a net, or a net class, to a width | Build |
| All 0402 capacitors: set a parameter | Build |
| Move all designators to the top, or centre them on the part | Build |

The plan is one general "select, then change" ability, so every row here is the same single
step.

## 5. Silkscreen and drawing

| Job | Status | Notes |
|---|---|---|
| Move designators off pads | Yes | |
| Move or edit free text (revision, date, labels) | Partly | Editing works. Moving is built today, live after the next restart. |
| Board outline and cut-outs | Build | |
| Title block and revision | Yes | |
| Logos and pictures | Build | |

## 6. Checks

| Job | Status | Notes |
|---|---|---|
| DRC and knowing which warnings matter | Yes | |
| Compare the board with the schematic | Yes | |
| Check a layout against the manufacturer's reference layout | Yes | |
| Check footprints against the datasheet | Yes | |

## 7. Outputs (Output Job)

| Job | Status | Notes |
|---|---|---|
| Gerbers and drill, JLC names, zip | Yes | |
| BOM for JLC (Comment, Designator, Footprint, LCSC) | Build | KiCad can do it. About half a day. |
| Pick and place for JLC, with rotation fixes | Build | KiCad can do it. Rotation offsets to be checked against the last order. |
| Check LCSC parts exist and are in stock | Build | Web lookup per part. |
| Assembly drawing and schematic PDF | Build | KiCad can do it. |
| 3D STEP for enclosure work | Build | KiCad can do it. |

## 8. Libraries

| Job | Status | Notes |
|---|---|---|
| Make a footprint from a datasheet | Yes | |
| Make a symbol | Yes | |
| Push library changes to GitHub | Yes | Asks you first. |

## So far

- **Nothing on this list so far needs a KiCad fork,** except push-and-shove routing.
- Most of the "Build" items are one ability (section 4) plus KiCad's own commands for
  outputs. That's a few days of work, not weeks.

## Your part

Cross out what you never do, and add what's missing, in your own words. Altium terms are fine.
