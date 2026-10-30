# Particle firmware work (D-062)

RoomReader V2 is `alx-1977/RoomReader_V2`, main, firmware 5.16. Firmware
belongs in GitHub. An iCloud project contains only a `firmware.md` link to it.

## Configuration

- `ALX_FIRMWARE_REPOSITORIES`: comma-separated GitHub owner/repository allowlist;
  default `alx-1977/RoomReader_V2`.
- `ALX_DATA_DIRECTORY`: default `~/Library/Application Support/ALX`. Firmware
  clones and artifacts are under its `firmware/` directory. This must be outside
  the canonical AL/X checkout; firmware coding refuses an inside location.
- `ALX_PARTICLE_CLI`: default `particle` on PATH.
- `ALX_PARTICLE_TOOLCHAIN_ROOT`: default `~/.particle/toolchains`.

`run_coding_task` accepts `firmware_repository` beside its existing job fields.
It creates `firmware/jobs/<call-id>/source`, clones and fetches origin main,
then uses the existing CodingAgent branch/session/verification/commit path.
Only the verified feature commit is pushed to that feature ref. An empty-ref
lease makes remote creation atomic and refuses replacing any existing branch;
an already-present identical commit is recognised as a successful prior push.
Neither the coding session nor the canonical AL/X checkout gains push authority.
Omitting the selector preserves the canonical coding behaviour.

Failed jobs retain their clone and checkpoint for the existing same-goal
`resume_job_id` mechanism. A failed push retains the verified commit; resume
checks its exact state and retries the push without reopening the session.
Firmware jobs create new branches; goal-branch continuation is not supported.

`compile_particle_firmware` takes `repository`, optional `job_id` (the original
coding call ID), optional `revision` that must resolve to the clone's HEAD,
and `platform`, `device_os`, `buildscripts`, defaulting to b5som, 6.4.1, 1.17.2.
Without job_id it reads an already configured clone at
`firmware/repositories/alx-1977--RoomReader_V2`. It never downloads firmware.
Compilation uses a disposable snapshot, with gcc-arm/10.2.1/bin and the highest
installed numbered buildtools version's bin first on PATH. Generated build files
cannot become session changes. The make process has a 600-second deadline.
Only a single fresh, nonempty binary (at most 16 MiB) is certified; missing or
ambiguous output fails. Results contain at most 40 error records, warning count,
artifact path and size and duration, without the build log.

USB listing must be complete and parseable. Bench flashing accepts only an
unchanged certified artifact and exactly one USB device, with an optional exact
device ID check. It invokes only `particle flash --local`. A flash timeout
reports an unknown outcome. Serial capture requires the single USB device,
accepts 1–60 seconds and retains at most 32768 bytes while draining the process.
Processes and descendants are reaped at the deadline.

Clones, artifact copies and digest records survive restarts until explicitly
deleted. Removing an artifact or changing its bytes removes its flash eligibility.
Temporary build snapshots are cleaned after each compile.

## Authority and architecture evidence

The production coding outcome retains `run_coding_task` → `CodingAgent`. The
four new outcomes each have one `ParticleLocal` method, one catalogue definition
and one executor, composed in `bootstrap/particle_local.py`. The architecture
manifest declares those entries. Searches of the existing Particle provider,
tools and bootstrap found no competing local compile, USB flash or serial paths;
superseded paths: none. Cloud product functions and variables retain
`particle_api_request`.

The broker and safety gate remain authoritative. Local reads and compilation
have SideEffect.NONE; USB bench flashing has standing effectful authority under
D-062. The existing Cloud API now requires exact-call approval for mutations;
GET remains available without approval. Bulk firmware publication is refused so
a fleet call cannot substitute for each device's approval. Core already binds
and consumes approvals per attempt. No OTA capability is introduced.

All new inputs are structured repository, revision, version, artifact, device or
duration fields. Raw conversation still enters Core alone. Capabilities return
facts; AL/X judges diagnostics, permissions and next steps. Goal state remains
in the existing durable store and resumes resolve retained clones by checkpoint.
KiCad gains the iCloud Projects default root; its existing work-copy, backup and
write-back approval path remains authoritative. No iCloud files are modified here.

Tests in `test_particle_firmware.py` use fake make/Particle executables and a
local bare remote, never hardware or GitHub. They cover filtered compilation,
artifact provenance, device refusals, serial/process bounds, registration,
configuration, isolated feature commits/pushes, origin-main freshness and restart
recovery after compile or push failure. Existing coding tests cover the omitted
selector path. Cloud authority tests cover read/write gating and fleet refusal.
Governance and architecture gates must pass. Git-dependent tests and other
sandbox-restricted checks require AL/X's outside-sandbox run before acceptance;
no real toolchain or hardware validation is claimed by these fixtures.

Exceptions: none. No SkyView, firmware-main merge, deployment or release work.
