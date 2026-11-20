# Board edits as checked scripts: build plan (D-084a)

For Friedl's approval before any coding. Short version: **give the board agent the script route that already exists, put a strong gate after it, let it see its own drawing, and teach it the standard.**

## What exists today

- **`kicad edit` already has two inputs:**
  - `script`: bounded Python on her work copy, with tested helpers run first;
  - `change`: D-084's fixed operations.
- **Only AL/X may use `script`.** The board agent is held to `change`, which has no sheet creation and no redraw. That's why P0005 failed.
- **After every edit, the change report already gives:** ERC (with "only new"), a comparison with the original, layout rules, and file diffs.
- **Render to PDF exists**, but no model looks at it. Codex image viewing is switched off (D-083a).

## What changes

1. **One path.** The board agent edits through `script`. The `change` operations become functions inside the script library, so a script can call `swap_symbol(...)` in one line or do custom work. `change` as a separate input is removed (Law 0).
2. **The script states its intent.** Each edit declares the connections it means to make and remove, for example "R38.1 to V_ISO, R38.2 to IN_D; IC4 off RS485_A/B". Code then checks the result's full netlist against the copy before the edit:
   - every declared change holds;
   - **nothing else changed**.
   This replaces D-084's per-operation check for all edits.
3. **The gate.** It runs automatically after every script, and a failing edit is undone and the failure named:
   - net comparison against the declared intent;
   - `stylecheck` (the FireFli standard; today pin-to-wire and junction dots);
   - ERC with no new findings;
   - DRC with no new findings, for board edits.
4. **Eyes.** After each step the agent gets a PNG of the changed sheet or area, cropped to what changed so it stays cheap, and must look at it before reporting. That needs Codex's image viewing switched on only for these images (amends D-083a).
5. **The standard.** The agent's instructions point at `SCHEMATIC_STANDARD.md` and the FireFli design blocks. They don't repeat them, so the instructions stay lean.
6. **Working scripts become library functions.** When the same kind of script works twice (for example "move a section to a new sheet", or "copy a block's layout to its twin"), it's added to the library with a test. That's how today's P0005 scripts come in first.
7. **Cost controls:**
   - the library gives compact views (one block, one net), not whole files;
   - working context target 40–60k, from Astra's review;
   - every job's usage is measured before and after, as agreed.

## Model

The board agent stays on Codex (your ChatGPT plan). **Sol 6.2, high effort** for jobs that need design judgment, medium for mechanical ones. AL/X chooses per job. We measure, then adjust.

## Who builds it

- **Claude (here):** this plan, the Codex brief, the gate design, and reviewing Codex's work against the plan.
- **Codex:** the coding and tests, on its own branch, from the brief.
- **Steps 1–3 first** (one path, intent check, gate), then 4 (eyes), then 6 (library growth).

## Test before it counts

Rerun the same P0005 RS485 job on a fresh copy with the board agent. It passes if:
- the gate is clean;
- you approve the drawing in KiCad;
- usage is measured.

Then one small change on P0009.

## Your call

1. Approve this plan?
2. Is Codex building it, with me reviewing, OK?
