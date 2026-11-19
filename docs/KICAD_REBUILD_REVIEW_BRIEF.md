# Brief: review of KiCad tool rebuild, steps 1 to 3

For Codex Astra, 18 Nov 2026. **Findings only. Do not change any files.**

You reviewed this tool earlier today (`.alx/reviews/2026-11-18-kicad-tool-review.md`). Friedl
approved a rebuild plan based on that review: `docs/KICAD_TOOL_REBUILD_PLAN.md`. Steps 1, 2,
3 and 5 are now built on branch `board-edit-capability`. Please review that code critically.

## What to review

Read `AGENTS.md` and `LAWS_OF_ALX.md` first. The commits are on `board-edit-capability`; use
`git log --oneline 50ab9c8..HEAD` and `git show <sha>`.

| Commit | What |
|---|---|
| 6b092cb | Step 1: the five defects from your review |
| 80918ab, 0ce2575 | Step 5: JLC BOM and positions; export from storage under `.alx` |
| fa4838c | Step 2a: `KiCadProvider._change`, one snapshot, taint, history and restore for edit, make_hierarchical, take_edits, revert |
| 20ca909 | Step 2b: `kicad_index.board_index` and `select`, `netlist_model`; `_netlist` and `_board_index` caches |
| e58d621 | Step 3: `kicad select` action and edit's `set` operation (Find Similar plus Inspector), with `set_check` receipts |

Main files:
- `src/alx/providers/kicad.py`
- `src/alx/providers/kicad_index.py`
- `src/alx/providers/kicad_change.py`
- `src/alx/providers/kicad_helpers.py`
- `src/alx/providers/kicad_assembly.py`
- `src/alx/tools/kicad.py`
- the matching tests

## Questions

1. **Correctness.** Bugs, unsafe edge cases, or places where a failure could leave her work
   copy, history or his project wrong. Pay particular attention to:
   - `_change` (restore, taint, evidence snapshot, preview);
   - the netlist and index caches (staleness: what the cache key misses);
   - `set` selection and `set_check` (what it fails to catch);
   - the JLC rotation and offset maths.
2. **Law 0.** Did each replacement delete what it replaced? List any remaining competing
   path, dead code or duplicate reader, with file and line. The free-script route is known
   and is scheduled for step 4.
3. **Your earlier findings.** Which of them are now resolved, which are partly resolved, and
   which are untouched? Separate what belongs to step 4 or 6 from what should have been in
   steps 1 to 3.
4. **Before AL/X uses this live.** Anything that must be fixed first.

Be concrete: file, line, failure scenario. Write the answer as Markdown to
`.alx/reviews/2026-11-18-kicad-rebuild-review.md` if you can. Otherwise print it in full.
