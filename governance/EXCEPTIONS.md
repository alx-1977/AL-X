# Approved exceptions to the Laws of AL/X

An exception is valid only when Friedl explicitly approves it before implementation and this register records:

- a unique identifier;
- the exact law and exact code or behaviour affected;
- why the exception is necessary;
- alternatives considered;
- risks and safeguards;
- the narrow approved scope;
- approval date;
- expiry date or mandatory review condition.

Silence, prior implementation, model recommendation, technical convenience, and approval of a broader feature do not constitute approval of an exception.

---

## EX-001 — Origin-selected Core for the D-024a Luna evaluation

### Register metadata

- **Law:** Law 0 (one outcome, one production path) and Law 1 (AL/X decides meaning).
- **Scope:** `OriginSelectedReasoner` in `src/alx/bootstrap/reasoning.py` and its single composition site in `src/alx/bootstrap/live_voice.py`; `PERSON_TURN` → OpenAI `gpt-5.6-sol` / `medium`, and `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED` → OpenAI `gpt-5.6-luna` / `max`. Nothing else.
- **Necessity:** the evidence for choosing a permanent Core topology for autonomous turns does not exist, and cannot be produced without running both configurations under one identity. See "Why this is necessary" below.
- **Alternatives:** Sol for all turns; Luna for all turns; delaying the experiment. All considered and set out below.
- **Risks and safeguards:** inconsistent judgement between configurations, drift into semantic routing, an unconcluded experiment becoming architecture, and procedural drift; guarded by an origin-only architecture gate, one CoreAgent/broker/gate, no fallback, off-by-default operation and a hard spend fuse. Set out in full below.
- **Approved by Friedl:** yes, explicitly, for this exact wording and scope.
- **Approval date:** 2026-09-03.
- **Expiry or review condition:** conclusion of the D-024a Luna experiment, requiring an explicit Friedl-approved decision recording the permanent Core topology. Not renewable by silence.

| Field | Value |
| --- | --- |
| **Exception ID** | **EX-001** |
| **Status** | **APPROVED** |
| **Approval date** | **2026-09-03** |
| **Approved by** | Friedl |
| **Laws affected** | **Law 0** (one outcome, one production path) and **Law 1** (AL/X decides meaning) |
| **Affected code** | `OriginSelectedReasoner` in `src/alx/bootstrap/reasoning.py`, and its single composition site in `src/alx/bootstrap/live_voice.py` |
| **Affected behaviour** | `PERSON_TURN` → OpenAI `gpt-5.6-sol` / `medium`; `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED` → OpenAI `gpt-5.6-luna` / `max` |
| **Mandatory review / expiry** | Conclusion of the D-024a Luna experiment. Not renewable by silence. |
| **Related decision** | D-024a in `governance/DECISIONS.md` |

### Procedural history

The experimental split was initially implemented before the required exception
was raised. That sequencing violated the exception procedure recorded at the
top of this register.

That implementation was removed in commit `2037eb4`, before this exception was
approved and before any merge or live activation. This approval therefore does
**not** retroactively legitimise the earlier implementation. The experiment may
only be re-implemented after this approved exception is committed.

The removal is recorded in the branch history rather than erased from it.

### Laws affected

**Law 0 — One outcome. One production path.** The production outcome "a Core
reasoning decision for one turn" is reached through one of two `ModelReasoner`
instances, selected before reasoning begins. Two instances of the same
authoritative path exist where the law requires one.

**Law 1 — AL/X decides meaning.** Deterministic code selects which reasoning
authority produces the decision, before that authority has reasoned. The
selection reads only provenance and never content, so no interpretation of
Friedl occurs; the exception is recorded against Law 1 regardless, because the
choice of which mind answers is made outside the mind.

### Exact code and behaviour affected

- `OriginSelectedReasoner` in `src/alx/bootstrap/reasoning.py`, and only its
  `decide` method, whose entire selecting logic is:

  ```python
  if context.origin.is_autonomous:
      if self._autonomous is None:
          raise AutonomousReasonerUnavailable(context.origin.value)
      return self._autonomous.decide(context)
  return self._conversational.decide(context)
  ```

- Its single construction in `src/alx/bootstrap/live_voice.py`.

Nothing else. No other module may select, construct, or reference both
reasoners.

### Approved behaviour

