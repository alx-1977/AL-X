# AL/X's KiCad work: why it takes hundreds of calls, and the plan to fix it

25 Nov 2026. For Friedl's approval before anything is built.

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
- **Add another copy of a part already on the board.** For example, "a third RFSW6024": it copies U6's symbol, footprint and fields, so there's no datasheet hunt.
- **Replace a part with a new one** at the same pins, like J3/J9 today.
- **Teardrops** (done today).
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

Three test jobs, replayed on copies and measured:

| Test | Today | Target |
|---|---|---|
| C94–C96 to 0402, including new library parts | 148+ calls | under 10 calls, 5 minutes |
| P0009: add the third RF switch, then update the PCB | 541 calls | under 40 calls, 30 minutes |
| P0005: teardrops | 48 calls | under 5 calls |

If a step doesn't meet its target, it isn't switched on.

## 7. Order of work

1. **The capacitor/resistor package swap (4.1) and the full view (4.2).** This is the biggest win for the least work, and today's 0402 job is the test.
2. **The other single operations (4.1) and part facts on library parts (4.3).**
3. **The batch schematic change (4.4).** At this step, test mixelpixx and kicad-sch-api against our own code, using the RF switch test.
4. **Edit directly, and the visible count (4.5, 4.6).** Small changes to her instructions and status.

Each step is measured against section 6 before the next one starts.

**Who builds it:** I'd do steps 1 and 4 myself, since they're small and need care with your existing tool. Steps 2 and 3 suit Codex on Sol at medium effort, with me reviewing.

## 8. What I need from you

1. Is this the right direction?
2. Are the targets in section 6 right?
3. Is the split in section 7 (Claude, then Codex) OK?
