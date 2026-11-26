# AL/X's KiCad work: why it takes hundreds of calls, and the plan to fix it

25 Nov 2026. Approved by Friedl on 26 Nov 2026, without call targets.

## 1. What happened

| Job | Calls | Time | Altium, by hand |
|---|---|---|---|
| P0009: add a third RF switch (same part as U6), sync to the board, place it | 541 | about 6.5 hours, over two days | about 30 minutes |
| P0009: change C94, C95, C96 from 0603 to 0402 | 148 so far, still running | 30 minutes so far | 2 minutes |
| P0005: teardrops | 48 (after a bug fix) | | one command |

Every call is one round of her model thinking, so each call costs usage.

## 2. Where the calls go

From the call logs of the P0009 job, all 692 calls so far:

- **Looking, not doing: about two thirds.**
  - Reading documents: 225 calls. Datasheets page by page, often the same one again: the RF switch datasheet 9 times, the 0402 capacitor datasheets 13 times.
  - Small views of the schematic: 71.
  - Pictures: 66.
  - 101 "edits" were little programs that only print things so she can see them.
- **Trial runs:** of 172 edits, 148 were trial runs that put the copy back afterwards. Then she does the same edit again for real.
- **Reading her own tools to learn how to use them.** In the capacitor swap she read AL/X's own source code 17 times, and the library generator, its README and build script 16 times. That was to work out how to make a 0402 capacitor part.
- **Failures:** 92. Most came from our tool and were fixed today, so failures since are down from 16% to 8%.

## 3. Why

1. **The tool hands out small pieces.** There's no single view of a sheet or a board area with everything on it: parts, pins, wires, labels, nets, footprints. She builds the picture one call at a time.
2. **Common jobs have no single operation.** "Change these caps to 0402" means:
   - find or make the library part;
   - swap the symbol's footprint and fields;
   - swap the footprint on the board;
   - check.

   She works each step out from scratch, by reading code.
3. **Part facts aren't kept.** A datasheet read for one step is gone by the next, so she reads it again. A part we already use (U6) is researched as if it were new.
4. **She trial-runs everything.** Every edit can already be undone, so most trial runs are wasted.

## 4. The plan

No hard limit on calls (your rule): a limit would throw away a nearly finished job. The calls come down by giving her better tools.

### 4.1 Common jobs become single operations

Each is one call, done and checked in code (netlist, ERC, DRC and a picture), and reported in plain words:

- **Change a part's package or value.** For example, "C94–C96 to 0402":
  - picks or makes the FireFli library part through the generator (capacitors, resistors);
  - swaps the schematic and the board;
  - keeps every connection.

  Built 26 Nov in three calls she can make:
  - **find_part:** when a capacitor isn't in the library, it lists what JLC stocks for that value and size under the brand rule: Taiyo Yuden for X5R/X7R, YAGEO for C0G. Each part shows its ratings, LCSC number and stock. She chooses.
  - **add_passive:** puts her choice into a library copy through the generator, as the build does. The copy is written back with your approval.
  - **swap_part:** one call changes the schematic. After your approval, a second call changes the board, with every pad keeping its net and the footprint taking the new part's MPN and LCSC number.
- **Add another copy of a part already on the board.** For example, "a third RFSW6024": it copies U6's symbol, footprint and fields, so there's no datasheet hunt.
- **Replace a part with a new one** at the same pins, like J3/J9 today.
- **Teardrops** (done today).
- **Route or reshape a track** (added 26 Nov, from the P0009 RF job: 218 calls for four short tracks):
  - add a track between two pads, or round an existing bend, in one call, changing only what was asked;
  - 45° pad exits and rounded bends built in;
  - for RF nets, the width comes from the board's stack-up for 50 Ω, and the bend radius from the width. On 26 Nov she first used 0.20 mm (about 69 Ω) until Friedl asked for proper RF calculation.
- **Update the PCB from the schematic:** new parts placed beside related parts, removed parts deleted, nets updated.

### 4.2 One full view per job

The first call of every job returns everything relevant in one go:
- the sheet(s) and board area involved;
- each part with its pins and nets;
- the footprints;
- the part facts we already hold.

That replaces the dozens of small views and the print-only programs.

### 4.3 Part facts kept on the library part

Pinout, pin functions, layout notes (keep-outs, RF guidance) and the JLC number are read from the datasheet **once** and stored with the FireFli library part. Every later job reads them in that one call. Datasheets are opened only for something new, and then only at the page needed (pin table, layout section), never page by page.

### 4.4 Bigger schematic changes in one batch

For a change no single operation covers, she states the parts, the pins and the connections in one call. The tool draws it to your standard and checks it. That's Astra's main recommendation too.

### 4.5 Edit directly

No trial runs unless something is risky; a wrong edit is undone with one call. One picture per finished step, not per attempt.

### 4.5a His own edits count as approved

When Friedl saves a change on her copy himself, it counts as approved; he isn't asked to approve his own work (26 Nov: his moved parts needed a placement approval before she could clear a clearance error).

### 4.5b She shows every error she reports

KiCad only marks clearance errors after its checker has run, unlike Altium, so she keeps telling Friedl about them, and shows each one: a close-up picture with the spot marked, and the parts and nets named. On 26 Nov she reported three, never showed them, and the copy she opened showed nothing in KiCad.

### 4.6 You can see the count

Her status shows the calls used so far on a job, so you can stop her early if something looks wrong. It shows; it never stops her.

## 5. Outside options (from Astra's review)

- **Keep:** KiCad's own checks (`kicad-cli`: ERC, DRC, netlist, pictures).
- **Test before choosing:**
  - **mixelpixx KiCad MCP server:** it has batch "add parts and connect" commands.
  - **kicad-sch-api:** a Python library for editing schematics. It has to prove itself on our KiCad 10 projects, because its notes mention older versions and incomplete connection checks.
- **KiCad's official live interface** covers schematics only from KiCad 11. Not usable yet.

If one of them does 4.1 or 4.4 better than our own code, it's adopted behind the same tool, so nothing changes for you or for her.

## 6. Proof before it goes live

There are no call targets or limits: every job should simply be as efficient as possible. Each new tool is proven on three test jobs, replayed on copies:

- P0009: C94–C96 to 0402, including the new library parts (148+ calls today);
- P0009: add the third RF switch, then update the PCB (541 calls today);
- P0005: teardrops (48 calls today).

Friedl sees the calls and time before and after. A tool is switched on only when it is clearly better, and it keeps being improved while it still wastes calls.

## 7. Order of work

1. **The capacitor/resistor package swap and the track operation (4.1), the full view (4.2), edit directly (4.5) and his own edits and shown errors (4.5a, 4.5b).** The biggest wins for the least work. Tested on the next design.
2. **The other single operations (4.1) and part facts on library parts (4.3).**
3. **The batch schematic change (4.4).** At this step, test mixelpixx and kicad-sch-api against our own code, using the RF switch test.
4. **The visible count (4.6).** A small change to her status.

Each step is measured on the section 6 jobs before the next one starts.

**Who builds it:** I'd do steps 1 and 4 myself, since they're small and need care with your existing tool. Steps 2 and 3 suit Codex on Sol at medium effort, with me reviewing.

## 8. Decision

26 Nov 2026: Friedl approved the direction and the split, with no call targets.