| Origin | Model | Effort |
| --- | --- | --- |
| `PERSON_TURN` | OpenAI `gpt-5.6-sol` | `medium` |
| `EXTERNAL_EVENT`, `WORK_COMPLETED`, `SELF_REQUESTED` | OpenAI `gpt-5.6-luna` | `max` |

Selection is strictly by `CognitionOrigin`, strictly in composition, and
nowhere else. Both paths use the same `CoreAgent`, Laws, identity, contracts,
continuity context, goals, memory, notebook, `CapabilityBroker`, `SafetyGate`
and capability set. The only permitted differences are provider, model,
reasoning effort, and the provider-side token bounds the autonomous
reservation is computed against.

An autonomous origin with no autonomous reasoner configured raises
`AutonomousReasonerUnavailable` and is refused. There is no fallback to the
conversational Core.

### Explicitly prohibited by this exception

This exception authorises the arrangement above and nothing adjacent to it.
The following remain full violations and are not covered:

- routing by topic, subject or keyword;
- routing by capability;
- routing by goal or goal state;
- routing by content, intent, importance, urgency, priority or domain;
- any classifier, scorer or other pre-Core intelligence deciding which
  reasoner answers, or whether an occasion deserves cognition at all;
- a sidecar, curiosity, personality or relationship model;
- a generic model-router abstraction, registry, strategy object or lookup
  table, whether or not it currently routes on origin;
- any third or further reasoning-authority path;
- extending origin selection to anything other than the two models named
  above.

### Why this is necessary

D-024 gives AL/X occasions to think when nobody has asked her to. Whether that
produces something worth having is an open question, and the model-and-effort
configuration answering an unprompted turn is one of the few variables likely
to decide it. The evidence for choosing a permanent Core does not exist yet.

The experiment exists to produce that evidence: whether the Luna/`max`
configuration produces better autonomous cognition than the conversational
Sol/`medium` configuration, or worse, or indistinguishable. Nothing is assumed
about which will prove better; that is the question, not the premise.

Evidence sought covers continuity and personality quality, autonomous
judgement, the interests she chooses, how often she requests further cognition,
speech versus silence, and real cost. Running both configurations under one
identity and one capability environment is the only way to compare them without
comparing two different minds.

### Alternatives considered

**Sol for all turns.** Preserves one Core exactly and needs no exception. It
answers a different question than the one being asked: whether a different
model-and-effort configuration performs differently on unprompted turns cannot
be learned by never varying it. Available at any time as the conclusion of the
experiment rather than a substitute for it.

**Luna for all turns.** Also preserves one Core, and removes the split by
moving conversation onto a `max`-effort model. Rejected on latency and cost for
ordinary conversation, and because it changes the Core Friedl actually talks to
in order to answer a question about autonomous turns.

**Delaying the experiment.** Ship Phases 0–7 with one Core and add the second
later. Rejected because the observation period is the point of Phase 8, and the
Luna question would still require this same exception whenever it was asked —
the deferral buys nothing but time.

### Risks

**Inconsistent personality and judgement between configurations.** Her
memories, preferences and self-model would be written by two configurations and
read by both. If Sol and Luna differ materially in temperament, the entity
deciding whether to interrupt Friedl is systematically not the entity he
converses with. This is a continuity problem in a workstream whose purpose is
continuity, and it is among the primary things the experiment is meant to
detect.

**Accidental evolution into semantic routing.** The likeliest regression. A
future change adds "only use Luna when the goal is stale" or "when the topic is
technical," and origin selection becomes topic routing without anyone deciding
to build it.

**Two reasoning authorities becoming permanent architecture.** An experiment
nobody concludes becomes the architecture by default. The failure mode is not a
bad decision; it is no decision.

**Procedural drift.** This exception was itself raised after implementation
once. The safeguard is that the implementation was removed rather than
grandfathered, and that this entry records the fact rather than obscuring it.

### Safeguards

- `scripts/check_architecture.py` enforces that only
  `bootstrap/reasoning.py` and `bootstrap/live_voice.py` may name both
  reasoners, and that the selecting class may read nothing semantic — a
  24-token blocklist covering topic, capability, goal, notebook, research,
  memory, intent, importance, priority, urgency, domain, sentiment, score and
  interest. Each rejection is proven by injecting the violation.
- One `CoreAgent`, one `CapabilityBroker`, one `SafetyGate`, one capability
  registry. `CoreAgent`, the broker and the gate are never told which model
  answered, and tests assert they cannot discover it.
