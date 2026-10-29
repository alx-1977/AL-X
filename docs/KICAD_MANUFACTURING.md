# KiCad manufacturing export

`export_kicad_manufacturing` exports a registered, untainted work copy through
`KiCadProvider.export`. Gerbers use `kicad-cli pcb export gerbers
--board-plot-params`: the board's saved plot settings control selected layers,
Protel extensions and other Gerber options. To change that selection, edit the
work copy's plot settings with `edit_kicad_work_copy` before exporting.

The optional boolean `separate_drills` defaults to `false`. Set it to `true` to
pass `--excellon-separate-th` to `kicad-cli pcb export drill`, producing separate
plated and non-plated Excellon files instead of one merged file.

`output_dir` remains relative to the original project. `renames` accepts exact
generated filenames or board layer names, including layers exported with Protel
extensions. Ambiguous names and collisions return failures. Publication remains
add-only: existing targets are never overwritten. `zip_name` optionally bundles
the final filenames. `compare_with` compares those filenames against an earlier
zip or directory using the existing timestamp and generator-comment normalization;
drill geometry and plating attributes remain significant.

The opt-in `tests/test_kicad_roomreader.py` reads its external board, accompanying
project and production zip in place. It skips unavailable inputs or KiCad, uses
only temporary work copies and exports, derives geometry from the reference at
runtime, and checks original sizes and mtimes. No external design fixtures are
stored in the repository.
