# Altium schematic editor: everything it does, and AL/X in KiCad

18 Nov 2026. Draft for Friedl.

This is the full feature set of Altium's schematic editor, so AL/X can do all of it for you
and we stop patching one gap at a time. You won't use the editor. Each row is something you
might ask her for. Editor gestures (drag, copy-paste) are written as the result she
has to produce, because she draws and you don't.

I wrote this from my knowledge of Altium and KiCad 10. Rows marked "check" I'll confirm on
your installed KiCad before building.

- **KiCad:** whether KiCad itself can do it. "Our own" means we'd have to build it ourselves.
- **AL/X:** whether she can do it today. **Yes**, **Partly** or **Build**.

## Summary

- 77 features in 12 groups.
- **Yes today: 18. Partly: 12. Build: 45. Skip: 2.**
- KiCad can do almost all of them itself. **Only about 8 have no KiCad equivalent:** compile masks, signal
  integrity, supplier search, part lifecycle, ActiveBOM-style sourcing, design compare,
  harness definitions in full, and managed components. For those we'd write our own or skip.
- **No fork is needed for the schematic.**

## 1. Placing things on the sheet

| Altium feature | KiCad | AL/X |
|---|---|---|
| Place part from library | Yes | Partly: copies an existing part; by part number from the FireFli library needs building |
| Place part from manufacturer search | No, but our own via LCSC | Build |
| Wire | Yes | Yes |
| Bus and bus entry | Yes | Partly: used on P0009, not drawn new yet |
| Net label | Yes | Yes |
| Power port (GND, 3V3 ...) | Yes | Yes |
| Port, off-sheet connector | Yes (global and hierarchical labels) | Yes |
| Sheet symbol and sheet entries | Yes | Partly: converted P0009, can't add a new sheet |
| Signal harness, harness connector | Partly (bus groups) | Build |
| No ERC marker | Yes | Yes |
| Junction (automatic) | Yes | Yes |
| Text, note, text frame | Yes | Build |
| Lines, boxes and arcs (graphics) | Yes | Build |
| Image or logo on the sheet | Yes | Build |
| Parameter Set directive (PCB rules from schematic) | Yes (net class labels, rule areas) | Build |
| Differential Pair directive | Yes (_P/_N names) | Build |
| Blanket (directive over an area) | Yes (rule area) | Build |
| Compile Mask (ignore an area) | No | Skip |

## 2. Editing what's there

| Altium feature | KiCad | AL/X |
|---|---|---|
| Move a part or a group; its wires follow (for layout and tidying) | Yes | Build |
| Rotate, mirror | Yes | Build |
| Delete | Yes | Yes |
| Duplicate a circuit, with new designators | Yes | Build |
| Repeat a circuit N times with numbered names (CH1, CH2 ...) | Partly | Build |
| Align and distribute | Yes | Build |
| Add a part into an existing line (e.g. a ferrite on 3V3), or remove one and close the line | Yes | Partly: remove and redraw only |
| Change part (swap symbol) | Yes | Yes |
| Edit a part's parameters | Yes | Build |
| Text size and visibility (designators, values, notes) | Yes | Build |
| Go back to an earlier version | Yes | Yes: any earlier revision of her copy |

## 3. Find Similar and the Inspector

| Altium feature | KiCad | AL/X |
|---|---|---|
| Find Similar: pick all parts alike (same value, footprint, type ...) | Partly (selection filter and search) | Build |
| Inspector: change many at once | Yes (properties panel) | Build: the "select, then change" ability |
| Find and replace text | Yes | Build |
| Pick by net, sheet or area | Yes | Build |

## 4. Parameters and footprints across the design

