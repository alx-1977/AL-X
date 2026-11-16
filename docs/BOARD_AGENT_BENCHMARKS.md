# Board agent benchmarks

One row per measured run of AL/X's board agent. Friedl reads the usage pages before and after; the
call counts and times come from the job's call log (`.alx/runtime/board-jobs/<job>.calls.jsonl`).
Claude usage is AL/X's Claude plan (her own turns, and the agent when it runs on Claude); GPT usage is
Friedl's ChatGPT/Codex plan (the agent when it runs on Codex).

**The job (from 16 Nov):** P0009, replace the two 4ch RF switches (PE42462) with the 2ch RFSW6024 from
P0020; keep RFC2/RFC4 (J5 2/6, 4/8), remove the RFC1/RFC3 filters; RF1 (pin 9) to RFC2, RF2 (pin 12) to
RFC4; VCTL V3, VEN V2, V1 and LS unused; NC pins per the datasheet; his preferences. Schematic step
only, on a fresh copy, shown to him in KiCad. Gate for the whole swap (Astra review, 15 Nov): at most 2%
of a week, no routine errors.

## Runs

| Run | Date | Agent model | Effort | Tool code | Step | Time | Calls (failed) | Claude session / week | GPT session / week | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| R2 | 15 Nov | claude-opus-5-5 | CLI default | early D-083 (scripts) | schematic + his fixes | — | ~80 | 48% / 5% (same plan) | — | Schematic "quite good"; stopped before placement |
| R3b | 15 Nov | claude-opus-5-5 | CLI default | scripts | schematic (unfinished) | — | 116 steps | 43% / 4% | — | Hit 5-hour limit before done; 26 of 31 edits were read-only previews |
| R4 | 15 Nov | claude-opus-5-5 | CLI default | first structured changes | schematic | — | — | 26% / 3% | — | Ruined by a tool bug (read-only arguments) |
| R5 | 15 Nov | claude-opus-5-5 | CLI default | structured | schematic | — | — | 75→100% / 19→21% | — | Tool bugs (#PWR?, short); copy unusable; not a clean number |
| R7 | 16 Nov am | claude-opus-5-5 | CLI default | 6da964e | schematic incl. hierarchy conversion | ~25 min | ~51 (several) | 30→53% / 24→27% | — | Electrically right; drawing faults (ports, text, root sheet) → tool fixes |
| R8 | 16 Nov | claude-opus-5-5 | CLI default | 01a3d80 | schematic | stopped | 31 (2) | 59→? / 27→? | — | Stopped on an order-dependent drawing gap (fixed in 63572f7) |
| R9 | 16 Nov 12:45 | gpt-6-astra | high | 63572f7 | schematic | 16 min | 46 (2) | 4→9% / 29→30% (her turns) | 8→46% / 19→25% | Finished, no new ERC/DRC; 6 previews. Friedl: acceptable; faults: some GND ports entered sideways (wire not in the port's direction), imported labels centred on their wires (both fixed for R10) |

## Planned (16 Nov, Friedl)

Same job, same tool code, one change at a time:

1. gpt-6-astra, medium
2. claude-fable-5-2, medium
3. claude-sonnet-5-5, high
4. gpt-6-luna, high

From R10 the tools run no ERC on each structured edit (once per step, at the agent's check) and tell
the agent a refused change costs nothing, so previews are rarely needed. Also from R10: every lead runs
into a GND or power port the way the port points, and labels on a changed sheet sit on their wire.
Friedl reads both usages after each restart, once AL/X is idle, before the job is given.
