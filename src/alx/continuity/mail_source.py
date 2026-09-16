"""Turn an observed mail fact into a cognition occasion.

The third sibling of the matured-request and completed-work sources, and
deliberately as thin. One notices a time has passed, one notices a task has
finished, this one notices the mailbox changed. All three produce an occasion
and none of them decides anything about it.

It does not read the subject, rank the sender, judge whether a message matters,
or skip an observation because of what it might be about. It does not know what
an invoice is. Anything of that kind would be a rule about what deserves
thought, which is the second mind arriving without anyone deciding to build one.

Existing production ingress is reused rather than extended: the occasion goes to
the same runner, through the same claim, the same ledger and the same lock as
every other origin. There is no second path to a Core turn, and mail no longer
needs a transport to be connected before AL/X can think about it.

Two facts are reported, because the mailbox has two kinds of news. A message
arrived, and a message she was told about has gone. They are separate occasions
with separate identities, because settling one says nothing about the other.
"""

from __future__ import annotations

import logging
from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity

LOGGER = logging.getLogger(__name__)

# How an occasion names the observation it was raised from. The reference is
# what ties the two together for settling and for claiming, so it is stated
# once here rather than spelled out at each site.
OBSERVATION_PREFIX = "mail_observation:"


# What an occasion raised from an observation is called. The observation keeps
# its own identity; this names the occasion to think about it, so the gateway's
# event merge cannot have one silently replace the other.
OCCASION_PREFIX = "mail-occasion:"


def mail_conversation_id(event: Any) -> str:
    """The durable thread one observed message's cognition continues in.

    Derived from RFC 5322 identifier headers, which are the only confirmed
    threading evidence a message carries. The root of the chain names the
    thread: `References` first, because its first entry is the original
    message of the conversation; then `In-Reply-To` for a reply whose client
    sent no chain; and finally the message's own `Message-ID`, which is correct
    for a message that starts one.

    This replaced a single mailbox-wide thread keyed on the person. That put
    every correspondent, every subject and every unfinished goal in one
    history: AL/X reasoning about one supplier's quote could see, and continue,
    a goal belonging to an unrelated conversation. A thread boundary is not a
    presentation detail — it is what keeps one piece of work from silently
    becoming evidence in another.

    Never inferred from subject similarity. Two unrelated messages can share a
    subject, a reply can change one, and deciding that two subjects "mean" the
    same conversation is a semantic judgement that would belong to AL/X rather
    than to this function. Identifier headers are mechanical: either the
    message names its parent or it does not.

    A message carrying no usable identifier at all falls back to its own
    durable observation identity, so it gets its own thread rather than joining
    somebody else's. That is the safe direction: a thread too narrow costs
    continuity, a thread too wide leaks one conversation into another.
    """
    data = getattr(event, "data", None) or {}
    references = data.get("references") or ()
    if isinstance(references, str):
        references = (references,)
    for candidate in (*references, data.get("in_reply_to"), data.get("message_id")):
        if isinstance(candidate, str) and candidate.strip():
            return f"mail-thread:{candidate.strip()}"
    # No identifier headers. The observation's own identity is stable across
    # restart and unique to this message, so it becomes a thread of one.
    return f"mail-thread:{getattr(event, 'event_id', '')}".rstrip(":") or "mail-thread"


