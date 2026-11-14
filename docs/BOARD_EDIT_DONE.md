# Board edits with AL/X — what "done" means

Confirmed by Friedl, 14 Nov ("Aside from that it looks ok"), with the note on new boards added below. This is the test the work is built and judged against. The job is the P0009 one, but done the way it was done today.

## How it goes, in your words

1. I tell AL/X the change in plain words, for example "On P0009, replace the two 4ch RF switches with the 2ch ones from P0020."
2. She reads both boards and the datasheets, then asks me only what she can't settle herself (for example which antenna pins are used, or whether a firmware change is OK). She doesn't ask what the datasheet already answers.
3. She makes the change on her own copy and shows me the **placement only** on her screen: current on the left, her copy on the right. Nothing is routed yet.
4. I say what to change ("turn them 90° CCW", "move them closer", "remove the parts no longer needed"). She does it and shows me again, as often as needed.
5. When I approve the placement, she routes it and shows me the result on her screen, with DRC compared to my original and the RF lengths.
6. I can ask for changes to the routing too ("curved RF traces", "remove the redundant vias"). She does them and shows me again.
7. She updates the schematic to match, and shows me it matches the board.
8. When I say yes, she writes it back to my project. Nothing is written before that.

## What I never have to do

- Open KiCad.
- Check her work myself to find out whether she broke something. She checks against my original and tells me.

## What has to be true at every "show me"

- She has looked at the actual board picture herself, not only at the DRC result.
- DRC against my original: no new problems, nothing unconnected.
- The datasheet is the reference for every part (pins, ground pins, land pattern).
- RF: straight and short where possible, curved bends, P and N matched.
- Placement is decided before routing, from where the pins point.

## Not only edits

Not every job will be an edit to an existing board. New boards start from the schematic: she draws it with me first, then places and routes, with the same "show me, I say yes" steps. This first build covers editing an existing board. It should be built so the same steps work for new boards later, without a second, separate tool.

## How we know it works

I build it, then run this exact P0009 job through AL/X myself on a fresh copy of the original, until it ends as cleanly as today's reference result. Then Friedl does the same job with her in his own words. Greptile reviews it only after that.
