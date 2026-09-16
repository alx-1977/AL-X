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

from typing import Any

from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity


# What an occasion raised from an observation is called. The observation keeps
# its own identity; this names the occasion to think about it, so the gateway's
# event merge cannot have one silently replace the other.
OCCASION_PREFIX = "mail-occasion:"


def mail_conversation_id(person_id: str) -> str:
    """The durable thread AL/X's thinking about the mailbox continues in.

    Derived rather than generated, so it is the same thread after a restart.
    A fresh identifier each time would give every observed message its own
    private history, and she would meet the mailbox for the first time on every
    occasion.

    Scoped by person because relationship context is, under
    `IDENTITY_AND_MEMORY.md`: one person's mail must not accumulate in a thread
    another person's turns can reach.
    """
    if not person_id.strip():
        raise ValueError("person_id must not be blank")
    return f"mail:{person_id.strip()}"


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
        conversation_id: str,
        enabled: bool = False,
    ) -> None:
        if not conversation_id.strip():
            raise ValueError("conversation_id must not be blank")
        self._source = source
        self._ledger = ledger
        # The thread mail belongs to. Unlike a matured request, an observation
        # has no originating conversation to return to: nobody asked for it, so
        # there is no turn it arose in. One durable thread is named for it, so
        # her thinking about the mailbox accumulates in one history rather than
        # scattering across whichever browser session happened to be open.
        self._conversation_id = conversation_id
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
                    conversation_id=self._conversation_id,
                    # What the observation is, in the mailbox's own terms.
                    # Never the subject, the sender or the body: AL/X reads
                    # the message herself, through the capabilities that
                    # already exist.
                    references=(f"mail_observation:{event.event_id}",),
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
            reference.startswith("mail_observation:")
            for reference in opportunity.references
        )

    def claim(self, opportunity: CognitionOpportunity) -> bool:
        """Take an occasion exactly once, before anything is spent on it."""
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
            if not reference.startswith("mail_observation:"):
                continue
            self._source.record_delivery(reference[len("mail_observation:"):])