- No fallback: an autonomous origin without a configured autonomous reasoner
  is refused, never answered by the conversational Core. A test asserts zero
  conversational calls across all autonomous origins.
- Autonomous cognition is off by default and separately fused by a hard daily
  spend ceiling.
- The arrangement is recorded in D-024a as explicitly experimental and
  without standing as architecture.

### Narrow scope

This exception covers the two named models, selected by `CognitionOrigin`, in
`OriginSelectedReasoner` and its single composition site, for the duration of
the D-024a evaluation. It authorises no other dual-Core arrangement, no model
routing of any kind, and no reasoning authority beyond the two named.

### Expiry and mandatory review

This exception expires when the D-024a Luna experiment is concluded.

Conclusion requires an explicit Friedl-approved decision recording the
permanent Core topology — Luna, Terra, Sol, or another single authoritative
Core configuration. Until that decision is recorded, the arrangement has no
standing as architecture; once it is recorded, this exception lapses and is not
precedent for any future dual-Core or routing proposal.

The exception does not renew by silence, by continued operation, by the
experiment producing good results, or by the passage of time.

---

## EX-002 — Merging PR #14 at `16bf2d9` without its `Greptile Review` status

### Register metadata

- **Law:** Law 0 enforcement via `docs/LAW_ENFORCEMENT.md` gate policy — "A change fails if any applicable automated gate fails" and "Disabling a gate is not a workaround". `main` requires the status checks `law-gates` and `Greptile Review`; this suspends the second one for one merge.
- **Scope:** Pull request #14 only. Authorised implementation head `16bf2d9740098e69f9221561607777dfa1fa4896`; merge head is the single commit that adds this exception record on top of that implementation and changes nothing else. The `Greptile Review` required-status requirement on `main` is suspended for the duration of that single squash merge and restored immediately afterwards. Nothing else.
- **Necessity:** The account's Greptile review credits are exhausted, so the required `Greptile Review` status cannot be produced for this head by any legitimate means. Waiting would block AL/X development for the remainder of the billing period.
- **Alternatives:** wait for credits; merge the reviewed parent `4cd1035` instead. Both rejected below.
- **Risks and safeguards:** one unreviewed commit above a 5/5-reviewed parent, narrowly scoped to canonical provenance; guarded by the reviewed parent, the full test suite, both law gates, `law-gates` in CI, and a mandatory retrospective review. Set out in full below.
- **Approved by Friedl:** yes, explicitly, for this exact PR, this exact head and this exact mechanism.
- **Approval date:** 2026-09-04.
- **Expiry or review condition:** expires immediately once PR #14 is merged and `Greptile Review` is restored as a required check. A retrospective Greptile review of `16bf2d9` remains outstanding until credits allow it.

### Authorised target

The implementation authorised here is `16bf2d9`. The commit actually merged
is the one that adds this exception record on top of it and changes nothing
else: the trees under
`src/`, `tests/`, `requirements.txt` and `architecture/` are byte-identical
between the two, and `src/` carries the same tree hash `a0b8198` in both. The
governance record therefore lands on `main` together with the merge it
authorises, rather than the merge arriving unexplained.

Any commit other than these two is outside this exception.

### Why this is necessary

`Greptile Review` is a required status check on `main`. It is produced by a paid external service, and the account has no review credits remaining. The check therefore cannot report on `16bf2d9` at all — this is not a failing gate or a false positive, it is a gate that cannot run.

Three mechanisms were available and two are refused outright. Posting a synthetic `Greptile Review` success status would fabricate evidence that a review happened, which is worse than any merge it would unblock. An administrative override bypassing all branch protection would suspend `law-gates`, linear history and conversation resolution along with it, none of which are obstructed. This exception therefore suspends exactly one named check, for one named head, and restores it immediately.

### Alternatives considered and why they were rejected

**Wait for credits.** Correct in principle and rejected on cost: it blocks the branch, and everything built on it, for the remainder of the billing period. The delay buys a review of a commit whose parent is already reviewed 5/5 and whose own change is small and adversarially tested.

