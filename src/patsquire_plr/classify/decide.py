"""Decision rules for relevance and segments (ADR 0011). Pure functions: the same inputs
always give the same decision and reason.

**Relevance.** The embedding score puts a family in a band:
* ``high`` (score >= high threshold): relevant;
* ``low`` (score <= low threshold): not relevant;
* ``middle``: borderline, always sent to the judge.

A fixed sample of high and low families also goes to the judge, as a quality check.

A judge answer counts only when its evidence is in the text and its confidence meets the
minimum. Then:
* in the middle band the judge decides; with no usable answer the family is ``uncertain``;
* in a confident band, agreement confirms the band, and a usable disagreement makes the
  family ``uncertain`` (two methods disagree, so a person decides).

**Segments** (relevant families only, multi-label). The embedding votes yes when the
similarity to the segment's prototype meets the threshold; the judge votes yes when it
assigns the segment with valid evidence.
* Both yes: ``assigned``.
* Both no: ``not_assigned``.
* They differ: ``uncertain``.
* No usable judge answer (the call failed): ``uncertain`` for every segment. One method
  alone can neither assign a segment nor rule it out, so the family goes to review.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from patsquire_plr.classify.judges import RelevanceJudgement, confident_enough

Band = Literal["high", "middle", "low", "no_text"]
RelevanceFinal = Literal["relevant", "not_relevant", "uncertain", "no_text"]
SegmentFinal = Literal["assigned", "not_assigned", "uncertain"]


def band(score: Decimal | None, *, high: Decimal, low: Decimal) -> Band:
    if score is None:
        return "no_text"
    if score >= high:
        return "high"
    if score <= low:
        return "low"
    return "middle"


@dataclass(frozen=True)
class UsableJudgement:
    """A judge answer that passed the checks, or why it did not."""

    judgement: RelevanceJudgement | None
    problem: str | None


def usable(
    judgement: RelevanceJudgement | None, evidence_problem: str | None, minimum: str
) -> UsableJudgement:
    if judgement is None:
        return UsableJudgement(None, evidence_problem or "no judge answer")
    if evidence_problem is not None:
        return UsableJudgement(None, evidence_problem)
    if not confident_enough(judgement.confidence, minimum):
        return UsableJudgement(None, f"judge confidence {judgement.confidence} below {minimum}")
    return UsableJudgement(judgement, None)


def decide_relevance(
    family_band: Band, score: Decimal | None, judged: UsableJudgement | None
) -> tuple[RelevanceFinal, str]:
    if family_band == "no_text":
        return "no_text", "no title or abstract to judge"
    if family_band == "middle":
        return _decide_borderline(score, judged)
    embedding_says = family_band == "high"
    expected: RelevanceFinal = "relevant" if embedding_says else "not_relevant"
    side = "at or above the high" if embedding_says else "at or below the low"
    basis = f"embedding score {score} is {side} threshold"
    if judged is None:
        return expected, basis
    if judged.judgement is None:
        return expected, f"{basis}; sampled judge not usable ({judged.problem})"
    if judged.judgement.relevant == embedding_says:
        return expected, f"{basis}; the sampled judge agrees"
    return "uncertain", f"{basis} but the sampled judge disagrees: {judged.judgement.reason}"


def _decide_borderline(
    score: Decimal | None, judged: UsableJudgement | None
) -> tuple[RelevanceFinal, str]:
    basis = f"embedding score {score} is borderline"
    if judged is None or judged.judgement is None:
        why = judged.problem if judged else "not judged"
        return "uncertain", f"{basis} and the judge gave no usable answer ({why})"
    verdict: RelevanceFinal = "relevant" if judged.judgement.relevant else "not_relevant"
    return verdict, f"{basis}; judge: {judged.judgement.reason}"


def decide_segment(
    embedding_vote: bool, judge_vote: bool | None, judge_problem: str | None
) -> tuple[SegmentFinal, str]:
    if judge_vote is None:
        side = "yes" if embedding_vote else "no"
        why = judge_problem or "no judge answer"
        return "uncertain", f"embedding says {side}; judge not usable ({why})"
    if embedding_vote and judge_vote:
        return "assigned", "embedding and judge agree"
    if not embedding_vote and not judge_vote:
        return "not_assigned", "embedding and judge agree it does not belong"
    who = "embedding" if embedding_vote else "judge"
    return "uncertain", f"only the {who} says it belongs"
