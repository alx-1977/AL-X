# D-084a implementation report

The board agent can now run a bounded Python script through `kicad edit` on its registered work copy. A drawing edit declares the connections and parts it intends to change. Before keeping it, the provider checks the result against the immediately preceding revision. A refusal names the failed conditions and restores the snapshot. A dry run returns its findings and restores the snapshot even when the checks pass.

## Changed files

Paths in the first column are relative to the repository root.

| File | Change |
| --- | --- |
| `src/alx/tools/kicad.py` | Removed the `change` and `checks` inputs. Defined `intent`, documented the helpers, and returned ordered image digests for kept edits. |
| `src/alx/tools/board_agent.py`, `src/alx/specialists/board_agent.py` | Allowed scripts on the existing edit capability. Instructed the agent to read the FireFli standard, use the block library and inspect each crop. |
| `src/alx/tools/worker.py` | Updated the explanation of the general worker's existing script restriction. |
| `src/alx/providers/kicad.py` | Integrated the gate, preview and drawings into the locked work-copy transaction. Checks include parts, pin connectivity, changed sheets, ERC, DRC and optional selected-object properties. Reused PDF export and flattened derived library symbols, including nested inheritance. |
| `src/alx/providers/kicad_change.py`, `src/alx/providers/kicad_index.py` | Removed operation dispatch. Validated declared postconditions and compared full pin groups, part identities and selected object properties. The netlist index now includes library IDs and exported sheet-instance paths. |
| `src/alx/providers/kicad_board_helpers.py`, `src/alx/providers/kicad_helpers.py` | Exposed board operations as callable script helpers, retained the foreign-copper clearance check, and made runner input safe for non-ASCII part data. |
| `src/alx/providers/kicad_schematic_helpers.py` | Added sheet creation, region moves with boundary reporting, and layout copying with a reference map. Moves retain symbol UUIDs and repair instance paths. Copies retain target symbol identities and values. |
| `src/alx/providers/kicad_style.py` | Ported FireFli's pin-to-wire and junction-dot checks. Each new violation identifies the sheet, position and affected pins or wire. |
| `src/alx/providers/kicad_drawing.py`, `src/alx/bootstrap/kicad.py`, `requirements.txt` | Rendered changed files, cropped pixel differences with a margin, capped the long side at 1,600 px, and stored the PNGs in the existing image store. Added Pillow. |
| `src/alx/contracts/models.py`, `src/alx/core/model_reasoner.py`, `src/alx/specialists/worker.py` | Loaded ordered image bytes into the next model request, preserving existing single-image results. |
| `tests/test_board_agent.py`, `tests/test_kicad_change.py`, `tests/test_kicad.py` | Tested script access, image delivery, gate refusals and restoration, warnings, stale selections, previews, cropping and rejection of the old input. |
| `tests/test_kicad_schematic_helpers.py`, `tests/test_kicad_copper_helpers.py` | Exercised the new helpers with KiCad, including netlist equality after a region move, twin-block values, derived symbols and board operations. |
| `tests/test_kicad_layout_check.py`, `tests/test_kicad_real.py`, `tests/test_kicad_roomreader.py`, `tests/test_kicad_round_trip.py` | Migrated edits to the script contract and verified that new DRC errors are refused at edit time. |

## D-084 operation mapping

`s` is the sheet returned by `sheet(file)`. `b` is the board returned by `load()`. A script saves its changes with `s.save()` or `save(b)`.

| Schematic operation | Script equivalent |
| --- | --- |
| `remove_symbols` | `s.remove_symbol` for each reference, including its cleanup of loose ends |
| `swap_symbol` | `s.swap_symbol`, then `s.tidy` and `s.tidy_fields` as needed |
| `add_symbol` | `s.copy_part`, then `s.swap_symbol` when a different library part is required |
| `wire` | `s.wire_pin_to` using the destination pin coordinates from `s.symbol` |
| `label` | `s.label_pin` |
| `power` | `power_template` and `s.port_pins`, or `s.ground_pin` / `s.power_pin` |
| `no_connect` | `s.add_no_connect` at the pin coordinates |
| `tidy_fields` | `s.tidy_fields` |
| `remove_labels` | `s.remove_labels` on each applicable sheet |
| `remove_items` | `s.remove_items` |
| `remove_dangling` | `s.remove_dangling` |
| `unwire` | `s.unwire` |

| Board operation | Script equivalent |
| --- | --- |
| `remove_parts` | `remove_parts` |
| `replace_part` | `replace_part` |
| `add_part` | `add_part` |
| `place` | `place_part` |
| `track` | `add_track`, using `pad` and `xy` for pin endpoints |
| `via` | `add_via` |
| `remove_tracks` | `remove_tracks` |
| `remove_teardrops` | `teardrops` to select, then `remove` for each |
| `remove_dangling_tracks` | `remove_dangling_tracks` |
| `sync_from_schematic` | `sync_from_schematic` with required library footprints supplied to the edit |
| `keepout` | `add_keepout` |
| `remove_keepouts` | `remove_keepouts` |
| `copper_shape` | `add_copper` |
| `route` | `route_connection` |
| `to_plane` | `to_plane` |
| `remove_copper_shapes` | `remove_copper_shapes` |
| `add_teardrops` | `add_teardrops`, then `refill` |
| `set` | `set_properties` with IDs from `select`. A board `set` declaration verifies the properties at the selected revision. |
| `title_block` | `set_title_block` |

## Verification

- `python3 scripts/check_governance.py`, `python3 scripts/check_architecture.py`, and `git diff --check` all passed.
- `python3 -m pytest -n 6 -q -rs` completed in 140.77 seconds with **5,701 passed, 2 skipped, and 11,319 passing subtests**. The skips were the missing RoomReader antenna before-stiffener fixture and the opt-in live conversation test.
- Real KiCad 10.0.8 tests verified that a declared connection was kept, an unmet connection and an undeclared connection were rolled back with pin names, and a new missing junction was reported with coordinates. The sheet move preserved the exported netlist and moved-symbol UUIDs. New ERC-error rollback and warning retention were also exercised with controlled ERC reports.
- Through the production capability, I added a small text note to the Power sheet on a disposable P0005 copy and the Charger sheet on a disposable P0009 copy. Each returned a **179 × 72 px PNG**, zero unmet conditions and zero new ERC or DRC findings. KiCad used `/opt/homebrew/bin/pdftoppm` for rasterization. Both source projects were read-only inputs.

## Implementation decisions and acceptance

- The port uses AL/X's existing KiCad s-expression reader and implements the two rules in FireFli's checker. Drawing quality beyond those rules remains a visual judgment.
- A new warning is returned to the agent. A new ERC or DRC error refuses the edit. KiCad can report an undriven power net at different pins on successive runs. Where the exported net identifies that error, the comparison counts findings by net while preserving the original marker in the report.
- The changed-area crop includes both ends of a move. A file without a visible pixel difference returns a scaled drawing. An unavailable renderer produces a per-file diagnostic. Both native subscription providers already accept tool-result images, so the implementation needed no D-083a permission amendment.
- A region move requires one identifiable source and destination sheet instance. It reports crossing geometry and shared local labels for the script to resolve. Twin-layout copying requires an explicit one-to-one reference map, including power symbols.
- The full P0005 RS485 redesign by a live board agent, its usage measurement, and Friedl's visual acceptance in KiCad remain outstanding. The project checks above exercised the production edit capability on disposable copies.
