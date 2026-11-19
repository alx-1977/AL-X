# Brief: independent review of AL/X's KiCad tool

For Codex Astra, 18 Nov 2026. **Findings and a recommendation, not code. Do not change any
files.**

## Who and what

- **Friedl** is a hardware/PCB engineer, not a programmer. He has used Altium for 15 years
  and does not use KiCad himself. Altium's cost is pushing him to KiCad.
- **AL/X** is his assistant. She runs locally from `src/alx`. Read `LAWS_OF_ALX.md` (short)
  and `AGENTS.md`.
  - **Law 0:** one production path per outcome; superseded code is deleted.
  - **Law 2:** mechanical steps belong in deterministic code.
  - **Law 3:** ambiguity returns to AL/X.
- **The goal:** Friedl tells AL/X in plain words what he wants on a schematic or board, and
  she does all the drawing and editing in KiCad. He only views the result in KiCad and
  approves it. The KiCad tool must cover what Altium's schematic and PCB editors let an
  engineer do. See `docs/ALTIUM_SCHEMATIC_FEATURES.md` and `docs/ALTIUM_JOB_LIST.md` for
  his feature list.
- He feels the tool was not planned as a whole. He says it has been patched for days, one
  gap at a time, during a real job (P0009, `ALX_Storage/Projects/P0009-bhl-roomreaderv2`,
  under `~/Library/Mobile Documents/com~apple~CloudDocs/`).

## The code

Current branch `board-edit-capability`, which is ahead of main:

- `src/alx/tools/kicad.py`: the one `kicad` capability and its actions.
- `src/alx/providers/kicad.py` and `src/alx/providers/kicad_*.py`: the implementation.
- `src/alx/specialists/board_agent.py`: the sub-agent that does whole board jobs (D-083).
- Decisions: `governance/DECISIONS.md` (D-058, D-061, D-074, D-080, D-082, D-083, D-084).
- Tests: `tests/test_kicad*.py`.
- Real usage logs (read-only):
  - `.alx/runtime/board-jobs/*.calls.jsonl`: every call the board agent made, with
    refusals and failures. `5c8475a00cde` is today's "set all designators to 0.5 mm" job.
  - `11c9ca3763ed` is the P0009 routing job.
- Earlier independent review: `.alx/reviews/2026-11-15-board-work-independent-review.md`.

The installed KiCad is 10.0.8 at `/Applications/KiCad/KiCad.app`. Use its Python and
`kicad-cli` if you want to check what KiCad's interfaces allow.

## Questions

1. **One coherent tool, or patches?** Judge the architecture as it stands: the read model,
   the edit model, the checks, the approval steps and the agent's instructions. Name the
   specific places it is coherent and the places it is patchwork.
2. **Redundant or competing code.** List every place where two paths reach the same outcome,
   dead or superseded code, and duplicated logic, with file and line. Friedl wants redundant
   code removed so the codebase does not become a mess.
3. **The right foundation.** Given KiCad 10's interfaces (pcbnew Python, the IPC API,
   kicad-cli and the file formats), what should the editing layer be built on, so that new
   jobs from his feature list don't each need new code? What should be kept as it is?
4. **Efficiency.** Why did the real jobs in the logs take so many calls? What design would
   make a job like "all designators 0.5 mm" one or two calls?
5. **Gaps that need a KiCad fork,** if any, against his feature list. Only list a job if no
   interface can do it.
6. **Order of work.** A practical sequence that rebuilds once rather than patching, with what
   to delete at each step.

Be concrete and critical. Disagree with the current design where the evidence warrants it.
Write your answer as Markdown to `.alx/reviews/2026-11-18-kicad-tool-review.md` if you can
write files. Otherwise print it in full.
