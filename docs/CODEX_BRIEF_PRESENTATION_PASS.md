# Codex brief: the presentation pass (D-084a addition)

**Branch:** `board-edit-capability` (HEAD 5a5caef).

**Read first:**
- `AGENTS.md` and the documents it names;
- D-084a;
- `docs/CODEX_REPORT_D084a_ROUND4.md`;
- `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/SCHEMATIC_STANDARD.md`.

**Ground rules:**
- Don't commit, push, open PRs or request reviews.
- Write `docs/CODEX_REPORT_PRESENTATION_PASS.md`.

## Why

**Friedl's decision, 20 Nov:** the board agent on Sol does the engineering well and cheaply, but its drawings aren't client quality. A stronger model (Opus) took AL/X's finished RS485 sheet to Friedl's standard in one pass, about 7% of a 5-hour Claude session, without changing a connection. He wants that built in, so he never has to edit in KiCad himself.

## What to build

1. **Settings** (in `src/alx/config/settings.py`, beside `board_agent_settings`). The presentation agent reads:
   - `ALX_BOARD_PRESENT_PROVIDER`, default `claude_subscription` (AL/X's own Claude account, never an API key);
   - `ALX_BOARD_PRESENT_MODEL`, default `claude-opus-5-5`;
   - `ALX_BOARD_PRESENT_EFFORT`, default `medium`.

   Validate them like the board-agent settings.
2. **One presentation pass per board job.** The `board_job` capability (`src/alx/tools/board_agent.py`) accepts `"pass": "presentation"` on an existing job whose schematic step is `step_ready`. It runs the same board agent class, on the same job journal and work copy, but:
   - uses the presentation settings (model, provider, effort);
   - uses a fixed, short brief: redraw only, to the FireFli standard and the job's sheet(s). Make no electrical change. Use the slot grid, `route`, `info_table`, `mirror_block`, `place_power`, `upright`, `arrange_root` and design blocks. Look at every render.
   - **runs every edit with `intent: []`.** The gate already refuses any connectivity change for an empty intent. Make that mechanical for this pass: the provider refuses a presentation-pass edit whose intent isn't empty.
   - ends with a normal `step_ready` report, plus a short note of what was tidied.
3. **Core's choice.** AL/X decides when to run it (normally straight after the schematic step is ready, before showing Friedl). Add one line to her board-work instructions. No deterministic router (AGENTS.md: the choice of what to do next stays with AL/X).
4. **Usage.** Record the pass's model calls under the job, like other board-agent calls, so its cost can be read separately from the engineering stretches.
5. **Reference for the drawing.** Mention in the brief that `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/reference_p0005/` holds the scripts of the presentation pass Friedl accepted. Don't paste them into the prompt.

## Tests and gates
- Tests:
  - the settings parse and validate;
  - `pass: presentation` is refused unless the schematic step is `step_ready`;
  - a presentation edit with a non-empty intent is refused;
  - a presentation edit that changes a connection is undone (gate);
  - the pass uses the presentation model and provider (stub the provider);
  - its calls are journalled under the job.
- Run both law gates and the full suite; it was 5,722 passed, 2 skipped.
- Report results and judgment calls.