**Merge the reviewed parent `4cd1035` instead.** Rejected because it is knowingly the worse code. `4cd1035` carries a real defect that `16bf2d9` fixes: `urljoin` treats unparseable input as a relative path, so a malformed canonical such as `ht!tp://[[[/x` is grafted onto the fetched host and recorded as `http://example.com/ht!tp:/[[[/x` — a durable citation to a page that never existed. Merging the reviewed head would mean deliberately shipping a known false-provenance bug in the milestone whose entire purpose is exact provenance. Greptile did not find that defect; tracing the composed path did.

### Compensating safeguards

- The parent commit `4cd1035` was reviewed by Greptile at 5/5 with no outstanding findings.
- The change from that reviewed parent is narrowly scoped: rejecting malformed and fragment-only canonical metadata, and the composed tests and Law 0 source assertions that prove it.
- No implementation behaviour outside canonical provenance handling is altered by the unreviewed commit.
- The full suite passes: 1504 tests, 1788 subtests.
- `scripts/check_governance.py` passes.
- `scripts/check_architecture.py` passes.
- The `law-gates` CI check passes on the exact head.
- Four mutation checks prove the new guards are load-bearing: a hand-built authority without IPv6 brackets, a canonical inheriting the page scheme, a disallowed canonical port admitted, and the malformed-canonical guard removed.
- Every accepted canonical is asserted to survive `parse_public_url`, so a recorded citation is re-fetchable by construction.
- All ten of PR #14's review threads are resolved.

### Narrow scope

This exception authorises removing the `Greptile Review` context from the required status checks on `main` for long enough to merge PR #14 at `16bf2d9`, and nothing else. It does not authorise:

- altering `law-gates`, `enforce_admins`, `required_linear_history`, `required_conversation_resolution`, or any other protection setting;
- posting, forging or simulating any status check;
- merging any other pull request, or any other head of this pull request;
- skipping review on any future change, including further Web Access work.

### Expiry and mandatory review

This exception expires immediately once PR #14 is merged and `Greptile Review` is restored as a required status check on `main`. Restoration is part of the exception, not a follow-up task.

**A retrospective Greptile review of `16bf2d9` remains outstanding** and must be obtained once review credits are available. If that review finds anything, it is fixed as ordinary work under the restored gate. This exception is not precedent: the next head requires the check like any other, and exhausted credits are a reason to stop, not a standing reason to merge unreviewed.

---

## EX-003 — Merging `feat/web-search-v1` at `b1470fc` with an independent review that is not Greptile

### Register metadata

- **Law:** Law 0 enforcement via `docs/LAW_ENFORCEMENT.md` gate policy — "A change fails if any applicable automated gate fails" and "Disabling a gate is not a workaround". `main` requires the status checks `law-gates` and `Greptile Review`; this suspends the second one for one merge.
- **Scope:** The `feat/web-search-v1` branch only, at head `b1470fca49458c7cb7b5a779ff813c4eae0f4291`, comprising the ten commits listed below plus the single commit that adds this exception record. The `Greptile Review` required-status requirement on `main` is suspended for the duration of that one merge and restored immediately afterwards. Nothing else.
- **Necessity:** The account's Greptile review quota is exhausted, so the required `Greptile Review` status cannot be produced for this head by any legitimate means. An independent review was obtained from a different reviewer instead, so the substantive requirement — that someone other than the author examined this code — is met; only the named provider cannot be.
- **Alternatives:** wait for quota; merge nothing and continue on the branch; split the branch and merge only the parts already reviewed. All rejected below.
- **Risks and safeguards:** a required review provider replaced by one with no recorded track record in this repository, and a precedent risk that any unavailable gate may be substituted; guarded by the full branch diff having been reviewed, both law gates, the full suite, `law-gates` in CI, and the retrospective-review obligation below. Set out in full below.
- **Approved by Friedl:** yes, explicitly, for this exact wording and scope.
- **Approval date:** 2026-09-05.
- **Expiry or review condition:** expires immediately once the branch is merged and `Greptile Review` is restored as a required check. A retrospective Greptile review of `b1470fc` remains outstanding until quota allows it, alongside the one still outstanding for `16bf2d9` under EX-002.

### Authorised target

The implementation authorised here is the ten-commit branch `feat/web-search-v1`
at `b1470fc`:

