# Program lifecycle implementation

`run_program` has one execution path: the capability broker calls
`ProgramRunner.run`, which launches the independent `work` worker. Recovery
never launches or repeats a program.

The worker checks its deadline every 10 ms, with sleeps capped by the remaining
deadline. Descendant discovery runs no more often than every 100 ms and uses
process ancestry metadata. Argument/environment inspection is reserved for
cleanup, where inherited run tags can identify reparented descendants. On macOS
one bounded argument buffer is reused throughout that census, and known process
generations and their descendants do not need argument inspection.

Observed child generations are checkpointed under the existing run directory.
Worker cleanup and interrupted-run recovery use the same bounded stop primitive.
Each attempt has a monotonic half-second budget, including census polling. A
surviving child, unavailable census, or exhausted budget leaves the run pending;
only confirmed termination permits the write-once result publication.

`ProgramCompletionSource` reads already-published results on the conversation
path. Startup recovers only occasion-ledger claims. The existing
`DueCognitionSource` background tick invokes interrupted-run reconciliation off
the event loop and outside the Core-turn lock, even when autonomous cognition is
disabled. A nonblocking source lock and per-run worker lease prevent overlapping
attempts. Cancellation drains the active tick's worker before shutdown. Subsequent
ticks retry pending runs; no conversation or startup waits for cleanup.
