"""Cognitive-difficulty tiers for bounded questions.

A tier says how hard the thinking is, not what the subject is. "Jellyfish" is
not cheap and "engineering" is not expensive; a question about either can be a
lookup or a genuinely difficult judgement. Routing on topic would be Law 1
phrase routing wearing a different name, so the tier is a value AL/X sets when
she composes the question, never a classifier reading her words.

A tier buys computational resources. It does not decide what AL/X investigates,
what she concludes, or what any answer means.
"""

from __future__ import annotations

from enum import Enum


class Cognition(str, Enum):
    """How much reasoning capability one bounded question is worth."""

    # Reading, extraction, transcription, summarising what a source says.
    SURVEY = "survey"
    # Comparison, synthesis, reconciling sources, moderate ambiguity.
    COMPARE = "compare"
    # Genuinely difficult reasoning where a cheaper model would be wrong.
    JUDGE = "judge"


class CognitionOrigin(str, Enum):
    """Where a cognition turn came from. Never what it is about.

    An origin says that something new exists and where it came from. It carries
    no subject, importance, urgency or suggestion, and nothing may be derived
    from it about what AL/X should think. Deciding whether an occasion is worth
    pursuing is a judgement, and only the Core makes it.

    This is deliberately a closed set of four facts about provenance. It is not
    a category of work, a priority, or a routing key for capabilities, goals or
    prompts: those would each be a rule about what deserves attention, which is
    Law 1 phrase routing wearing a different surface.
    """

    # Friedl typed or spoke.
    PERSON_TURN = "person_turn"
    # An observer reported a fact, such as mail arriving.
    EXTERNAL_EVENT = "external_event"
    # A capability result became available, or a goal's state changed.
    WORK_COMPLETED = "work_completed"
    # A future cognition AL/X asked for herself came due.
    SELF_REQUESTED = "self_requested"

    @property
    def is_autonomous(self) -> bool:
        """Whether this turn began without a person.

        The one distinction the runtime draws, and it is about provenance
        rather than content: an autonomous turn is one nobody asked for.
        """
        return self is not CognitionOrigin.PERSON_TURN