| Commit | Subject |
| --- | --- |
| `0d5c07b` | Web Search V1: discovery through Brave, with its own spend ledger |
| `81796e5` | One voice: serialise audible playback in the browser |
| `98b4ef2` | Return the notebook to AL/X: research he asked for is not her Diary |
| `a0f41e6` | Never offer a vanished message as a new arrival |
| `e1897ca` | Scope the unheard-text guard to capabilities that actually send |
| `ac33fc0` | Carry a pre-goal refusal to the next reasoning step |
| `9f9d335` | Pay Cartesia only for audio around actual speech |
| `2b6f5fd` | Require 200 ms of voicing before opening a paid stream |
| `0d80c9f` | Run the voice-billing tests under the gate that enforces them |
| `b1470fc` | Hold the two D-025 search prices to one figure |

The commit actually merged is the one that adds this exception record on top of
`b1470fc` and changes nothing else, so the governance record lands on `main`
together with the merge it authorises rather than the merge arriving
unexplained.

Any commit other than these is outside this exception.

### Why this is necessary

`Greptile Review` is a required status check on `main`. It is produced by a paid
external service whose quota for this account is exhausted, so the check cannot
report on `b1470fc` at all. This is not a failing gate or a false positive; it
is a gate that cannot run.

This differs from EX-002 in the one respect that matters. There, the
compensating evidence was that the *parent* commit had been reviewed and the
unreviewed delta was small. Here, an independent review of the **entire** branch
diff was actually performed — by Augment (Auggie) rather than Greptile. The
requirement that this code be independently reviewed is therefore satisfied in
substance. What cannot be satisfied is the requirement that the review come from
one specific named provider.

Friedl commissioned that review and accepted its verdict. Its findings are
recorded below rather than summarised as an outcome, because a review's value is
in what it found, not in its verdict line.

### What the substitute review found

Augment reviewed the full branch diff (26 source files, 12 test files), ran both
law gates, and executed the suite. It raised **one material finding**:

> `tests/test_speech_transmission_gate.py` — the suite guarding the Cartesia
> billing defect — contained fifteen bare `def test_*` functions and no
> `unittest.TestCase` class. CI runs `python -m unittest discover -s tests`,
> which does not collect module-level functions, so none of the fifteen tests
> executed in CI.

That finding was verified independently and confirmed: `unittest discover`
collected zero of them, the file was the only suite in the repository written in
that style, and it also lacked the `sys.path` shim every other suite carries.
Verification additionally established a fact the review had not: `pytest` is
absent from `requirements.txt`, so CI could not have run those tests even had
collection worked. The suite was converted to the repository's convention in
`0d80c9f`, with no change to what any test asserts, and discovery went from
1691 tests to 1706.

Four further observations were raised and classified as non-blocking. One — that
`APPROVED_SEARCH_USD_PER_REQUEST` and `BRAVE_USD_PER_REQUEST` both state the
D-025 price with nothing asserting they agree — was judged worth acting on
because it guards spending, and became `b1470fc`. The remaining three are
recorded as outstanding housekeeping: a wrong return annotation on
`research/store.py::open_threads`, two pre-existing suites that rely on
discovery order for their import path, and unclosed sockets in the Brave fixture
server. None affects behaviour.

A review that found a real defect in the branch's own safety net is materially
better evidence than a review that found nothing.

### Alternatives considered and why they were rejected

**Wait for quota.** Correct in principle and rejected on cost. The branch carries
a fix for a live, measured billing defect — 7,272 seconds of audio billed against
zero spoken words in a single day. The fix is already what runs locally, so
delay does not expose Friedl to the defect again; what it does is leave the
protected branch as the one place that reviewed fix is absent, while the branch
carrying it diverges further from `main` with every subsequent change. The delay
would buy a second independent review of code that has already had one, at the
cost of a compounding merge.

**Merge nothing and continue on the branch.** Rejected because it compounds the
problem rather than deferring it. The branch is already ten commits and 39 files
ahead of `main`; further work would widen a diff that must eventually be
reviewed and merged as one piece, making the eventual review harder rather than
easier.

**Split the branch and merge only the already-reviewed parts.** Rejected because
no part of this branch has a Greptile review. PR #14's 5/5 review covered
`4cd1035`, which is already on `main`; everything here is above it. There is no
reviewed subset to extract, so the split would produce two unreviewed merges
instead of one.

### Compensating safeguards

- The full branch diff was independently reviewed, and its one material finding
  was fixed in `0d80c9f` before this exception was drafted.
- `scripts/check_governance.py` passes on the exact head.
- `scripts/check_architecture.py` passes on the exact head.
- The full suite passes: 1707 tests under `python -m unittest discover -s tests`,
  the exact command CI runs, with no `PYTHONPATH` set.
