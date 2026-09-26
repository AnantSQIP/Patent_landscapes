"""Measured accuracy of a classification run against the gold set (verification Layer 4).

**Relevance.**
* Positive means the run's final decision is ``relevant``.
* ``uncertain`` and ``no_text`` count as not found. They are not in the landscape until a
  person decides, so they lower recall rather than being assumed right.

**Segments.** Measured on gold families labelled for that segment. Positive means
``assigned``.

Every rate comes with a Wilson score interval at 95%. All arithmetic is ``Decimal``, and
results are rounded half-even to 4 places.

**A run is publishable only when all of these hold:**
* there are at least ``min_gold_labels`` relevance labels;
* precision and recall meet their minimums;
* every segment with gold positives meets the minimum F1;
* no family in the run is still ``uncertain`` without a person's label.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.config import EvaluationSettings
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import (
    ClassificationRun,
    Evaluation,
    GoldLabel,
    RelevanceDecision,
    SegmentDecision,
)
from patsquire_plr.errors import PlrError

Z_95 = Decimal("1.959963984540054")  # standard normal quantile for a 95% interval
FOUR = Decimal("0.0001")


@dataclass(frozen=True)
class Counts:
    tp: int
    fp: int
    fn: int
    tn: int

    def rates(self) -> dict[str, object]:
        precision = _ratio(self.tp, self.tp + self.fp)
        recall = _ratio(self.tp, self.tp + self.fn)
        f1 = _ratio(2 * self.tp, 2 * self.tp + self.fp + self.fn)  # from counts, unrounded
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": _s(precision),
            "precision_ci95": _ci(self.tp, self.tp + self.fp),
            "recall": _s(recall),
            "recall_ci95": _ci(self.tp, self.tp + self.fn),
            "f1": _s(f1),
        }


def _q(value: Decimal) -> Decimal:
    return value.quantize(FOUR, rounding=ROUND_HALF_EVEN)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    return None if denominator == 0 else _q(Decimal(numerator) / Decimal(denominator))


def wilson(successes: int, trials: int) -> tuple[Decimal, Decimal] | None:
    """95% Wilson score interval for a proportion; None when there are no trials."""
    if trials == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        n, p, z2 = Decimal(trials), Decimal(successes) / Decimal(trials), Z_95 * Z_95
        centre = p + z2 / (2 * n)
        margin = Z_95 * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt()
        denominator = 1 + z2 / n
        low, high = (centre - margin) / denominator, (centre + margin) / denominator
    return _q(max(low, Decimal(0))), _q(min(high, Decimal(1)))


def _ci(successes: int, trials: int) -> list[str] | None:
    interval = wilson(successes, trials)
    return None if interval is None else [str(interval[0]), str(interval[1])]


def _counts(pairs: list[tuple[bool, bool]]) -> Counts:
    """(gold, predicted) pairs."""
    return Counts(
        tp=sum(g and p for g, p in pairs),
        fp=sum(not g and p for g, p in pairs),
        fn=sum(g and not p for g, p in pairs),
        tn=sum(not g and not p for g, p in pairs),
    )


def evaluate(engine: Engine, run_id: uuid.UUID, settings: EvaluationSettings) -> Evaluation:
    with Session(engine) as session:
        run = session.get(ClassificationRun, run_id)
        if run is None:
            raise PlrError(f"no classification run {run_id}")
        gold = _gold(session, run.dataset_id)
        decisions = dict(
            session.execute(
                select(RelevanceDecision.family_key, RelevanceDecision.final).where(
                    RelevanceDecision.run_id == run_id
                )
            ).all()
        )
        assigned = {
            (family, segment)
            for family, segment in session.execute(
                select(SegmentDecision.family_key, SegmentDecision.segment_id).where(
                    SegmentDecision.run_id == run_id, SegmentDecision.final == "assigned"
                )
            )
        }
        segment_uncertain = {
            family
            for (family,) in session.execute(
                select(SegmentDecision.family_key).where(
                    SegmentDecision.run_id == run_id, SegmentDecision.final == "uncertain"
                )
            )
        }
    relevance_pairs = [
        (label, decisions[family] == "relevant")
        for (family, task), label in gold.items()
        if task == "relevance" and family in decisions
    ]
    results: dict[str, object] = {
        "relevance": {"labelled": len(relevance_pairs), **_counts(relevance_pairs).rates()}
    }
    segment_tasks = sorted({task for _, task in gold if task.startswith("segment:")})
    for task in segment_tasks:
        segment = task.removeprefix("segment:")
        pairs = [
            (label, (family, segment) in assigned)
            for (family, t), label in gold.items()
            if t == task and family in decisions
        ]
        results[task] = {"labelled": len(pairs), **_counts(pairs).rates()}
    segment_labelled = {family for family, task in gold if task.startswith("segment:")}
    unresolved = sorted(
        {
            f
            for f, final in decisions.items()
            if final == "uncertain" and (f, "relevance") not in gold
        }
        | (segment_uncertain - segment_labelled)
    )
    problems = _problems(results, settings, unresolved)
    with Session(engine, expire_on_commit=False) as session, session.begin():
        row = Evaluation(
            run_id=run_id,
            results={**results, "unresolved_review_items": len(unresolved)},
            publishable=not problems,
            problems=list(problems),
        )
        session.add(row)
        session.flush()
        append_event(
            session,
            step="classify",
            event_type="classification_evaluated",
            actor="system",
            payload={"run_id": str(run_id), "publishable": not problems, "problems": problems},
        )
    return row


def _gold(session: Session, dataset_id: uuid.UUID) -> dict[tuple[str, str], bool]:
    """The newest label per (family, task)."""
    rows = session.execute(
        select(GoldLabel.family_key, GoldLabel.task, GoldLabel.label)
        .where(GoldLabel.dataset_id == dataset_id)
        .order_by(GoldLabel.created_at, GoldLabel.id)
    ).all()
    return {(family, task): label for family, task, label in rows}


def _problems(
    results: dict[str, object], settings: EvaluationSettings, unresolved: list[str]
) -> list[str]:
    problems = []
    relevance = results["relevance"]
    assert isinstance(relevance, dict)  # noqa: S101 - built above
    if relevance["labelled"] < settings.min_gold_labels:
        problems.append(
            f"only {relevance['labelled']} relevance labels; at least "
            f"{settings.min_gold_labels} are needed"
        )
    for name, minimum in (("precision", settings.min_precision), ("recall", settings.min_recall)):
        value = relevance[name]
        if value is None or Decimal(str(value)) < minimum:
            problems.append(f"relevance {name} {value} is below {minimum}")
    for task, measured in results.items():
        if not task.startswith("segment:") or not isinstance(measured, dict):
            continue
        if measured["tp"] + measured["fn"] == 0:
            continue  # no gold positives: F1 is not defined for this segment yet
        f1 = measured["f1"]
        if f1 is None or Decimal(str(f1)) < settings.min_segment_f1:
            problems.append(f"{task} F1 {f1} is below {settings.min_segment_f1}")
    if unresolved:
        problems.append(f"{len(unresolved)} families still need a person's review")
    return problems
