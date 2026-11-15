# Brief: making AL/X's KiCad board work cost-effective

For an independent reviewer (Codex Astra). Written 15 Nov 2026 by Claude Code, after a day of building and rehearsing. **We want your best plan, not code.** Please read the code and the records named below before answering, and challenge our approach where you think it is wrong.

## 1. What AL/X is, in one paragraph

AL/X is Friedl's personal assistant (Friedl: hardware/PCB engineer, not a programmer, runs FireFli). She runs locally on his Mac as a Python service (`src/alx`). One "Core" reasoning loop (Claude Opus 5.5 through the Claude Code CLI on her own Claude subscription, `src/alx/providers/claude_subscription.py`) interprets everything, chooses capabilities (tools) and speaks. Capabilities are structured tools behind a broker and a safety gate. Governing rules are in `LAWS_OF_ALX.md` (read it; it is short) and enforced by `scripts/check_architecture.py`, `scripts/check_governance.py` and the tests. The ones that matter most here:

- **Law 0:** one production path per outcome; superseded paths are deleted, not kept.
- **Law 1:** only the AL/X reasoning path interprets Friedl and decides meaning. Sub-agents may make judgements only where Friedl approved it in a recorded decision (D-070 worker, D-083 board agent).
- **Law 2:** anything mechanical with one correct outcome belongs in deterministic code, not a model call.
- **Law 3:** ambiguity returns to AL/X; consequential writes need his recorded approval.

Decisions live in `governance/DECISIONS.md`. The relevant ones: D-058/D-061 (KiCad mechanics), D-070 (Sonnet worker for mechanical briefs), D-074 (one capability per job: one `kicad` tool with many actions), D-080 (one approval covers a listed set of writes), **D-082** (board work order: schematic, then placement, then routing, then write-back, each locked until Friedl approves) and **D-083** (a board agent does a whole board job).

## 2. The goal

Friedl wants AL/X to do real KiCad board work, so he spends less of his own time on it:

- **edits** to existing boards (swap parts, move things, re-route a section), and later
- **new boards**, starting from the schematic.

He tells her in plain words, she does the work on her own copy, and shows him each step. He approves or says what to change. Nothing touches his project until he says yes. The done-lines he confirmed are in `docs/BOARD_EDIT_DONE.md`; his drawing preferences are in `ALX_Storage/Preferences/Schematic and layout.md` (iCloud: `~/Library/Mobile Documents/com~apple~CloudDocs/ALX_Storage/Preferences/`).

**Scale he cares about:** P0008 (`ALX_Storage/Projects/P0008-bhl-ups-powersupply`), about 360 parts, 12 schematic sheets, about 2,500 track segments and 800 vias. Friedl designed it himself in about 10 days. The small test job is two parts.

**What "working" means to him (his words, paraphrased):**
1. **Quality** like the reference result below. His verdict on today's schematic: "quite good", with small style fixes.
2. **No errors.** "Code should not crash for any reason, as much as my hardware should not randomly blow up."
3. **Cost:** "10% [of her weekly allowance] for replacing two parts simply makes this not a feasible option. Not even close." He would accept about half of what we measured, and he is willing to upgrade her Claude plan **if** it works and the waste is gone.
4. **Speed:** slow is acceptable if results are good and usage moderate. But 35+ minutes for a two-part schematic change felt far too long.

## 3. The test job and its known good answer

**Job:** "On P0009, replace the two 4ch RF switches with the 2ch ones from P0020."

- P0009 = `ALX_Storage/Projects/P0009-bhl-roomreaderv2/hardware/kicad/BHL_RoomReaderV2_KiCAD` (an Altium import: seven top-level sheets).
- U6/U7 PE42462 (4-channel, QFN-24) become RFSW6024TR13 (2-channel, from P0020; FireFli library `FireFli_ICs`).
- Facts Friedl supplies: the bench board drives J5 pins 2/6 and 4/8, so **RFC2 and RFC4 are kept**, and the RFC1/RFC3 filter parts (12 parts) go. NC pins are handled as the datasheet says (grounded, RFSW6024 datasheet p9).
- RFSW6024 control: VEN low with VCTL high routes RF1 to RFC2; VCTL = V3, VEN = V2; V1 and LS are unused now.

