# D-084a live-test fixes

Implemented the five fixes on `board-edit-capability` from `a468c72`. The work is in the working tree.

| Fix | Implementation and regression evidence |
| --- | --- |
| ERC only-new comparison | The edit gate and original-baseline report share a signature based on check type, description and the sorted multiset of symbol references and pin numbers. Occurrence counts preserve genuine duplicates. ERC reports still carry the original locations and item details. Undriven power nets retain net-based matching when the exported netlist identifies the net. Existing work copies can still read their saved ERC baseline. |
| Sheet split | `split_sheet(source_file, region, new_name, new_file, keep="remaining")` shares the existing region-transfer operation. With `keep="moved"`, the selected region stays in the original file under the original sheet symbol and instance path. `new_name` and `new_file` name the sibling containing the remainder. With `keep="remaining"`, the new sibling takes the selected region. The helper reports crossing geometry and shared local labels for the script to resolve. |
| Project lookup | A bare project folder name resolves under the configured AL/X Projects root. Folder lookup prefers a unique board under `hardware/kicad`, including a nested KiCad project folder, and skips backups. Ambiguity reports the candidate PCB paths. The `~/ALX_Storage` alias uses the same storage resolution as reference reads. |
| Script helper documentation | The `kicad edit` contract lists schematic helper signatures and return shapes. `print(help("split_sheet"))` and `print(help("Sheet.symbols"))` return the helpers' docstrings inside the existing script runner. |
| Scoped schematic reads | `inspect_schematic` accepts `sheet` with an optional rectangle in millimetres, an exact `net` for membership, or an exact `ref` for a symbol's pins and connections. Region selection uses symbol anchors and label positions. Existing overview, plural filters, and raw evidence reads remain available. |

## Evidence from job 8ab320b33638

The first 150 calls included 84 `read_reference` calls. Forty read the FireFli library's standalone `RS485_Activity_LEDs.kicad_sch` block template, 39 with an offset. Those were evidence reads outside the checked project. The new project selectors do not replace those calls. Several of the eight existing `inspect_schematic` requests fetched broad project views. Narrower replies reduce context for those requests and can answer a specific region, net or symbol in one call. A reduction in project-read calls has yet to be measured.

The log shows eight failed project lookups before the agent reached the canonical folder and eight dry-run scripts in the first stretch that inspected signatures, source, or the private sheet tree. Resolving the first project lookup could have avoided the eight failed attempts, and the helper descriptions could have avoided up to eight discovery previews where they answer the same question. These estimates use the recorded calls rather than a rerun. The sheet helper also replaces the manual UUID and file-reference rewrite in call 150.

## Law compliance

The production edit outcome remains `kicad edit` → `KiCadProvider.edit` → the confined script runner and `_edit_gate` inside the existing snapshot and rollback transaction. `split_sheet` reuses the transfer mechanics of `move_region` and the sheet creation mechanics of `new_sheet` within that same edit entry point. Schematic reads still dispatch to `KiCadProvider.inspect_schematic`. Project selection resolves structured folder names in `_project`. The production entry-point and dispatcher search found no superseded routes.

The changes introduce no new registered capability, raw-language routing, frontend authority, goal state, or approval path. The agent still chooses the region, the preserved instance, and how to handle crossings. The existing edit gate, restart-safe work-copy state, approval order, and write-back boundary apply. No law exception was needed.

## Verification

- Both law gates passed, along with `git diff --check`.
- KiCad 10.0.8 verified the P0005 two-region split in a disposable work copy. The original had two `pin_to_pin` errors. Splitting the main region with `keep="moved"` and transferring the separate monitor region back with `move_region` produced no new ERC findings against either the immediate snapshot or the original baseline. The edit gate reported no unmet checks. The dry run restored the starting revision.
- Real KiCad regression tests cover both split directions, the pre-existing Open collector/Power output conflict, unchanged netlist pin groups, valid exported instance paths, and preservation of the original sheet symbol and all symbol UUIDs. Other regressions cover multiset counts, undriven-power marker movement, crossing reports, folder ambiguity, the script help contract and scoped reads.
- Read-only P0005 checks resolved both the bare name and the storage alias to `WEG_RS485.kicad_pcb`. A scoped U3 read returned one symbol with coordinates for all 11 pins. A separate exact-net read returned that net's 11 members. A rectangle on `WEG_RS485_2.kicad_sch` returned three parts from that sheet.
- `python3 -m pytest -n 6 -q -rs` passed with **5,707 tests**, **11,321 subtests**, and **2 skips** in 191.35 seconds. The skips were the missing RoomReader antenna before-stiffener fixture and the opt-in synthetic conversation sanity check.

The P0005 activity circuit redesign and live-agent call savings were outside this replay. The tests used work copies and fixtures. There was no project write-back, commit, push, pull request, or review request.