| Altium feature | KiCad | AL/X |
|---|---|---|
| Parameter Manager (table of every part's fields) | Yes (Symbol Fields Table) | Build |
| Footprint Manager (assign or change footprints) | Yes (Assign Footprints) | Partly: swaps footprints on the board |
| Update from library | Yes | Partly: footprints yes, symbols to build |
| Item Manager (sync symbols to library) | Yes | Build |

## 5. Annotation

| Altium feature | KiCad | AL/X |
|---|---|---|
| Annotate schematic | Yes | Build |
| Board-level annotation (positional designators) | Partly (geographic re-annotate on the PCB) | Build |
| Back-annotate from the PCB | Yes | Build |
| Reset or renumber designators | Yes | Build |

## 6. Hierarchy and multi-channel

| Altium feature | KiCad | AL/X |
|---|---|---|
| Hierarchical sheets | Yes | Partly |
| Create, rename or delete a sheet | Yes | Build |
| Multi-channel (repeat a sheet N times) | Yes (repeated sheet instances, multichannel tool on the PCB) | Build |
| Device sheets and snippets (reusable blocks) | Yes (design blocks) | Build |
| Net identifier scope (flat, hierarchical, global) | Fixed rules | Yes: she works with KiCad's rules |
| Port cross-references | Yes (inter-sheet references) | Build |

## 7. Libraries

| Altium feature | KiCad | AL/X |
|---|---|---|
| Make a symbol | Yes | Yes |
| Multi-part symbols (U1A, U1B) | Yes | Build: check |
| Make a footprint | Yes | Yes |
| 3D model on a footprint | Yes | Build |
| Database library with supplier fields | Yes (database libraries) | Partly: MPN and LCSC fields on FireFli parts |
| Managed components, lifecycle, supplier data | No | Our own: LCSC lookup for stock and price |
| Pin and gate swap | check | Build |

## 8. Compile and checks

| Altium feature | KiCad | AL/X |
|---|---|---|
| Compile project, ERC with connection matrix | Yes | Yes |
| Explain and fix ERC errors | n/a | Partly: she explains; fixing is per case |
| Net classes from buses or sheets | Yes | Build |
| Component classes and rooms | Partly (rule areas) | Build |
| Cross-probe schematic to PCB | Yes (live) | Build: needs her live in KiCad |
| Navigator (find a net or part across sheets) | Yes | Yes: inspect_schematic |
| Net colours: colour nets so you can follow them when viewing | Yes | Build |

## 9. Schematic to PCB

| Altium feature | KiCad | AL/X |
|---|---|---|
| Update PCB (ECO) with preview | Yes | Yes |
| Keep PCB work during updates | Yes | Yes |
| Back-annotation of pin swaps | Yes | Build |
| Design Compare (two revisions) | No | Our own: Build |

## 10. Simulation and analysis

| Altium feature | KiCad | AL/X |
|---|---|---|
| Mixed-signal SPICE (AC, transient, DC, sweeps) | Yes (ngspice) | Partly: runs; models and plots to build |
| Monte Carlo, parameter sweeps | Yes | Build |
| Signal integrity | No | Skip, or our own later |

## 11. Variants and documentation

| Altium feature | KiCad | AL/X |
|---|---|---|
| Assembly variants (fitted, not fitted, alternate values) | check (KiCad 10 may have them) | Build |
| Sheet templates and title blocks | Yes | Partly: board title block yes, sheets to build |
| Special strings (revision, date, project fields) | Yes (text variables) | Build |
| Wire crossing hops | Yes (KiCad 10) | Build: check it shows |
| Everything on the grid | Yes | Yes |

## 12. Outputs

| Altium feature | KiCad | AL/X |
|---|---|---|
| Smart PDF of the schematic | Yes | Partly: renders for viewing; release PDF to build |
| BOM, including ActiveBOM-style supplier check | BOM yes, supplier check our own | Build |
| Netlist | Yes | Yes |
| Output Job (all outputs in one go) | Partly (KiCad 9+ jobsets) | Build |

## Proposed approach

Build it in rounds by group. Each row is tested on a real board before you see it. The first
round would be groups 2 and 3, editing plus Find Similar and the Inspector, because almost
everything else uses them.