**Reference result**, done by Claude Code with Friedl on 14 Nov, which he said he "can work with": `.../BHL_RoomReaderV2_KiCAD/backups/2026-11-14-switch-swap-reference/`. It has a `README.md` and the `scripts/` used: `layout.py` (placement in one go), `route.py` (routing in one go), `sch_edit.py` (schematic), `netcompare.py`. Placement: switches turned 90° CCW, 6.8 mm apart, J5 down 1.5 mm, filters above their switch pins. Routing: RF traces 0.355 mm with 1 mm arcs, P/N matched to about 0.03 mm, redundant GND vias removed. That session was one continuous Claude Code session: big scripted steps, with Friedl steering in short sentences ("turn them 90° CCW", "curved RF traces").

## 4. What exists now (branch `board-edit-capability`, local, not pushed)

- **`src/alx/tools/kicad.py`**: the one `kicad` capability. Its actions include inspect, check (DRC/ERC; also on her own copy via `work_copy_id`), edit, render, open_in_kicad, approve_step, write_back, inspect_schematic, make_hierarchical, check_layout, fetch_datasheet and more.
- **`src/alx/providers/kicad.py`**: the mechanics. Work copies live in her storage. `edit` runs her Python script inside KiCad's bundled Python (pcbnew), confined by macOS `sandbox-exec`: writes only to the copy, no reading of his home folder, no starting processes. After each edit it reports a structural diff, DRC before and after, `compared_to_original` (DRC findings new against his original, plus a pin-by-pin schematic/board comparison across all sheets from KiCad's netlist) and the D-082 step status. `dry_run` previews and restores. A failed edit restores the copy; an uncertain restore taints it.
- **`src/alx/providers/kicad_steps.py`**: the D-082 gate (fingerprints of schematic, placement and copper; edits refused out of order; write-back refused until every changed step is approved and the schematic and board agree).
- **`src/alx/providers/kicad_helpers.py`**: pcbnew helpers prepended to every script (move/rotate parts, `replace_part`, `add_part`, `add_track` with arc bends, `track_length`, `teardrops`, …) plus `helper_manual()`.
- **`src/alx/providers/kicad_schematic_helpers.py`**: text editing of `.kicad_sch`, since KiCad has no schematic Python API. `sheet(name)` gives symbols with pin points; it supports swap/remove symbols, wires, labels, no-connects, copying power symbols, `wire_pin_to`, `label_pin`, `ground_pin`/`power_pin` and `tidy_fields`. Tested against KiCad's netlist in `tests/test_kicad_schematic_helpers.py`.
- **`src/alx/specialists/board_agent.py` and `src/alx/tools/board_agent.py`**: the D-083 board agent. Opus, its own Claude CLI session, tools limited to `kicad` (no approvals, no write-back, no windows) plus `look` (image viewing). Each stretch ends in `report_step`; the next stretch starts a fresh session from a JSON journal plus his preferences plus the helper manual. Bounds are 80 calls and 30 minutes per stretch. AL/X runs it as a planned (background) step and relays Friedl's words.
- **`src/alx/specialists/worker.py`**: the D-070 Sonnet worker. Mechanical briefs only; no judgement.
- **Session records** (every model call with usage) are in `.alx/runtime/reasoning-sessions/2026-11-15/*.jsonl`. The trace of every tool call is in `.alx/runtime/trace.sqlite3`. Board-job journals are in `.alx/runtime/board-jobs/`. The goal store is `.alx/runtime/goals.sqlite3`.

## 5. What we measured today

All on her Claude subscription (Claude Pro-level plan). Friedl read her usage page; "session" is the 5-hour window, "week" the weekly allowance.

| Run | How | To where | Session | Week | Notes |
|---|---|---|---|---|---|
| R1 (morning) | AL/X Core plus Sonnet worker | schematic, wrong | n/a | about 3–5% | 132 Core rounds, 15 fresh Core sessions (each about 57k characters of system prompt plus 30–50k of goal state). Kept the wrong channels without asking. Hit the **weekly** limit. |
| R2 | board agent, one session paused between steps | schematic plus his fixes | 48% | 5% | Agent: about 80 tool calls (29 edits, 20 full-page images, 11 renders), about 160 model steps, **28M cache-read tokens**; context about 250k tokens per step by the end. Quality good. |
| R3a | fresh session per stretch | schematic, unfinished | >28% | 3% | 116 steps, 17M cache reads. 31 edits, of which **26 were read-only "previews"**: listing symbols and wires, reading helper source with `dir()`/`inspect`. Each edit took about 25 s (two whole-board DRCs per edit). |
| R3b (after fixes: schematic view, helper manual, DRC reuse) | fresh session per stretch | schematic, unfinished | **43%** | **4%** | 41 plus 82 steps, about 9.7M cache reads, context up to about 185k. 4 then 15 edits. **Errors:** the agent guessed symbol library names for a GND symbol 5 times (his sym-lib-table holds only the FireFli libraries; KiCad's own `power` library is not listed; the sheet's GND symbols come from an Altium-import cache library). One read with an offset past the end was refused. After the 30-minute stretch limit, the new session did not know which work copy it had. **"Stop" did not stop:** AL/X cancelled her plan and said "Stopped" while the agent ran on; killing the CLI process made the provider's retry start a new session. |