- The `law-gates` CI check is unaffected by this exception and must still pass on
  the merge commit. `webrtcvad-wheels` publishes cp312 manylinux and musllinux
  wheels, so the new dependency installs on CI's Python 3.12 Linux runner without
  a compiler.
- The only governed configuration file the branch touches is
  `architecture/boundaries.toml`, and its single change restricts a new
  third-party import to `providers`. It tightens the boundary rather than
  relaxing it.
- The economic boundaries the branch adds fail closed: search does not register
  at all without an approved price, positive ceilings and a working ledger, and
  the Cartesia gate transmits nothing when nobody is speaking.
- Mutation coverage was re-verified under the CI runner after the test-wiring
  fix, so the fifteen recovered tests are load-bearing rather than merely
  collected.

### Narrow scope

This exception authorises removing the `Greptile Review` context from the
required status checks on `main` for long enough to merge `feat/web-search-v1`
at `b1470fc`, and nothing else. It does not authorise:

- altering `law-gates`, `enforce_admins`, `required_linear_history`,
  `required_conversation_resolution`, `allow_force_pushes`, `allow_deletions`,
  or any other protection setting;
- posting, forging or simulating any status check;
- merging any other branch, or any other head of this branch;
- treating Augment, or any other reviewer, as a standing substitute for the
  required check on a future change;
- skipping review on any future change.

### Expiry and mandatory review

This exception expires immediately once the branch is merged and
`Greptile Review` is restored as a required status check on `main`. Restoration
is part of the exception, not a follow-up task.

**A retrospective Greptile review of `b1470fc` remains outstanding** and must be
obtained once review quota is available. The retrospective review of `16bf2d9`
owed under EX-002 remains outstanding as well; this exception does not discharge
it. If either review finds anything, it is fixed as ordinary work under the
restored gate.

This exception is not precedent. A second unavailable-quota exception in
consecutive merges is a signal that the review arrangement itself needs
attention, not that the requirement has become optional. The next head requires
the check like any other, and an exhausted quota remains a reason to stop rather
than a standing reason to merge.

---

## EX-004 — Merging PR #16 at `30b1dd9` to bootstrap provider-independent review

### Register metadata

- **Law:** Law 0 enforcement via `docs/LAW_ENFORCEMENT.md` gate policy — "A change fails if any applicable automated gate fails" and "Disabling a gate is not a workaround". `main` requires the status checks `law-gates` and `Greptile Review`; this suspends the second one for one merge.
- **Scope:** Pull request #16 only, at head `30b1dd9b38e7901662fcfe5bf114a863e1902dec`, plus the single commit that adds this exception record on top of it. The `Greptile Review` required-status requirement on `main` is suspended for the duration of that one squash merge and restored immediately afterwards. Nothing else.
- **Necessity:** Greptile's quota for this account is exhausted, so the required `Greptile Review` status cannot be produced for this head by any legitimate means. The change being merged is the mechanism that removes this dependency; it cannot satisfy a requirement it exists to replace, and it is not yet on `main` to enforce anything itself.
- **Alternatives:** wait for quota; merge without the exception; enable pull-request reviews so Friedl approves as CODEOWNER. All rejected below.
- **Risks and safeguards:** a third consecutive quota-driven exception, and a governance mechanism arriving without the review it will later require; guarded by a completed independent Augment review and its remediation, both law gates, the full suite, `law-gates` in CI, and the fact that the merged mechanism changes no enforcement. Set out in full below.
- **Approved by Friedl:** yes, explicitly, for this exact PR, this exact head and this exact mechanism.
- **Approval date:** 2026-09-05.
- **Expiry or review condition:** expires immediately once PR #16 is merged and `Greptile Review` is restored as a required check. The retrospective Greptile reviews owed under EX-002 and EX-003 remain outstanding and are not affected.

### Authorised target

The implementation authorised here is PR #16 at `30b1dd9`. The commit actually
merged is the one that adds this exception record on top of it and changes
nothing else, so the governance record lands on `main` together with the merge
it authorises rather than the merge arriving unexplained.

Any commit other than these two is outside this exception.

### Why this is necessary

`Greptile Review` is a required status check on `main`, produced by a paid
external service whose quota for this account is exhausted. It cannot report on
`30b1dd9` at all. This is not a failing gate or a false positive; it is a gate
that cannot run, for the third consecutive merge.

