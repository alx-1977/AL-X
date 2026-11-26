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
| R14 | 16 Nov 16:04 | gpt-6-luna | high | 21b85e4 (part note visible) | schematic | stopped at ~25 min | 72 (23) | not read (plans reset at 17:00) | not read | **Failed** (Friedl). Stopped by Friedl. Read the RFSW6024 note (call 35) but grounded only pin 11 and EP; NC 4, 6, 7, 8, 10, 13-16 left open. Refusals: guessed folders; real shorts caught (pin 12 on old GND, RF1 end, RX line); 3 over-strict same-net refusals (GND/3V3 leads touching their own net). AL/X then began the clean-up herself on her own model until stopped. Friedl: likely not feasible with Luna; larger projects would fail |
| R15 | 16 Nov 17:23 | claude-opus-5-5 | medium | 21b85e4 (part note visible) | schematic | 20 min | 53 (5) | 3→26% / 0→3% | unchanged | Finished; read the RFSW6024 note and grounded every NC pin and EP on both switches; nothing new in ERC; RF nets took KiCad's automatic names. Refusals include two over-strict same-net ones. Friedl: looks good; only C44/C45 text not neat |
| R16 | 16 Nov 17:53 | gpt-6.1-sol | medium | 21b85e4 (part note visible) | schematic | 8 min | 38 (4) | 27→29% / 3→3% (her turns) | 0→5% / 29→30% | Finished; read the part note at call 8; every NC pin and EP grounded on both switches; nothing new in ERC or DRC; 3 of 4 refusals were real shorts caught. Friedl: only C44/C45 text placement |
| R17 | 16 Nov 18:12 | gpt-6.1-sol | medium | 21b85e4 (part note visible) | schematic | 8 min | 42 (9) | 30→32% / 4→4% (her turns) | 5→10% / 30→31% | Repeat of R16. All connections right, NC pins and EP grounded, RF nets kept RFC2/RFC4 names; nothing new in ERC/DRC. Refusals: known traps and over-strict same-net rules. Friedl: quirks — C37's net turns down right at the capacitor pin (not out of the pin first); VCTL between U6 and U7 crosses the C56 symbol; C44/C45 text untidy (every run) |

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
| R15 Opus medium | 20 min | 53 (5) | Claude | +23% | +3% | good; C44/C45 text |
| R16 Sol 6.1 medium | 8 min | 38 (4) | GPT | +5% | +1% | only C44/C45 text |
| R17 Sol 6.1 medium | 8 min | 42 (9) | GPT | +5% | +1% | right; C37 turn, wire over C56, C44/C45 text |
| R18 Sol 6.1 medium | 6 min | 33 (2) | GPT | +5% | 0% | right; J5 wires drawn twice (tool, fixed) |
| R18 | 16 Nov 19:34 | gpt-6.1-sol | medium | e2939f7 (refusal fixes) | schematic | 6 min | 33 (2) | 35→38% / 4→4% (her turns) | 10→15% / 31→31% | Finished; part note read at call 10; NC pins and EP grounded; nothing new in ERC. Both refusals the swap-order trap. Friedl: R38/R40/R42/R44 to J5 drawn twice (a second wire over the one already joining them, allowed by the same-net rule); fixed after: a wire between pins already joined draws nothing, no wire may run along another, later swaps done first |

Friedl: time matters less than cost when the result is right, since it runs in the background; a faster
model can be offered when he asks.

## Next (16 Nov evening, Friedl)

Sol 6.1 medium is the agent model: R16 and R17 gave the same right result at about 1% of a week. Luna is
out; the second round of the other models is skipped. Next: one more Sol schematic run on the fixed
tools, then the full swap (placement and routing) on Sol, measured.

## Refusal fixes after R17 (16 Nov evening)

Every refusal of the day was sorted (73): finding files ~35, over-strict drawing ~15, unclear messages ~5,
real catches ~15 (kept). Fixed without adding to any instructions sent with each run:

- Files: `~/ALX_Storage/...` and `P0020/...` paths resolve; a missing file names where things are; a
  project is found by its number in inspect_schematic too; a tool action called as a tool names its tool.
- Drawing: leads may touch wiring already on their own port's net; a free pin's exit spot is only in the
  way when run along or ended on, not crossed; a GND/power symbol is placed clear of where later pins come
  out (one size for placing and checking).
- His drawing points: a wire into a part's pin arrives the way that pin points (C37), trying both corners
  first; a wire may not cross a part's body (C56); nothing new in the title block (read from his page
  template); text keeps clear of other parts' bodies and keeps air from wires, and a part lying down gets
  its text stacked above or below (C44/C45).
- Messages: a pin joined to a whole net is named by the net ("net GND (120 pins)"); a swapped part's pin
  on old wiring is told to unwire; a symbol on another sheet names that sheet; remove_labels without a
  sheet works on every sheet; the call log now keeps a refused drawing's reason (it kept only the code).

Replayed against the agents' own logged edits (R10-R17) on fresh copies: every edit that succeeded still
succeeds except where it drew across a part (R17's VCTL over C56, now refused as Friedl asked); the
refusals left are real shorts caught and the agents' own bad detours.
