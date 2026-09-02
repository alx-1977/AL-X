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