What distinguishes this from EX-002 and EX-003 is what is being merged. PR #16
is D-026: the mechanism that makes independent review a property rather than a
provider, so that an unavailable vendor stops being an unmergeable branch. It
cannot satisfy the provider-specific check it exists to replace, and it cannot
enforce the provider-independent requirement either, because it is not yet on
`main`. A mechanism intended to end a recurring exception cannot be installed
without one.

The change has had an independent review. Augment reviewed the full branch
diff, raised two material findings — that a dismissed GitHub review would still
have satisfied the verifier, and that the fail-closed behaviour under
`--enforce` was implemented but untested — and both were fixed in `30b1dd9`
before this exception was drafted. That review is not published to GitHub and
therefore cannot be machine-verified; it is recorded here as the human evidence
`docs/LAW_ENFORCEMENT.md` contemplates, not as a substitute for the check.

### Alternatives considered and why they were rejected

**Wait for quota.** Correct in principle and rejected on cost. Waiting to merge
the fix for a recurring blockage, because of that same blockage, prolongs
exactly the condition the change removes. The delay would buy a Greptile review
of a change whose purpose is to stop depending on Greptile's availability.

**Merge without an exception.** Refused. It would require either fabricating a
status check or an administrative override that suspends `law-gates`, linear
history and conversation resolution along with the one check that is actually
obstructed. Both are worse than the merge they would unblock.

**Enable required pull-request reviews and approve as CODEOWNER.** The cleanest
alternative, and genuinely arguable: it needs no exception and constitutes a
real review by the owner. Rejected for this merge because enabling
`required_pull_request_reviews` is a broader and more permanent change to
branch protection than suspending one check and restoring it, and this
exception is meant to be narrow and reversible. It remains available as a
deliberate decision later.

### Compensating safeguards

- The full branch diff was independently reviewed by Augment, and both material
  findings were fixed before this exception was drafted.
- The merged mechanism changes no enforcement. The verifier runs in reporting
  mode, cannot fail `law-gates`, and `Greptile Review` remains required.
- `scripts/check_governance.py` passes on the exact head.
- `scripts/check_architecture.py` passes on the exact head.
- The full suite passes: 1746 tests under `python -m unittest discover -s tests`.
- `law-gates` passed in CI on `30b1dd9`, including the new verifier, which
  reported honestly that no accepted review covers this head without blocking.
- Six mutation checks prove the verifier's guards are load-bearing.
- The review brief moved to `review/` byte-identically, verified by sha256, so
  the nine constitutional rules and six canonical context sources are unchanged.
- `LAWS_OF_ALX.md`, `docs/LAW_ENFORCEMENT.md` and the EX-002 and EX-003 records
  are untouched by the merged change.

### Narrow scope

This exception authorises removing the `Greptile Review` context from the
required status checks on `main` for long enough to merge PR #16 at `30b1dd9`,
and nothing else. It does not authorise:

- altering `law-gates`, `enforce_admins`, `required_linear_history`,
  `required_conversation_resolution`, `allow_force_pushes`, `allow_deletions`,
  or any other protection setting;
- posting, forging or simulating any status check;
- merging any other pull request, or any other head of this one;
- promoting the D-026 verifier to blocking, or removing `Greptile Review` from
  branch protection permanently. Both remain separate decisions;
- treating Augment, or any reviewer, as a standing substitute for a required
  check.

### Expiry and mandatory review

This exception expires immediately once PR #16 is merged and `Greptile Review`
is restored as a required status check on `main`. Restoration is part of the
exception, not a follow-up task.

**The retrospective Greptile reviews owed for `16bf2d9` under EX-002 and for
`b1470fc` under EX-003 remain outstanding, and remain specifically Greptile
reviews.** This exception neither discharges nor reinterprets them, and adds no
retrospective obligation of its own: the change it covers has been
independently reviewed, and what it lacks is the provider-specific status, not
the review.

This is the third consecutive merge blocked by exhausted quota. EX-003 recorded
that such a pattern is a reason to revisit the review arrangement deliberately
rather than to keep spending exceptions on it. D-026, which this merge
installs, is that revision. If a fourth such exception is ever needed, the
correct response is to complete the cutover — an accepted reviewer that can
publish GitHub-native evidence, and the verifier promoted to blocking — not to
approve another one.
