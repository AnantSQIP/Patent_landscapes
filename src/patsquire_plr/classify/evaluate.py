"""Measured accuracy of a classification run against people's labels (verification Layer 4).

**Which labels measure accuracy.** Only labels from ``sample`` exports: seeded uniform random
samples, which represent the whole run. Labels from ``review`` exports resolve uncertain
decisions but are never part of the figures, because they are the hardest cases.

**Several labellers.** Each labeller's newest label per family and task counts. Where
labellers disagree, the item is left out of the figures and reported, and the run is not
publishable until they agree.

**What counts as found.**
* Relevance: positive means the final decision is ``relevant``. ``uncertain`` and
  ``no_text`` count as not found.
* Segments: positive means ``assigned``.

**Figures.** Precision, recall and F1 (from counts), each rate with a Wilson 95% interval.
Arithmetic is ``Decimal``; reported values are rounded half-even to 4 places.

**Publishable** only when all of these hold:
* at least ``min_gold_labels`` sampled relevance labels, of which at least
  ``min_gold_positives`` are relevant;
* precision and recall meet their minimums, compared unrounded against the 95% lower bound
  (``gate_on: lower_bound``) or the estimate (``point``);
* at least ``min_segment_labels`` sampled relevant families have segment labels, and every
  segment with any error or hit meets the minimum F1;
* no labellers disagree;
* no uncertain decision is left without a person's label.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
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
    LabelExport,
    RelevanceDecision,
    SegmentDecision,
)
from patsquire_plr.errors import PlrError

Z_95 = Decimal("1.959963984540054")  # standard normal quantile for a 95% interval
FOUR = Decimal("0.0001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(FOUR, rounding=ROUND_HALF_EVEN)


def _wilson_raw(successes: int, trials: int) -> tuple[Decimal, Decimal] | None:
    if trials == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        n, p, z2 = Decimal(trials), Decimal(successes) / Decimal(trials), Z_95 * Z_95
        centre = p + z2 / (2 * n)
        margin = Z_95 * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt()
        denominator = 1 + z2 / n
        low, high = (centre - margin) / denominator, (centre + margin) / denominator
    return max(low, Decimal(0)), min(high, Decimal(1))


def wilson(successes: int, trials: int) -> tuple[Decimal, Decimal] | None:
    """95% Wilson score interval for a proportion, rounded; None when there are no trials."""
    raw = _wilson_raw(successes, trials)
    return None if raw is None else (_q(raw[0]), _q(raw[1]))


@dataclass(frozen=True)
class Counts:
    tp: int
    fp: int
    fn: int
    tn: int

    def rates(self) -> dict[str, object]:
        def ratio(n: int, d: int) -> str | None:
            return None if d == 0 else str(_q(Decimal(n) / Decimal(d)))

        def interval(n: int, d: int) -> list[str] | None:
            bounds = wilson(n, d)
            return None if bounds is None else [str(bounds[0]), str(bounds[1])]

        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": ratio(self.tp, self.tp + self.fp),
            "precision_ci95": interval(self.tp, self.tp + self.fp),
            "recall": ratio(self.tp, self.tp + self.fn),
            "recall_ci95": interval(self.tp, self.tp + self.fn),
            "f1": ratio(2 * self.tp, 2 * self.tp + self.fp + self.fn),
        }

    def meets(self, kind: str, minimum: Decimal, gate_on: str) -> bool:
        """Whether precision or recall meets ``minimum``, compared exactly (unrounded)."""
        hits = self.tp
        trials = self.tp + (self.fp if kind == "precision" else self.fn)
        if trials == 0:
            return False
        if gate_on == "point":
            return Decimal(hits) >= minimum * Decimal(trials)
        bounds = _wilson_raw(hits, trials)
        return bounds is not None and bounds[0] >= minimum


def _counts(pairs: list[tuple[bool, bool]]) -> Counts:
    """(gold, predicted) pairs."""
    return Counts(
        tp=sum(g and p for g, p in pairs),
        fp=sum(not g and p for g, p in pairs),
        fn=sum(g and not p for g, p in pairs),
        tn=sum(not g and not p for g, p in pairs),
    )


@dataclass(frozen=True)
class _Gold:
    sample: dict[tuple[str, str], bool]  # agreed labels from random samples
    any_label: set[tuple[str, str]]  # every (family, task) someone labelled, any purpose
    conflicts: list[tuple[str, str]]  # labellers disagree


def _gold(session: Session, dataset_id: uuid.UUID) -> _Gold:
    rows = session.execute(
        select(
            GoldLabel.family_key,
            GoldLabel.task,
            GoldLabel.label,
            GoldLabel.labeller,
            LabelExport.purpose,
        )
        .join(LabelExport, LabelExport.id == GoldLabel.export_id)
        .where(GoldLabel.dataset_id == dataset_id)
        .order_by(GoldLabel.created_at, GoldLabel.id)
    ).all()
    newest: dict[tuple[str, str, str], tuple[bool, str]] = {}
    for family, task, label, labeller, purpose in rows:
        newest[(family, task, labeller)] = (label, purpose)
    by_item: dict[tuple[str, str], list[tuple[bool, str]]] = defaultdict(list)
    for (family, task, _), value in newest.items():
        by_item[(family, task)].append(value)
    sample: dict[tuple[str, str], bool] = {}
    conflicts = []
    for item, values in by_item.items():
        if len({label for label, _ in values}) > 1:
            conflicts.append(item)
        elif any(purpose == "sample" for _, purpose in values):
            sample[item] = values[0][0]
    return _Gold(sample=sample, any_label=set(by_item), conflicts=sorted(conflicts))


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
        segment_rows = session.execute(
            select(
                SegmentDecision.family_key, SegmentDecision.segment_id, SegmentDecision.final
            ).where(SegmentDecision.run_id == run_id)
        ).all()
    assigned = {(f, s) for f, s, final in segment_rows if final == "assigned"}
    segment_uncertain = {f for f, _, final in segment_rows if final == "uncertain"}
    relevance = _counts(
        [
            (label, decisions[family] == "relevant")
            for (family, task), label in gold.sample.items()
            if task == "relevance" and family in decisions
        ]
    )
    results: dict[str, object] = {"relevance": relevance.rates()}
    segment_counts: dict[str, Counts] = {}
    for task in sorted({t for _, t in gold.sample if t.startswith("segment:")}):
        segment = task.removeprefix("segment:")
        segment_counts[task] = _counts(
            [
                (label, (family, segment) in assigned)
                for (family, t), label in gold.sample.items()
                if t == task and family in decisions
            ]
        )
        results[task] = segment_counts[task].rates()
    segment_families = {
        f
        for (f, t) in gold.sample
        if t.startswith("segment:") and gold.sample.get((f, "relevance"))
    }
    labelled_relevance = {f for (f, t) in gold.any_label if t == "relevance"}
    labelled_segments = {f for (f, t) in gold.any_label if t.startswith("segment:")}
    unresolved = sorted(
        {
            f
            for f, final in decisions.items()
            if final == "uncertain" and f not in labelled_relevance
        }
        | (segment_uncertain - labelled_segments)
    )
    problems = _problems(
        relevance,
        segments=segment_counts,
        segment_families=len(segment_families),
        settings=settings,
        unresolved=unresolved,
        conflicts=gold.conflicts,
    )
    results["sample_segment_families"] = len(segment_families)
    results["unresolved_review_items"] = len(unresolved)
    results["labeller_conflicts"] = [list(c) for c in gold.conflicts]
    with Session(engine, expire_on_commit=False) as session, session.begin():
        row = Evaluation(
            run_id=run_id, results=results, publishable=not problems, problems=problems
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


def _problems(
    relevance: Counts,
    *,
    segments: dict[str, Counts],
    segment_families: int,
    settings: EvaluationSettings,
    unresolved: list[str],
    conflicts: list[tuple[str, str]],
) -> list[str]:
    problems = []
    labelled = relevance.tp + relevance.fp + relevance.fn + relevance.tn
    positives = relevance.tp + relevance.fn
    if labelled < settings.min_gold_labels:
        problems.append(
            f"only {labelled} sampled relevance labels; at least {settings.min_gold_labels} "
            "are needed"
        )
    if positives < settings.min_gold_positives:
        problems.append(
            f"only {positives} sampled families labelled relevant; at least "
            f"{settings.min_gold_positives} are needed"
        )
    bound = "95% lower bound" if settings.gate_on == "lower_bound" else "estimate"
    for kind, minimum in (("precision", settings.min_precision), ("recall", settings.min_recall)):
        if not relevance.meets(kind, minimum, settings.gate_on):
            problems.append(f"relevance {kind} ({bound}) is below {minimum}")
    if segment_families < settings.min_segment_labels:
        problems.append(
            f"only {segment_families} sampled relevant families have segment labels; at least "
            f"{settings.min_segment_labels} are needed"
        )
    for task, counts in segments.items():
        if counts.tp + counts.fp + counts.fn == 0:
            continue  # nothing assigned and nothing missed: no evidence either way
        f1 = Decimal(2 * counts.tp) / Decimal(2 * counts.tp + counts.fp + counts.fn)
        if f1 < settings.min_segment_f1:
            problems.append(f"{task} F1 {_q(f1)} is below {settings.min_segment_f1}")
    if conflicts:
        problems.append(f"{len(conflicts)} labels differ between labellers; reconcile them")
    if unresolved:
        problems.append(f"{len(unresolved)} families still need a person's review")
    return problems
