"""A classification run: relevance and segments for every family of a dataset (ADR 0011).

Everything is computed first, then written in **one transaction**: the run, the family texts,
new embeddings, every decision and an audit event. There are no partial runs. Model calls go
through the gateway cache, so repeating a run after a failure costs nothing for the calls
that already succeeded.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from patsquire_plr.classify.decide import (
    Band,
    RelevanceFinal,
    SegmentFinal,
    UsableJudgement,
    band,
    decide_relevance,
    decide_segment,
    usable,
)
from patsquire_plr.classify.judges import (
    RELEVANCE_PROMPT,
    SEGMENT_PROMPT,
    RelevanceJudgement,
    SegmentJudgement,
    evidence_problem,
    segment_lines,
    unknown_segments,
)
from patsquire_plr.classify.texts import FamilyText, family_texts, text_sha256
from patsquire_plr.classify.vectors import (
    Prototype,
    cosine,
    in_sample,
    relevance_prototypes,
    segment_prototype_text,
)
from patsquire_plr.config import ClassificationSettings, ModelRole
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import (
    ClassificationRun,
    PatentDocumentRow,
    QuerySet,
    RawRecord,
    RelevanceDecision,
    SegmentDecision,
    TextEmbedding,
)
from patsquire_plr.db.models import (
    FamilyText as FamilyTextRow,
)
from patsquire_plr.domain.patent import normalize_publication_number
from patsquire_plr.errors import PlrError
from patsquire_plr.gateway.errors import StructuredOutputError
from patsquire_plr.gateway.gateway import EmbeddingResult, StructuredResult
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.landscape.store import get_scope, get_taxonomy
from patsquire_plr.landscape.taxonomy import SegmentSpec


class ClassificationError(PlrError):
    """A classification run cannot be carried out."""


class Models(Protocol):
    """The parts of the model gateway a run needs (``ModelGateway`` satisfies it)."""

    def embed(self, texts: list[str], *, use_cache: bool = True) -> EmbeddingResult: ...

    def structured[T: BaseModel](
        self,
        role: ModelRole,
        prompt: PromptTemplate,
        variables: Mapping[str, str],
        output_model: type[T],
        *,
        use_cache: bool = True,
    ) -> StructuredResult[T]: ...


Progress = Callable[[str], None]
JUDGE_INVALID = "judge output invalid"


@dataclass
class _Relevance:
    family: FamilyText
    score: Decimal | None
    best_prototype: str | None
    band: Band
    judged: UsableJudgement | None
    judge_record: dict[str, object] | None
    final: RelevanceFinal
    reason: str


@dataclass
class _Segment:
    segment_id: str
    score: Decimal
    embedding_vote: bool
    judge_vote: bool | None
    judge_record: dict[str, object] | None
    final: SegmentFinal
    reason: str


@dataclass
class RunResult:
    run_id: uuid.UUID
    summary: dict[str, object] = field(default_factory=dict)


def classify(
    engine: Engine,
    models: Models,
    *,
    dataset_id: uuid.UUID,
    query_set_id: uuid.UUID,
    settings: ClassificationSettings,
    judge_model: str,
    progress: Progress = lambda _: None,
) -> RunResult:
    with Session(engine) as session:
        query_set = session.get(QuerySet, query_set_id)
    if query_set is None:
        raise ClassificationError(f"no query set {query_set_id}")
    version, taxonomy = get_taxonomy(engine, version_id=query_set.taxonomy_version_id)
    _, scope = get_scope(engine, version.landscape_id)
    segments = list(taxonomy.spec.segments)
    families = family_texts(engine, dataset_id)
    if not families:
        raise ClassificationError(f"dataset {dataset_id} has no families")
    known = _known_texts(engine, scope.known_relevant)
    prototypes = relevance_prototypes(scope, segments, known)
    segment_protos = [Prototype(f"segment:{s.id}", segment_prototype_text(s)) for s in segments]

    progress(f"embedding {len(families)} family texts and {len(prototypes)} prototypes")
    vectors, embedding_backend, embedding_model = _embed(
        models, [f.text for f in families if f.text] + [p.text for p in prototypes]
    )

    relevance = [
        _judge_relevance(
            models,
            f,
            vectors=vectors,
            prototypes=prototypes,
            topic=scope.topic,
            segments=segments,
            settings=settings,
        )
        for f in _progress(families, progress, "relevance")
    ]
    relevant = [r.family for r in relevance if r.final == "relevant"]
    segment_results = {
        f.family_key: _judge_segments(
            models,
            f,
            vectors=vectors,
            segment_protos=segment_protos,
            segments=segments,
            settings=settings,
        )
        for f in _progress(relevant, progress, "segments")
    }
    _check_judge_failures(relevance, segment_results, settings)
    summary = _summary(relevance, segment_results, known, scope.known_relevant)
    config: dict[str, object] = {
        "settings": settings.model_dump(mode="json"),
        "embedding": {"backend": embedding_backend, "model": embedding_model},
        "judge_model": judge_model,
        "prompts": {
            p.id: {"version": p.version, "sha256": p.sha256}
            for p in (RELEVANCE_PROMPT, SEGMENT_PROMPT)
        },
        "prototypes": {p.name: text_sha256(p.text) for p in prototypes},
        "taxonomy_content_sha256": version.content_sha256,
    }
    run_id = _store(
        engine,
        dataset_id=dataset_id,
        query_set_id=query_set_id,
        taxonomy_version_id=version.id,
        config=config,
        summary=summary,
        relevance=relevance,
        segments=segment_results,
        vectors=vectors,
        embedding_backend=embedding_backend,
        embedding_model=embedding_model,
    )
    return RunResult(run_id=run_id, summary=summary)


def _progress(items: Sequence[FamilyText], progress: Progress, stage: str) -> list[FamilyText]:
    progress(f"{stage}: {len(items)} families")
    return list(items)


def _known_texts(engine: Engine, publications: Sequence[str]) -> dict[str, str]:
    """Texts of the person-confirmed publications, from their newest stored retrieval."""
    texts: dict[str, str] = {}
    with Session(engine) as session:
        for publication in publications:
            number = normalize_publication_number(publication)
            row = session.execute(
                select(PatentDocumentRow.title, PatentDocumentRow.abstract)
                .join(RawRecord, RawRecord.id == PatentDocumentRow.raw_record_id)
                .where(
                    PatentDocumentRow.publication_country == number.country,
                    PatentDocumentRow.publication_number == number.number,
                )
                .order_by(RawRecord.retrieved_at.desc())
                .limit(1)
            ).first()
            if row is not None:
                parts = [p.strip() for p in row if p and p.strip()]
                if parts:
                    texts[publication] = "\n\n".join(parts)
    return texts


def _embed(models: Models, texts: list[str]) -> tuple[dict[str, tuple[float, ...]], str, str]:
    """(text -> vector, backend, model) as reported by the gateway for this call."""
    unique = list(dict.fromkeys(texts))
    result = models.embed(unique)
    if len(result.vectors) != len(unique):
        raise ClassificationError("the embedding model returned a different number of vectors")
    return dict(zip(unique, result.vectors, strict=True)), result.backend, result.model


def _judge_relevance(
    models: Models,
    family: FamilyText,
    *,
    vectors: dict[str, tuple[float, ...]],
    prototypes: Sequence[Prototype],
    topic: str,
    segments: Sequence[SegmentSpec],
    settings: ClassificationSettings,
) -> _Relevance:
    text = family.text
    if text is None:
        final, reason = decide_relevance("no_text", None, None)
        return _Relevance(family, None, None, "no_text", None, None, final, reason)
    scores = [(cosine(vectors[text], vectors[p.text]), p.name) for p in prototypes]
    score, best = max(scores, key=lambda s: (s[0], s[1]))
    family_band = band(
        score, high=settings.relevance.high_threshold, low=settings.relevance.low_threshold
    )
    judged, record = None, None
    sampled = in_sample(settings.seed, family.family_key, settings.relevance.qa_sample_rate)
    if family_band == "middle" or sampled:
        answer, problem, cache_key = _ask(
            models,
            RELEVANCE_PROMPT,
            {
                "topic": topic,
                "scope": "; ".join(s.name for s in segments),
                "segments": segment_lines(segments, detailed=False),
                "text": text,
            },
            RelevanceJudgement,
        )
        problem = problem or (evidence_problem(answer.evidence, text) if answer else None)
        judged = usable(answer, problem, settings.relevance.min_judge_confidence)
        record = {
            **(answer.model_dump() if answer else {}),
            "cache_key": cache_key,
            "problem": judged.problem,
            "sampled": family_band != "middle",
        }
    final, reason = decide_relevance(family_band, score, judged)
    return _Relevance(family, score, best, family_band, judged, record, final, reason)


def _judge_segments(
    models: Models,
    family: FamilyText,
    *,
    vectors: dict[str, tuple[float, ...]],
    segment_protos: Sequence[Prototype],
    segments: Sequence[SegmentSpec],
    settings: ClassificationSettings,
) -> list[_Segment]:
    text = family.text
    if text is None:  # pragma: no cover - only relevant families (which have text) arrive
        raise ClassificationError(f"family {family.family_key} has no text")
    answer, call_problem, cache_key = _ask(
        models,
        SEGMENT_PROMPT,
        {"segments": segment_lines(segments, detailed=True), "text": text},
        SegmentJudgement,
    )
    ids = {s.id for s in segments}
    valid: dict[str, str] = {}
    problems: list[str] = []
    if answer is not None:
        problems += [f"unknown segment {u}" for u in unknown_segments(answer, ids)]
        for assignment in answer.assignments:
            if assignment.segment_id not in ids:
                continue
            issue = evidence_problem(assignment.evidence, text)
            if issue:
                problems.append(f"{assignment.segment_id}: {issue}")
            else:
                valid[assignment.segment_id] = assignment.evidence
    results = []
    for proto, segment in zip(segment_protos, segments, strict=True):
        score = cosine(vectors[text], vectors[proto.text])
        embedding_vote = score >= settings.segments.embedding_threshold
        judge_vote = None if answer is None else segment.id in valid
        final, reason = decide_segment(embedding_vote, judge_vote, call_problem)
        record: dict[str, object] = {
            "cache_key": cache_key,
            "evidence": valid.get(segment.id),
            "problems": problems,
            "call_problem": call_problem,
        }
        results.append(
            _Segment(segment.id, score, embedding_vote, judge_vote, record, final, reason)
        )
    return results


def _ask[T: BaseModel](
    models: Models, prompt: PromptTemplate, variables: dict[str, str], output: type[T]
) -> tuple[T | None, str | None, str | None]:
    """(answer, problem, cache key). A reply that never matches the schema is recorded as
    a problem for this family; transport and configuration errors stop the run."""
    try:
        result = models.structured("bulk_classifier", prompt, variables, output)
    except StructuredOutputError as exc:
        return None, f"{JUDGE_INVALID}: {exc}", exc.cache_key
    return result.value, None, result.cache_key


def _check_judge_failures(
    relevance: Sequence[_Relevance],
    segments: dict[str, list[_Segment]],
    settings: ClassificationSettings,
) -> None:
    """A judge that fails for many families is broken (e.g. output cut off), not
    uncertain: the run stops instead of filling the review queue with its failures."""
    outcomes = [
        str((r.judge_record or {}).get("problem") or "").startswith(JUDGE_INVALID)
        for r in relevance
        if r.judge_record is not None
    ] + [
        bool(rows[0].judge_record and rows[0].judge_record.get("call_problem"))
        for rows in segments.values()
        if rows
    ]
    failed = sum(outcomes)
    if (
        outcomes
        and Decimal(failed) / Decimal(len(outcomes)) > settings.relevance.max_judge_failure_rate
    ):
        raise ClassificationError(
            f"the judge gave no valid output for {failed} of {len(outcomes)} calls, more than "
            f"the allowed {settings.relevance.max_judge_failure_rate}; check the model and "
            "its max_output_tokens (see llm_call for the attempts)"
        )


def _summary(
    relevance: Sequence[_Relevance],
    segments: dict[str, list[_Segment]],
    known: dict[str, str],
    known_relevant: Sequence[str],
) -> dict[str, object]:
    judged = [r for r in relevance if r.judged is not None]
    sampled = [r for r in judged if r.band in ("high", "low")]
    agreement = Counter(
        "no usable answer"
        if r.judged is None or r.judged.judgement is None
        else ("agree" if r.judged.judgement.relevant == (r.band == "high") else "disagree")
        for r in sampled
    )
    per_segment: dict[str, Counter[str]] = {}
    for rows in segments.values():
        for s in rows:
            per_segment.setdefault(s.segment_id, Counter())[s.final] += 1
    unclassified = sum(
        1 for rows in segments.values() if all(s.final == "not_assigned" for s in rows)
    )
    return {
        "families": len(relevance),
        "bands": dict(sorted(Counter(r.band for r in relevance).items())),
        "relevance": dict(sorted(Counter(r.final for r in relevance).items())),
        "judged": len(judged),
        "judge_unusable": sum(1 for r in judged if r.judged and r.judged.judgement is None),
        "qa_sample": dict(sorted(agreement.items())),
        "segments": {k: dict(sorted(v.items())) for k, v in sorted(per_segment.items())},
        "unclassified_relevant_families": unclassified,
        "review_queue": sum(1 for r in relevance if r.final == "uncertain")
        + sum(1 for rows in segments.values() for s in rows if s.final == "uncertain"),
        "known_prototypes": sorted(known),
        "known_without_text": sorted(set(known_relevant) - set(known)),
    }


def _store(
    engine: Engine,
    *,
    dataset_id: uuid.UUID,
    query_set_id: uuid.UUID,
    taxonomy_version_id: uuid.UUID,
    config: dict[str, object],
    summary: dict[str, object],
    relevance: Sequence[_Relevance],
    segments: dict[str, list[_Segment]],
    vectors: dict[str, tuple[float, ...]],
    embedding_backend: str,
    embedding_model: str,
) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        run = ClassificationRun(
            dataset_id=dataset_id,
            query_set_id=query_set_id,
            taxonomy_version_id=taxonomy_version_id,
            config=config,
            summary=summary,
        )
        session.add(run)
        session.flush()
        embedding_rows = [
            {
                "backend": embedding_backend,
                "model": embedding_model,
                "text_sha256": text_sha256(text),
                "dims": len(vector),
                "embedding": list(vector),
            }
            for text, vector in vectors.items()
        ]
        for start in range(0, len(embedding_rows), 1000):
            chunk = embedding_rows[start : start + 1000]
            session.execute(pg_insert(TextEmbedding).values(chunk).on_conflict_do_nothing())
        for r in relevance:
            f = r.family
            session.add(
                FamilyTextRow(
                    run_id=run.id,
                    family_key=f.family_key,
                    publication=f.publication,
                    language=f.language,
                    title=f.title,
                    abstract=f.abstract,
                    text_sha256=f.sha256,
                )
            )
            session.add(
                RelevanceDecision(
                    run_id=run.id,
                    family_key=f.family_key,
                    score=r.score,
                    band=r.band,
                    judged=r.judged is not None,
                    judge={**(r.judge_record or {}), "best_prototype": r.best_prototype},
                    final=r.final,
                    reason=r.reason,
                )
            )
        for family_key, rows in segments.items():
            session.add_all(
                SegmentDecision(
                    run_id=run.id,
                    family_key=family_key,
                    segment_id=s.segment_id,
                    score=s.score,
                    embedding_vote=s.embedding_vote,
                    judge_vote=s.judge_vote,
                    judge=s.judge_record,
                    final=s.final,
                    reason=s.reason,
                )
                for s in rows
            )
        append_event(
            session,
            step="classify",
            event_type="classification_run",
            actor="system",
            payload={"run_id": str(run.id), "dataset_id": str(dataset_id), **summary},
        )
        return run.id
