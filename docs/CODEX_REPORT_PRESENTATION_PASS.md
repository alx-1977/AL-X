# D-084a presentation pass

Implemented on `board-edit-capability` at working-tree base `5a5caef`. AL/X can request a FireFli schematic redraw through the existing `delegate_board_job` capability with a job ID and `"pass": "presentation"`. The pass returns a normal schematic `step_ready` report naming the work copy, its current revision and what was tidied.

## Behaviour and judgment calls

| Setting | Default |
| --- | --- |
| `ALX_BOARD_PRESENT_PROVIDER` | `claude_subscription` |
| `ALX_BOARD_PRESENT_MODEL` | `claude-opus-5-5` |
| `ALX_BOARD_PRESENT_EFFORT` | `medium` |

- The Claude adapter uses AL/X’s configured subscription identity. The existing Codex subscription adapter is available as a configured alternative. Both use the job’s existing `BoardJobAgent` instance with a separate model session.
- Eligibility comes from the job journal. Its latest event must be a schematic `step_ready` report with a work copy and revision. The journal records the start before the first model call. A stopped or failed pass consumes that job’s one presentation attempt. Transport retries within the stretch can still use the journal, and AL/X can continue the existing engineering job.
- The fixed brief directs the agent to read the FireFli standard, use the existing drawing helpers, inspect every crop and look at every page of the finished schematic. It points to `/Users/friedlbasson/Desktop/VSC/FireFli-KiCad-Lib/tools/reference_p0005/` for the accepted redraw scripts. The scripts stay in the reference library.
- Every edit must carry an explicit empty intent. The KiCad provider scopes edits to the job’s current revision and allows changes to existing schematic files and their slot-grid data. It refuses revision reverts during the pass. The existing transaction rolls back refused edits. The gate also compares exported component values, footprints and fields because a change to those can preserve connectivity while changing the design.
- The job’s `<job_id>.calls.jsonl` labels presentation tool calls. Its `<job_id>.model-calls.jsonl` records presentation model interactions and the native session’s token totals when supplied. Presentation uses a final structured response so the subscription runner can return its session totals. A pause or an interrupted session can have unknown usage. Provider telemetry uses `<job_id>:presentation` as the task key.

## Law compliance

The authoritative path is the Core’s existing `delegate_board_job` executor → `BoardJobAgent.work` → the existing capability broker and safety gate → `KiCadProvider.edit` → the confined script runner and `_edit_gate` in the work-copy transaction. The registration and call-site search found one board-job registration and one checked KiCad edit entry point. Superseded or competing paths found for removal were **none**.

The Core decides whether and when to request the pass. The new input is a structured selector and the redraw brief is fixed. User conversation continues through the existing Core ingress. Job progress stays in the durable journal, and the existing Core goal store, stage approvals and write-back authority govern continuation. The pass reuses the existing KiCad and look primitives. It requires no new capability or Laws exception.

## Verification

- `python3 scripts/check_governance.py`, `python3 scripts/check_architecture.py` and `git diff --check` passed.
- `python3 -m pytest -n 4 -q -rs` passed with **5,726 passed, 11,365 subtests passed and 2 skipped** in 237.23 seconds. The skips were the opt-in synthetic conversation check and the unavailable RoomReader before-stiffener fixture. The first focused run encountered macOS’s nested sandbox restriction. The final suite ran outside the workspace sandbox so KiCad could apply its own script sandbox.
- The regressions exercise configuration and adapter selection, schematic readiness, one pass across restart, isolation to the job’s work copy, reporting its new revision, and separate usage records. With real KiCad, missing and non-empty intents were refused, connecting two pins rolled back byte-for-byte, a component value change was refused, a visual edit succeeded and its stale revision was refused.
- The FireFli standard, reference directory and a P0005 script were readable through the reference reader. A live subscription redraw, measured subscription-session usage and Friedl’s visual acceptance remain unverified.
