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
| R10 | 16 Nov 13:43 | gpt-6-astra | medium | c219e99 | schematic | 8 min | 30 (5) | 10→13% / 30→~31% (her turns) | 46→68% / 25→29% | Finished, no new ERC; no previews; refusals: 3 shorts caught, 1 wire over a port, 1 leftover 3V3 port in the way. Friedl: **best schematic so far**; nitpicks: drawing runs into the title block, C44 text placement, L6 designator could sit closer |
| R11 | 16 Nov 14:01 | claude-fable-5-2 | medium | c219e99 | schematic | 10 min | 40 (0) | 14→42% / 30→33%; Fable meter 6% | unchanged | Finished, no refusals, no previews; 6 renders and 11 looks at its own drawing. Asked whether the unused J5 pins 1/3/5/7 should be grounded (Friedl: no, no-connect). Friedl: much the same as R10; C47/C49 text better; U7 RF1 runs close to RF2 for a stretch; bottom GND still on the title block |
| R12 | 16 Nov 14:55 | claude-sonnet-5-5 | high | c219e99 | schematic | 13 min | 52 (6) | 53→66% / 34→35%; Fable meter unchanged | unchanged | Finished, nothing new in ERC; most reading and looking (14 schematic reads, 15 renders/looks); refusals mostly tool names, paths and a crowded power port. Friedl: C44 text poor; U6 pins 6/7/8 GND port placed oddly; bottom GND on the title block |
| R13 | 16 Nov 15:16 | gpt-6-luna | high | c219e99 | schematic | 17 min | 68 (13) | 68→72% / 36→36% (her turns) | 68→70% / 29→29% | Finished. First 8 calls mostly refused (wrong fields, guessed paths), then wandered folders for the datasheet. Faults (Friedl): NC pins 6, 7, 10, 11 left unconnected on both switches, where the datasheet recommends ground for isolation (allowed, but the other models grounded them; a mistake); RF nets joined by labels, not wires. Otherwise 'not too bad' |

## Planned (16 Nov, Friedl)

Same job, same tool code, one change at a time:

1. ~~gpt-6-astra, medium~~ (R10)
2. ~~claude-fable-5-2, medium~~ (R11)
3. ~~claude-sonnet-5-5, high~~ (R12)
4. ~~gpt-6-luna, high~~ (R13)

From R10 the tools run no ERC on each structured edit (once per step, at the agent's check) and tell
the agent a refused change costs nothing, so previews are rarely needed. Also from R10: every lead runs
into a GND or power port the way the port points, and labels on a changed sheet sit on their wire.
Friedl reads both usages after each restart, once AL/X is idle, before the job is given.

## Summary of the series (16 Nov)

| | Time | Calls (refused) | Plan used | Session | Week | Quality (Friedl) |
|---|---|---|---|---|---|---|
| R10 Astra medium | 8 min | 30 (5) | GPT | +22% | +4% | best so far |
| R11 Fable medium | 10 min | 40 (0) | Claude | +28% | +3% | much the same |
| R12 Sonnet high | 13 min | 52 (6) | Claude | +13% | +1% | three faults |
| R13 Luna high | 17 min | 68 (13) | GPT | +2% | ~0% | not too bad; NC pins not grounded, RF by labels |

Friedl: time matters less than cost when the result is right, since it runs in the background; a faster
model can be offered when he asks.
