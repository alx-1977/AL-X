# Proposal — Self-repair: AL/X diagnoses and fixes herself against real services

**Status:** The governing principle is approved as D-060 (§13b). The design here
(broker, one work room, review gate) is still a PROPOSAL: not approved, not implemented,
and needs Friedl's approval before any of it is built. AL/X's unpushed draft of a
D-060 "trial run" on `feature/trial-run-capability` is superseded by this proposal and is
to be dropped.
**Authority:** Subordinate to the Laws of AL/X, `IDENTITY_AND_MEMORY.md` and
`docs/LAW_ENFORCEMENT.md`.
**Sources:** Friedl's requirements of 2026-10-21; independent design reviews of the same
brief by Grok and by Codex; the usage-reader incident (#168, #169, #170). Vendor
capabilities cited in §4 come from those reviews and are **not yet verified** against
installed versions (see §15).

---

## 0. Governing principle (Friedl, 2026-10-22)

AL/X may do anything, except five things:

1. **Spend money** (paid reviews or paid services) beyond what Friedl has approved,
   without asking him.
2. **Send, delete, post or pay** on Friedl's behalf beyond what he has already approved,
   without asking him.
3. **Change her own laws, identity or governance records** without his approval.
4. **Read or reveal passwords and keys.** She uses them only through the tools that
   hold them.
5. **Change or delete Friedl's own files and designs** without asking him. Whatever
   file she changes, anywhere, its previous version is kept so it can be restored.

Everything else she does, sees and decides herself: coding, tests, reviews within
the allowance, merges by her own rule, GitHub, diagnosis and choice of approach.
Friedl decides *what* the code should do; AL/X decides *how*.

**Why:** AL/X was built allow-list style: she could see or do something only if a
tool for it existed. Every "she can't see that" and "she isn't allowed to do that"
this week was a missing tool, and patching them one at a time never ends (§2a).
This is the same principle Claude Code works by, with one difference: AL/X works
unattended, so the five exceptions are **enforced locks** (her safety gate and the
broker), not a person watching.

Her Laws already forbid the other thing Friedl does not want: fixed workflows,
phrase matching and hard-coded wording (Laws 1–3). Nothing here changes them.

## 1. Summary for Friedl

AL/X should be able to find and fix her own bugs, including bugs that only show up
against real services, without you or another AI diagnosing them for her.

The design has one idea at its centre. **The coding agent never holds a password.** A
small trusted program, the *broker*, holds every login and is the only door to real
services. AL/X and her coding agent ask the broker to look at a real service, or to run
their new code against it. The broker does it and keeps the complete raw result. Looking
is allowed freely. Anything that changes something — send, delete, post, pay — needs your
approval first, and that is enforced by the broker, not left to an instruction.

Five things follow:

- **She sees everything.** Every live run keeps the raw output (terminal bytes, the full
  email, the API reply) before anything cleans it up. Today's three failed usage-reader
  rounds would each have shown their cause the first time.
- **She tests before paying for a review.** A check refuses a Greptile request until the
  changed code has been seen working against the real service.
- **Faster fixes.** Today a small fix took her over an hour, where 20–30
  minutes is realistic. Most of it went on rule tests found only after each
  coding job and on running 4,400 tests one at a time (§13a). That is fixed
  first.
- **One sandbox.** The separate experiment sandbox is removed; experiments, coding,
  diagnosis and testing use one work room.
- **New logins need almost no work.** Reading through a new login works through the same
  door. For each new service AL/X drafts a one-page note of what is safe to do with it,
  and you approve it once.

What you give up: some care about what reaches OpenAI and Anthropic. Passwords never do,
but emails or screens shown to the coding agent do. §9 sets the rule.

What you still decide: changes to the broker's own rules, anything that sends, deletes,
posts or pays, and the one-page note for each new service.

---

## 2. The problem

Today a fix is made by the coding agent (Codex) in a locked box: no network, no logins,
no runtime data, no git. Its tests and AL/X's verification use made-up samples only. Bugs
where her code meets a real service therefore pass every test and fail live.

Evidence from 2026-10-21:

- **Usage reader** (drives the Claude CLI's `/usage` screen in a pseudo-terminal). Three
  merged fixes failed live, each costing a review, a merge and a restart. The causes were
  terminal codes the renderer rejected, keys pressed during Claude's startup redraw, and a
  no-break space after the prompt. An outside agent with an ordinary terminal found all
  three in minutes by running the real program. AL/X could not: her reader kept only
  cleaned text, and she had no way to run anything against the real service.
- **Email.** A DigiKey shipping email read as "." because the reader prefers a near-empty
  plain-text part over the HTML part. Only the real message shows that.
- **DHL.** API credentials exist; nothing uses them.

The paid review was being used as the debugger. A review checks whether a change is
acceptable; a live run checks whether it works. The live run has to come first.

## 2a. What she could not see about her own work (audit, 2026-10-22)

Checked against the code and her stored records, not from memory.

| # | She could not see | Effect |
| --- | --- | --- |
| 1 | Which coding tool and model is configured: not among the setup facts she is shown each turn | She did not know Grok was on |
| 2 | Which tool and model did a coding job, or how long it took: the session reports both, but they are dropped before the result is stored | A wrong Codex/Grok comparison on 2026-10-22 |
| 3 | When any action started or how long it took: an attempt records only when it was recorded | She cannot tell what is slow |
| 4 | What her work costs: every reasoning call's tokens are stored, but no tool reads them | She cannot weigh a fix against its cost |
| 5 | Why an outside service failed: many failures carry only a code (`provider_failed` for web pages and invoice capture, `operation_refused` without a reason, `message_unavailable`). Merge refusals already carry GitHub's own message; most others do not | She cannot diagnose and asks Friedl |
| 6 | Her log beyond about 24 hours (this run and the one before) | Older problems are out of reach |
| 7 | The coding session's own report after the turn ends: only the result survives | She cannot look back at what the agent said it did or skipped |

What already works: goal history, the trace with durations (30 days), GitHub checks
with log tails, review text as written, her own source at the running commit and
main, her allowance, and undelivered messages.

## 3. Requirements (Friedl, 2026-10-21)

1. **Test before review.** No paid review for code not seen working against the real
   service.
2. **Full visibility.** Raw output, logs, runtime data, real screens; the ability to run
   what is needed to diagnose and test.
3. **No restriction that brings us back here.** Where something is a genuine security
   risk she asks Friedl first, but the ability exists.
4. **Future-proof.** New logins and services work without a per-service whitelist in the
   execution engine.
5. **One sandbox**, not two.
6. **Simple and cheap** in Claude allowance and review credits.
7. (Implied) **Friedl is not in the loop** for routine diagnosis and repair.

## 4. What the vendors provide

Both reviews agree that the established pattern is *isolated execution + brokered
credentials + recorded evidence + controlled promotion*, and that no vendor ships the
whole loop.

- **Anthropic.** Claude Code's sandbox (Seatbelt on macOS) routes shell traffic through a
  local proxy that checks the destination and can substitute a placeholder credential for
  the real one on the way out. The Agent SDK has permission hooks. Anthropic recommends
  external credential proxies or custom tools for authenticated access. Limits: the proxy
  does not understand the action inside a connection; file tools, MCP servers and hooks
  are not inside the Bash sandbox.
- **OpenAI.** Codex has OS-enforced sandboxing, permission profiles and network controls.
  Its network proxy speaks HTTP and SOCKS; in its limited mode only GET, HEAD and OPTIONS
  pass and other TCP is blocked. This matches what we measured: with
  `--enable network_proxy`, IMAP and everything else were blocked. The Agents API docs
  recommend keeping credentials outside execution environments with injection at a
  trusted proxy; that API is not part of the ChatGPT subscription.
- **Docker Sandboxes** offers local microVMs with a host-side credential proxy (HTTP
  injection only).
- **Replay tools** (for example VCR.py) record HTTP for tests but cannot prove that today's
  service, login or terminal timing works.

The part we must build ourselves is small: a broker that knows the difference between
looking at a service and changing it, for IMAP, REST APIs and terminal programs alike.

## 5. Architecture

Five parts. Three exist today.

| Part | Exists? | What it does |
| --- | --- | --- |
| **Core** | yes | Decides meaning, what to try, and whether a fix worked. Keeps a case file per bug. Commits, pushes, requests review, merges, restarts. |
| **Work room** | replaces two | The one sandbox. Coding sessions, experiments, diagnostic programs and tests run here. |
| **Broker** | new | Holds every login. The only door to real services. Performs *observe* freely and *act* only with authority. Writes evidence and signed run records. |
| **Evidence store** | new | Raw captures, case files, run records and authority records on disk, outside git. |
| **Review gate** | new | Refuses a paid review unless a signed live run of the exact candidate passed. |

Plus one small addition: a **supervisor** outside AL/X that restarts the approved runtime
and keeps crash evidence, because she cannot repair herself while the process that
reasons is down. It has no judgement and no voice.

### 5.1 Core

Core owns interpretation and judgement, unchanged. For each bug it keeps a one-page
**case file**: symptom, evidence references, current hypothesis, what was tried, result,
and its own assessment of what a live run proved and what remains uncertain. The next
reasoning step loads the case file, not the history. Core creates authority records when
an action needs one and runs promotion (commit, push, review, merge, restart) exactly as
today under D-026, D-046 and D-046a.

### 5.2 Work room

One implementation, one lifecycle, a fresh disposable instance per job.

- **May:** run any program; edit the candidate copy of the code; read evidence and
  runtime-data snapshots that Core grants; call the broker; use network through the
  broker.
- **May not:** read the Keychain, `.env` or any credential; write git metadata; hold
  GitHub credentials; write AL/X's live runtime storage; modify the broker or its rules.
- **Candidate copy, not the live checkout.** The job works on a private copy, so a
  half-finished edit can never be what a restart loads.
- **Experiments** get a scratch folder instead of a repository copy. Same room, a
  different writable folder, chosen by Core at launch.
- **AL/X's own verification runs here too.** `pytest` executes repository code, so a fixed
  command list is not a security boundary.
- **On this Mac:** native macOS confinement, starting from the existing coding
  containment, preferably under a dedicated unprivileged macOS user. That separates it
  from the Keychain that holds Claude's and GitHub's logins. **On dedicated hardware:** the
  same interface behind a VM; the broker stays on the host. Mac-specific integrations need
  a macOS guest.

### 5.3 Broker

Ordinary Python that Core starts. The running broker is the last version Friedl
approved, never the file a job just edited. It reads logins from wherever AL/X keeps them
(today `.env`; the Keychain later). Passwords stay in its memory and never appear in
arguments, logs, evidence or model context.

It performs two kinds of operation.

**Observe** is a closed list of mechanical operations that change nothing:

- Run a local program in a pseudo-terminal and keep the raw bytes with timings and input
  events. This covers the Claude usage screen.
- HTTPS GET and HEAD with the matching credential attached by the broker.
- IMAP `EXAMINE`, `SEARCH` and `FETCH` with `BODY.PEEK`, so looking never marks mail
  read. The capture includes the full MIME tree, each part's type, size and encoding, and
  which part the parser chose.
- Further operations a connection note names as safe (§6).

**Act** is everything else: POST, PUT, DELETE, SMTP, IMAP store/delete/move, payments,
IoT commands, webhooks. The broker refuses *act* unless Core presents an authority record
bound to the exact account, operation, parameters and expiry, with replay protection.
Send, delete, post and pay require Friedl's approval in that record. If an operation is
not clearly observe or act, the broker refuses it and returns it to Core as ambiguous. If
an *act* times out with an unknown result, the broker reports the uncertainty and never
retries on its own.

### 5.4 Running the candidate code live

Fetching real data is not enough. The changed code itself must meet the real service,
otherwise the review gate only proves that the old code works. Two forms:

- **Parsers and API clients** (mail, DHL, Xero reads): the broker fetches the real bytes,
  and the candidate code runs on them in the work room. For an API client, the
  candidate's requests go through the broker's observe operations; the broker attaches
  the credential and refuses anything outside the connection note.
- **Interactive programs** (the Claude usage screen and the like): the broker owns the
  real session and pseudo-terminal. The candidate code in the work room drives it through
  the broker, which forwards keystrokes and returns raw output. The candidate never holds
  the login and cannot reach anything else through that session.

### 5.5 Evidence store

A directory on this Mac, outside git, never sent to a reviewer. For every live run:

| Evidence | Kept |
| --- | --- |
| Terminal | Original bytes, chunk boundaries, timestamps, input events, terminal size, locale, program version, exit status |
| HTTP | Request and response metadata, status, body bytes, timing; never authentication material |
| Email | Original MIME bytes and structure |
| Screens | Timestamped captures tied to the session and action |
| Runtime data | Consistent snapshots with schema and code revision |
| Test runs | Candidate digest, environment, command, granted authority, outputs, result |

Originals are never normalised or cleaned. Parsed views are stored beside them. Each
capture gets a local summary: unusual bytes and non-ASCII code points, terminal codes,
MIME tree, status line, and a short window around the anomaly. The summary is what goes
to a model by default (§9). A capture that fails a test becomes a regression fixture in
the repository once secrets and personal content are removed. That is how the live
usage-screen recording became the tests for #170.

### 5.6a Seeing everything about herself

One general, read-only capability replaces the specific self-readers. She can read:

- her source and its history at any commit (log, show, diff, blame), independently
  of the checkout;
- every file in her runtime storage and every retained log;
- every one of her stores, by read-only query (opened read-only; bounded rows);
- the configuration she runs with: which models, providers, switches and limits;
- the evidence store (§5.5), including raw outputs.

**The only exception is passwords and keys.** `.env` and other credential files,
login directories (Claude, Codex, Grok) and the Keychain are refused outright, and
anything key-shaped in any output is masked, as `read_own_log` already does.

Under Law 0 it replaces `read_own_source`, `read_own_log`, `read_own_trace` and
`read_own_goals`; each is deleted with its registrations. The capabilities that read
outside services (`read_claude_usage`, `read_reader_log`, GitHub checks and reviews)
stay: they read other systems, not her records.

### 5.6b Recording everything

What is not recorded cannot be seen. Recording is a rule at the one point every
action passes through, not a list per tool:

- **The capability broker records** every call's start and end time and duration,
  for every capability, past and future.
- **Every failure carries the outside service's own words:** HTTP status and its
  message, or the program's error output, bounded and with keys masked. A code alone
  is not enough. An architecture test fails any provider failure path that drops
  them.
- **Coding results keep** the tool and model that ran, the session's start, duration
  and its own report.
- **Her setup facts include** the coding tool and model, beside the reasoning model.
- **Logs are kept** for 30 days, rotated by size, and are all readable by §5.6a.

### 5.6 Review gate

Before any paid review, a script checks that:

1. law-gates and the required tests passed for the exact candidate;
2. if the change touches code that talks to a service or the broker, a broker-signed live
   run of that exact candidate exists and passed;
3. Core's case file records what that run proves and what it does not.

The gate checks existence, signature and match. It does not judge adequacy; AL/X does.
An inconclusive live run blocks review. A change that touches no service needs no live
run, and Core records why. A service change that cannot be run live stays *unverified*
and needs Friedl's explicit exception before review. The signing key belongs to the
broker; the work room cannot read it, so a job cannot forge a passing run.

## 6. New services: the connection note

Classifying by HTTP method alone is wrong. A GET can spend quota, and a POST can be a
harmless search. Each service therefore gets a short **connection note**:

- the account and environment it represents;
- which operations are observe (allowed freely), with limits on rate and spend;
- which operations are act, and which of those need Friedl;
- what evidence may be kept, and what may be shown to a model.

AL/X can discover a new login, read the service's documentation and draft the note.
Friedl approves it once, and after that it works with no code change in the broker or the
work room. Until a note exists, a new login supports only the generic observe list (§5.3).
This is the honest form of "no per-service whitelist": the execution engine has no
hard-coded service list, and authority over what a service may do is still explicit.

Preference order for any credential: (1) a credential the service itself limits
(read-only account, scoped token); (2) broker-enforced operations; (3) short-lived,
explicitly authorised elevated access.

## 7. Enforced and instructed

| Concern | Enforced by | Instructed only |
| --- | --- | --- |
| Who can reach a real service | Broker is the only process with credentials | |
| Who can see a password | Broker only | |
| Look versus change | Broker's observe list, connection notes, authority records | |
| Push, merge, restart | Work room has no GitHub credentials and no git write; Core's promotion path only | |
| Test before review | Review gate | |
| Broker rules change | Running broker is the approved version; changes need Friedl | |
| What a bug means, what to edit | | Core, from the case file |
| How to write the fix | | The coding agent |

An instruction is a hint. A missing credential, a refused operation or a signature check
is a lock.

## 8. Credentials

- No password, token, cookie or authenticated browser profile is placed in the work room.
- All of AL/X's logins, current and future, live with the broker. No per-login list
  decides who may use them; connection notes decide what may be done with them.
- GitHub credentials are not available to the broker's observe operations either.
  Publishing is Core's promotion path only.
- Measured on 2026-10-21: an empty `GH_CONFIG_DIR` locks out `gh` inside a Codex
  sandbox. Clearing git's credential helper is untested, and the Keychain is shared by
  the macOS user. That is why §5.2 prefers a separate macOS user over environment tricks.

## 9. What reaches model providers

Passwords never do. Content does whenever it is shown to a model. Rule:

- Core and the coding agent receive the **summary** of a capture by default.
- Core may widen what a model sees with a recorded decision when the summary is not
  enough: the bytes around an anomaly, one MIME part.
- Full mail bodies, invoices and personal data stay on this Mac unless widened that way.
- A pull request names the run record that passed; it never attaches screens, mail or
  responses.

## 10. Retention

Proposed, for Friedl's acceptance: routine captures 7 days; captures attached to an open
case file kept until it closes, then 30 days; run records and case-file summaries kept as
part of AL/X's history. Deletion covers derived summaries. Local deletion cannot recall
content already sent to a provider; §9 limits that.

## 11. Fit with the laws and existing decisions

**Law 0.** One production outcome: *AL/X investigates a failure, changes a candidate,
obtains live evidence and advances it through the existing promotion path.* One door to
real services: the broker. The existing providers (`icloud_mail`, `xero`, `dhl`,
`particle`, `claude_usage`) remain the authoritative integrations and move behind the
broker. No parallel "diagnostic" copy of any integration is built. Paths to delete when
replaced:

- the D-027 experiment sandbox (`bootstrap/sandbox.py`, `tools/sandbox.py`,
  `run_sandbox_experiment`, its registrations);
- the separate coding containment as an independent owner (`coding_containment.py`, the
  Codex permission profile), merged into the work room;
- the post-session allowlisted command executor as the thing that counts, once run
  records count;
- direct credential reads in providers, once they go through the broker.

**Law 1.** Core alone interprets. The broker, gate and work room take structured inputs
and do not read Friedl's words or decide meaning. Capture content is external, untrusted
data and is never followed as an instruction.
**Law 2.** The broker and gate are named deterministic sequences, recorded in the
decision.
**Law 3.** Anything ambiguous, including an operation that is neither clearly observe nor
act, or an inconclusive run, returns to Core. Authority records are durable. Case files,
evidence and run records survive restarts.

**Decisions this would amend or replace:** D-027 (replaced by the work room), D-028 and
D-052/D-052a/D-052b (coding containment and storage access move to the work room's rules:
read-only runtime snapshots, no live storage writes), D-020-style deterministic-execution
records for the broker and gate, D-046/D-046a (unchanged allowance; the gate adds a
precondition), D-026 (unchanged; the gate checks evidence, not reviews), D-059 (the usage
reader moves behind the broker).

## 12. Risks

**Accepted:**

- Some services have no truly side-effect-free read. Prefer peek and examine, and record
  it in the connection note where none exists.
- A summary can hide a bug. The raw capture remains, and Core can widen it.
- Live services are flaky. An inconclusive run blocks review rather than passing.
- Native macOS confinement shares a kernel. Accepted on the development Mac; a VM on
  dedicated hardware.
- Vendor sandbox flags will keep changing. Safety lives in the broker and the account
  boundary, not in those flags.

**Not accepted:**

- the coding agent holding credentials or reading the Keychain;
- the coding agent pushing, merging or restarting;
- send, delete, post or pay without Friedl's recorded approval;
- "test before review" as an instruction only, or any flag that skips the gate;
- a job editing the running broker or its rules;
- evidence, mail or secrets in the repository or sent to a reviewer;
- unreviewed code replacing the running runtime.

**To watch over the next year:** prompt injection through mail, web pages or terminal
banners (the broker never acts on text it fetched; send and pay still wait for Friedl);
stale fixtures; CLI redraw changes; credential-bearing screenshots; dependency install
scripts; ungoverned MCP connections; privilege creep through broker rule changes. Pin the
relevant tool versions, and run a small boundary test when any of them changes.

## 13. Cost

- Live runs and summaries are local Python, so they cost no tokens.
- Core reads the case file, not a replay of the whole session.
- The coding session is kept across investigation steps. Targeted tests run while
  editing; the full suite runs once per final candidate.
- One Greptile review, after a live run has passed.
- Measure first: Core's ~55,000-token context per step should be broken down into fresh,
  cached and output tokens before more infrastructure is added (separate work).

## 13a. Speed of the repair loop

Measured on 2026-10-22, on the "stop repeat mail announcements" fix (PR #172).
The change was small, and the same fix done directly would take about 20–30
minutes. AL/X took well over an hour:

| Step | Took |
| --- | --- |
| First coding job | 8 min |
| GitHub checks | 6 min |
| Greptile review 1 | 8 min |
| Coding job: fix the review findings | 10 min |
| Coding job: failed on a forbidden word in a rule test | 10 min |
| Retry: failed on another forbidden word | 18 min |
| Third try, then review 2 | … |

Model thinking was a small share (about 30–40 s a step). The time went to the
loop around it:

1. **Rule tests found last.** The coding session runs only the tests for the
   code it changed. The law-gate and wording tests run only in AL/X's full
   suite after the session, so each failure costs a whole new coding job
   (10–18 min) for a one-word fix. *Fix:* the coding session runs the
   architecture, governance and wording tests itself before it finishes, and
   the same session corrects what they find.
2. **A fresh session per round.** Each new coding job re-reads the code from
   scratch. *Fix:* a correction continues the same session (§13).
3. **The full suite runs one test at a time.** Measured: 4,397 tests, 393 s.
   88 % take under 0.1 s each (about 19 s together). The time is in 544
   slower tests (374 s), almost all exercising the coding machinery itself
   (real git repositories, processes, timeouts) plus 4 real-KiCad tests
   (15 s). *Fix:* run the suite in parallel (expected 1–2 min instead of
   6½), and run the slow coding-machinery tests in a coding job only when
   that machinery changed. GitHub's law-gates still run everything before
   merge.
4. **A fixed 30-second pause** between installing an execution plan and its
   first step, seen on most plans that day. *Fix:* find its purpose; remove
   it or make it conditional.
5. **Stale tests.** None found by timing. Do a file-by-file check when the
   experiment sandbox is removed (§11), since its tests go with it.

These do not depend on the broker and are built first (§14, phase 0).

## 13b. Proposed decision D-060 (text for Friedl's approval)

> **D-060 — AL/X may do anything except five things** (as approved; the fifth and
> three settled conflicts were added on review of the earlier decisions; the
> recorded text is in `governance/DECISIONS.md`).
> AL/X may do anything except: (1) spend money beyond what Friedl has approved
> without asking him; (2) send, delete, post or pay on his behalf beyond what he has
> already approved without asking him; (3) change her laws, identity or governance
> records without his approval; (4) read or reveal passwords and keys. Everything
> else she sees, does and decides herself, including coding, tests, reviews within
> the allowance, merges by her own rule, GitHub, diagnosis and the choice of
> approach. Friedl decides what the code should do; she decides how. The four are
> enforced by her safety gate and the broker, not by instruction. New capabilities
> are designed against this list: a missing capability is a defect to fix, not a
> boundary. This decision does not change the Laws; fixed workflows, phrase routing
> and hard-coded wording remain forbidden by them. Existing decisions stay in force
> until the work below replaces them; where one is narrower than this principle
> without serving one of the four, it is to be amended.

## 14. Build order

Each phase ends with a demonstration on a real case. Stop after each phase and use it on
the next real failure before building more.

0a. **See and record everything (§5.6a, §5.6b).** The general read capability,
   recording at the broker, failures with the service's own words, coding results
   with tool, model, duration and report, setup facts, 30-day logs. *Done when*
   every gap in §2a is closed and AL/X can answer "which tool did job X, how long
   did it take, what did it cost, and why did it fail" from her own records.
0. **Speed of the loop (§13a). Done: PR #173, merged 2026-10-22.** Rule tests inside the coding session,
   corrections in the same session, parallel test runs, slow machinery tests
   only when affected, and the 30-second plan pause explained or removed.
   *Done when* a small fix like PR #172 goes from coding to review in about
   15 minutes, with no coding job lost to a rule test.
1. **Raw capture.** The usage reader already returns `raw_output_tail` (#170). Extend this
   to full timed captures with input events, stored in the evidence store, with a summary.
   *Done when* a deliberately broken renderer's failure is diagnosable from the capture
   alone.
2. **Broker, observe only.** Socket API; operations: pseudo-terminal session, IMAP peek
   with MIME tree, HTTPS GET. *Act* returns "needs authority". The work room calls it.
   *Done when* the DigiKey "." email is diagnosed and fixed by AL/X from a broker capture,
   with no outside help.
3. **Review gate and case files.** *Done when* a review request on a service change
   without a signed passing run is refused, and one with it goes through.
4. **One work room.** Merge the experiment sandbox into it and delete D-027's paths. Run
   AL/X's verification inside it. Separate macOS user. *Done when* a scratch experiment
   and a coding job both run in it, and the old sandbox is gone.
5. **Act with authority.** Friedl's approval flow for send, delete, post and pay;
   connection notes for mail, Xero, DHL and Particle. Built before the first bug that is
   actually in a send or payment path.
6. **Supervisor.** Restart and crash evidence.
7. **Dedicated hardware.** The same interfaces behind a VM.

Before any phase grants live access, prove by test that candidate code cannot read
credentials, change production state, publish to GitHub, alter broker rules or forge a
run record.

## 15. Open questions for Friedl

1. Approve the design (broker, one work room, review gate)? D-060 itself is approved.
2. Retention: 7 days routine, 30 days after an incident closes?
3. Who builds phases 1–3: Claude Code (the broker cannot be tested by AL/X before it
   exists), AL/X, or Claude Code for the broker with AL/X doing the rest?
4. A separate macOS user account for the work room on this Mac?
5. Start after the weekly allowance resets on 2026-10-26?

Before implementation, verify the vendor features cited in §4 against the installed
Claude Code and Codex versions, and verify that each lock in §7 holds, by test.

## 16. Considered and rejected

- **Give the coding agent every login and network.** Puts secrets into the agent's
  context and from there with the model provider; "don't send" becomes an instruction,
  not a lock. Claude Code's own safety classifier refused to build it.
- **Codex per-domain network rules.** Proxy-based; blocks IMAP and every non-HTTP
  protocol; cannot tell a fetch from a delete on an allowed host.
- **Trial run with a fixed list of trial-able services** (AL/X's D-060 draft). A
  per-service list returns us here with every new login, and it needed a one-service
  network room that does not exist.
- **Replay-only testing.** Cannot catch today's failures. Recorded captures remain useful
  as regression fixtures after a live run has passed.