The improvements that did work: fewer edits, faster edits, far fewer images, and one error instead of many in the first stretch. But new waste replaced the old, and the total did not fall.

## 6. Our diagnosis so far (please test it, do not just accept it)

1. **The cost is model steps times context.** Each agent step re-reads its whole context. A step that only finds its way around (reading sheets, guessing names, checking) costs as much as one that changes something. The agent *draws* the schematic stroke by stroke, which is mechanical work done by a model.
2. **Missing or awkward primitives cause exploration.** No cheap schematic view (now added), no tool manual (now added), library lookup limited to his tables, no "swap this part and keep its connections" operation.
3. **Images and big results accumulate** in a session.
4. **The CLI transport** (`claude -p --output-format stream-json` with an MCP tool bridge, `src/alx/providers/claude_subscription.py`) means we cannot drop old tool results or images from the session context. We can only start a new session.
5. **AL/X's own Core** adds its rounds: a large system prompt, a goal state replayed per session, decision validations that reject a whole step.

## 7. Constraints you must respect

- The Laws, the decisions and the gates. A proposal that needs a new decision is fine, but say so.
- **Models:** Claude Opus 5.5 / Sonnet 5.5 / Haiku 5.5 through the **Claude subscription CLI** on her own login. There is no metered Anthropic API key in use for this; check `src/alx/config/settings.py` and the providers before assuming otherwise. Codex (OpenAI) is used for coding jobs (`src/alx/providers/coding_*`). Friedl can upgrade her Claude plan.
- **KiCad 10.0.8** on macOS. pcbnew Python is available in KiCad's bundled Python. KiCad's IPC API server is enabled in his settings (kicad-python can talk to a running KiCad). There is **no schematic Python API**; `.kicad_sch` is s-expression text. `kicad-cli` handles DRC/ERC/netlist/PDF/SVG export.
- **Safety:** scripts stay confined to her copy. His files change only by `write_back` with his approval. KiCad windows shown to him are opened on copies.
- **Friedl's quality bar and preferences** (his preferences file, his done-lines). The datasheet is the reference for every part.

## 8. The questions we want answered

1. **Is the architecture right?** Is a single LLM agent with fine-grained KiCad tools the right shape for this at all? What should be **deterministic code**, and what must be the **model's judgement**? (For example: should the model only write a short declarative "change spec" (part X becomes Y, pin map, nets kept or removed, placement intent) that code compiles into schematic and board edits?)
2. **Schematic edits:** text edits of `.kicad_sch` versus another route (IPC API, KiCad's own "update from schematic", generating schematics from a netlist or code such as SKiDL/atopile-style). What gives reliable, cheap, style-compliant edits?
3. **Placement and routing:** what is realistic? Model-placed with code helpers, an autorouter (Freerouting or KiCad's) for non-critical nets, the model only for RF/critical nets, or the human doing layout with AL/X checking? What would you do for a P0008-sized board?
4. **Model and transport:** is the Claude CLI session the right vehicle? Would a different split (Haiku/Sonnet for mechanical steps, Opus only for judgement, or Codex for script-writing) cut cost without losing quality? How should context be bounded per step?
5. **Errors:** how do we get to **zero** routine errors? (Library lookups, refused calls, a stop that really stops, never claiming an action that did not happen.)
6. **Estimates:** for your plan, what cost (share of a weekly allowance, or tokens) and wall-clock time would you expect for (a) the P0009 two-part swap and (b) a P0008-sized new board?
7. **What should we delete** (Law 0) if your plan replaces parts of what exists?

## 9. What we would like back

A ranked plan:
- the approach in a few sentences
- what to build, change and delete, with file pointers
- why it will cut cost and errors, with your expected numbers
- the risks
- what Friedl would have to decide or approve

Also say plainly if you think this goal is not feasible at a sensible cost today, and what the best useful alternative is.