class MailCognitionSource:
    """The one place an observed mail fact becomes an occasion.

    Idempotent by construction, like its siblings: an occasion's identity comes
    from the durable observation, the ledger refuses a repeat, and the
    observation is settled once the turn has run. A restart therefore replays
    nothing, which matters because a replayed occasion is a second paid Core
    call for one message.
    """

    # An arrival and a disappearance of the same message are different facts,
    # so they are different occasions. The suffix that keeps them apart is the
    # observation store's own, carried through rather than reinvented.
    _VANISHED_SUFFIX = ":vanished"

    def __init__(
        self,
        source: Any,
        ledger: Any,
        enabled: bool = False,
    ) -> None:
        self._source = source
        self._ledger = ledger
        # Off by default, for the same reason its siblings are: a runtime never
        # told it may think unprompted does not, and observed mail simply waits
        # to be looked at.
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def opportunity_id_for(event_id: str) -> str:
        """One observation becomes one occasion, permanently.

        Derived from the observation's own event identity, so
        `mail:<uid_validity>:<uid>` and `mail:<uid_validity>:<uid>:vanished`
        each map to exactly one occasion and a restart replays neither.

        Prefixed rather than reused verbatim. The gateway merges the occasion
        and the turn's contextual events into one set keyed by event id, so an
        occasion carrying the observation's own id replaced the observation:
        the synthesised `cognition.opportunity` event won, and the message body
        never reached the Core. They are two different facts -- one is the
        message, one is the occasion to think about it -- and they need two
        identities to both survive that merge.
        """
        return f"{OCCASION_PREFIX}{event_id}"

    def due_opportunities(self) -> tuple[CognitionOpportunity, ...]:
        """Every observed mail fact the Core has not yet been given.

        Disappearances first, then arrivals, each in the order the mailbox
        reports them. That ordering is mechanical and nothing more: a
        disappearance settles something she may already have raised, so it is
        offered before the next new thing. It says nothing about importance.

        Returns nothing when the master switch is off, and in that case touches
        no observation.
        """
        if not self._enabled:
            return ()
        opportunities: list[CognitionOpportunity] = []
        for event in (
            *self._source.pending_vanished(),
            *self._source.unclaimed_arrivals(),
        ):
            opportunity_id = self.opportunity_id_for(event.event_id)
            if self._ledger.exists(opportunity_id):
                continue
            opportunities.append(
                CognitionOpportunity(
                    opportunity_id=opportunity_id,
                    origin=CognitionOrigin.EXTERNAL_EVENT,
                    arose_at=event.occurred_at,
                    # The thread this message belongs to, from its own
                    # identifier headers. Unrelated correspondence therefore
                    # accumulates in unrelated histories, and a reply continues
                    # the one it replies to.
                    conversation_id=mail_conversation_id(event),
                    # What the observation is, in the mailbox's own terms.
                    # Never the subject, the sender or the body: AL/X reads
                    # the message herself, through the capabilities that
                    # already exist.
                    references=(f"{OBSERVATION_PREFIX}{event.event_id}",),
                    provenance=event.provenance,
                )
            )
        return tuple(opportunities)

    def recover(self, spend: Any = None) -> tuple[str, ...]:
        """Reclaim observations that a stopped process left claimed.

        Without this, a run that stopped between claiming an occasion and
        settling the observation left a durable claim behind: the observation
        stayed unsettled, and every later scan skipped it because the ledger row
        existed. The message would have been observed, claimed, and then never
        looked at.

        The rule is its siblings', for its siblings' reason. A spend row is
        marked dispatched before a provider is called, so an occasion with no
        dispatched reservation cannot have reached one and is safely offered
        again. One that did reach dispatch may already have been billed and
        answered, so it is retained rather than replayed.

        Only this producer's rows are touched. The ledger is shared, and
        reclaiming another producer's occasion would release a claim whose
        idempotence is kept somewhere else entirely.
        """
        reclaimed: list[str] = []
        for row in self._ledger.unfinished():
            opportunity_id = row["opportunity_id"]
            if not opportunity_id.startswith(OCCASION_PREFIX):
                continue
            if spend is not None and spend.dispatch_started(opportunity_id):
                # Provable dispatch. Retain for inspection; never replay.
                self._ledger.mark_unreconciled(opportunity_id)
                continue
            # No dispatch is recorded, so the provider was never reached.
            self._ledger.release(opportunity_id)
            reclaimed.append(opportunity_id)
        return tuple(reclaimed)

    def owns(self, opportunity: CognitionOpportunity) -> bool:
        """Whether this producer made the occasion."""
        return any(
            reference.startswith(OBSERVATION_PREFIX)
            for reference in opportunity.references
        )

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        """Take an occasion exactly once, before anything is spent on it.

        Taking it also records that the observation behind it is owed an
        answer. Reconciliation settles an observation nobody has been shown
        without reporting its disappearance, and a poll cycle between this
        claim and the turn used to do exactly that: the turn ran with no mail
        event in context, reasoning about a synthetic occasion for a message it
        could not see, and the disappearance was lost. Marking it here closes
        that window, because the mark is written before anything is spent and
        reconciliation reads the same durable fact.

        The mark is best effort in one direction only. If it cannot be written
        the claim is refused rather than taken, because a claimed occasion whose
        observation may vanish silently is the state this exists to prevent.
        """
        for reference in opportunity.references:
            if not reference.startswith(OBSERVATION_PREFIX):
                continue
            try:
                self._source.mark_claimed(reference[len(OBSERVATION_PREFIX):])
            except Exception:  # noqa: BLE001 - an unrecordable claim is refused
                LOGGER.warning(
                    "Refusing %s: its observation could not be marked claimed",
                    opportunity.opportunity_id,
                )
                return False
        return self._ledger.record_created(opportunity)

    def release(self, opportunity: CognitionOpportunity) -> None:
        """Return an occasion that produced nothing, so it can arise again."""
        self._ledger.release(opportunity.opportunity_id)

    def mark_honoured(self, opportunity: CognitionOpportunity) -> None:
        """Settle the observation behind an occasion that has been acted on.

        `record_delivery` reports whether it moved anything. False means the
        observation was reconciled or released while the turn was running,
        which is the benign race it has always tolerated rather than raised:
        the turn happened either way, and the ledger row is what stops the
        occasion arising again.
        """
        for reference in opportunity.references:
            if not reference.startswith(OBSERVATION_PREFIX):
                continue
            self._source.record_delivery(reference[len(OBSERVATION_PREFIX):])
