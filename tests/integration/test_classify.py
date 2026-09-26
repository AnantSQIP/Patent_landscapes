"""A full classification run, labels and evaluation on real Postgres + MinIO, from the
recorded Google pages. Models are TEST-ONLY scripted stand-ins with fixed answers."""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from patsquire_plr.classification.store import load_cpc_title_list, open_cpc_scheme
from patsquire_plr.classify.evaluate import evaluate
from patsquire_plr.classify.gold import LabelError, export_sample, import_labels
from patsquire_plr.classify.judges import RelevanceJudgement, SegmentJudgement
from patsquire_plr.classify.run import ClassificationError, classify
from patsquire_plr.classify.texts import family_texts
from patsquire_plr.cli import app
from patsquire_plr.config import (
    ClassificationSettings,
    EvaluationSettings,
    ModelRole,
    ObjectStorageSettings,
)
from patsquire_plr.db.datasets import build_dataset
from patsquire_plr.db.models import (
    FamilyText,
    RelevanceDecision,
    SegmentDecision,
    TextEmbedding,
)
from patsquire_plr.gateway.gateway import EmbeddingResult, StructuredResult
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.runner import run_lookup_batch, start_lookup_batch
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.search import create_query_set
from patsquire_plr.landscape.store import add_taxonomy_version, create_landscape, record_approval
from tests.integration.test_ingestion import _pages, _serve, _source
from tests.integration.test_landscape import _cli_config, _content
from tests.unit.test_cpc import title_list_zip

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALIASES = REPO_ROOT / "config" / "applicant_aliases.yaml"
NUMBERS = ["US10000000B2", "US20160266243A1", "US5093563A", "EP3123456A1", "WO2020123456A1"]

SETTINGS = ClassificationSettings.model_validate(
    {
        "seed": 7,
        "relevance": {
            "high_threshold": "0.5000",
            "low_threshold": "0.3500",
            "qa_sample_rate": "1",  # every confident family is also judged
            "min_judge_confidence": "medium",
        },
        "segments": {"embedding_threshold": "0.4500"},
        "evaluation": {
            "min_gold_labels": 3,
            "min_precision": "0.8000",
            "min_recall": "0.8000",
            "min_segment_f1": "0.7000",
        },
    }
)


class ScriptedModels:
    """TEST-ONLY stand-in for the gateway. Embeddings follow the text:
    "ladar" -> [1, 0], "optical imaging" -> [0.4, 0.9165] (borderline), else [0, 1].
    The relevance judge says relevant for "ladar" and for the price-label patent (a
    disagreement), and quotes invented evidence for the light-module patent."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, texts: list[str], *, use_cache: bool = True) -> EmbeddingResult:
        vectors = tuple(self._vector(t.lower()) for t in texts)
        return EmbeddingResult(
            vectors=vectors, dimensions=2, cached_count=0, backend="t", model="t"
        )

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        if "ladar" in text:
            return (1.0, 0.0)
        if "optical imaging" in text:
            return (0.4, 0.9165)
        return (0.0, 1.0)

    def structured[T: BaseModel](
        self,
        role: ModelRole,
        prompt: PromptTemplate,
        variables: Mapping[str, str],
        output_model: type[T],
        *,
        use_cache: bool = True,
    ) -> StructuredResult[T]:
        prompt.render(variables)
        self.calls.append(prompt.id)
        text = variables["text"]
        lowered = text.lower()
        if output_model is RelevanceJudgement:
            evidence = "an invented quote" if "light module" in lowered else text[:20]
            value: BaseModel = RelevanceJudgement(
                relevant="ladar" in lowered or "price label" in lowered,
                confidence="high",
                evidence=evidence,
                reason="scripted",
            )
        else:
            value = SegmentJudgement.model_validate(
                {
                    "assignments": [
                        {"segment_id": "ladar", "evidence": text[:20]},
                        {"segment_id": "ghost", "evidence": text[:20]},
                    ]
                }
            )
        assert isinstance(value, output_model)
        return StructuredResult(value=value, cached=False, backend="t", model="t", cache_key="k")


def _prepared(engine: Engine, store: ObjectStorageSettings) -> tuple[uuid.UUID, uuid.UUID]:
    """(dataset, approved query set) over the recorded pages."""
    source = _source(_serve(_pages()))
    batch = start_lookup_batch(engine, source, NUMBERS)
    assert run_lookup_batch(engine, RawStore(store), source, batch).status == "complete"
    dataset = build_dataset(engine, name="t", batch_ids=[batch], alias_file=ALIASES)
    row, _ = load_cpc_title_list(engine, RawStore(store), title_list_zip(), source_url="t")
    _, scheme = open_cpc_scheme(engine, RawStore(store))
    scope = Scope(
        topic="Coherent ladar",
        notes=None,
        date_from=date(1990, 1, 1),
        date_to=date(2024, 12, 31),
        countries=(),
        cpc_hints=(),
        seeds=(),
        known_relevant=("US10000000B2",),
        confirmed_by="Test Reviewer",
    )
    landscape = create_landscape(engine, name="t", scope=scope)
    version = add_taxonomy_version(
        engine,
        landscape_id=landscape,
        content=_content(scheme),
        origin="user_edit",
        scheme_id=row.id,
        cache_keys=[],
        parent_id=None,
        actor="t",
    )
    record_approval(
        engine,
        subject_type="taxonomy_version",
        subject_id=version.id,
        decision="approved",
        mode="human",
        decided_by="Test Reviewer",
        note=None,
    )
    query_set = create_query_set(engine, taxonomy_version_id=version.id, scope=scope, scheme=scheme)
    return dataset, query_set


def _run(engine: Engine, dataset: uuid.UUID, query_set: uuid.UUID) -> uuid.UUID:
    return classify(
        engine,
        ScriptedModels(),
        dataset_id=dataset,
        query_set_id=query_set,
        settings=SETTINGS,
        embedding_model="test-embedding",
        judge_model="test-judge",
    ).run_id


def test_a_run_decides_every_family_with_reasons(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    dataset, query_set = _prepared(db_engine, object_storage)
    run_id = _run(db_engine, dataset, query_set)

    with Session(db_engine) as session:
        texts = {t.family_key: t for t in session.scalars(select(FamilyText))}
        decisions = {
            texts[d.family_key].publication: d for d in session.scalars(select(RelevanceDecision))
        }
        segments = session.scalars(select(SegmentDecision)).all()
        embeddings = session.scalars(select(TextEmbedding)).all()

    # The ladar family is judged on its earliest publication with an abstract.
    assert "US20160266243A1" in decisions
    assert "US10000000B2" not in decisions
    finals = {p: (d.band, d.final) for p, d in decisions.items()}
    assert finals == {
        "US20160266243A1": ("high", "relevant"),  # the sampled judge agrees
        "US5093563A": ("middle", "not_relevant"),  # borderline: the judge decides
        "EP3123456A1": ("low", "uncertain"),  # the sampled judge disagrees
        "WO2020123456A1": ("low", "not_relevant"),  # judge's evidence invented: band stands
    }
    wo = decisions["WO2020123456A1"]
    assert wo.judge is not None
    assert "evidence is not in the patent text" in str(wo.judge["problem"])
    assert "sampled judge not usable" in wo.reason
    assert all(d.reason for d in decisions.values())

    [segment] = segments  # one relevant family, one segment
    assert (segment.segment_id, segment.final, segment.score) == (
        "ladar",
        "assigned",
        Decimal("1.0000"),
    )
    assert segment.judge is not None
    assert segment.judge["problems"] == ["unknown segment ghost"]
    assert {e.model for e in embeddings} == {"test-embedding"}

    rerun = _run(db_engine, dataset, query_set)  # embeddings are reused, not duplicated
    assert rerun != run_id
    with Session(db_engine) as session:
        assert len(session.scalars(select(TextEmbedding)).all()) == len(embeddings)


def test_family_texts_pick_the_representative_deterministically(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    dataset, _ = _prepared(db_engine, object_storage)
    texts = family_texts(db_engine, dataset)
    assert [t.family_key for t in texts] == sorted(t.family_key for t in texts)
    assert family_texts(db_engine, dataset) == texts  # the same choice every time
    assert len(texts) == 4
    assert all(t.text for t in texts)


def test_labels_and_evaluation(
    db_engine: Engine, object_storage: ObjectStorageSettings, tmp_path: Path
) -> None:
    dataset, query_set = _prepared(db_engine, object_storage)
    run_id = _run(db_engine, dataset, query_set)

    exported = export_sample(db_engine, run_id, size=10, seed=1)
    assert exported.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(exported.removeprefix("﻿"))))
    assert len(rows) == 4
    assert {"relevant", "segments"} <= set(rows[0])

    with Session(db_engine) as session:
        texts = {t.publication: t.family_key for t in session.scalars(select(FamilyText))}
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "family_key,relevant,segments\n"
        f"{texts['US20160266243A1']},y,ladar\n"
        f"{texts['US5093563A']},n,\n"
        f"{texts['EP3123456A1']},n,\n"
        f"{texts['WO2020123456A1']},n,\n",
        encoding="utf-8",
    )
    result = import_labels(db_engine, run_id, labels, labeller="Test Reviewer")
    assert (result.families_labelled, result.labels_stored) == (4, 5)
    assert export_sample(db_engine, run_id, size=10, seed=1).count("\n") == 1  # all labelled

    row = evaluate(db_engine, run_id, SETTINGS.evaluation)
    relevance = row.results["relevance"]
    assert isinstance(relevance, dict)
    assert (relevance["tp"], relevance["fp"], relevance["fn"], relevance["tn"]) == (1, 0, 0, 3)
    assert row.publishable is True
    assert row.problems == []

    strict = EvaluationSettings.model_validate(
        {**SETTINGS.evaluation.model_dump(), "min_gold_labels": 100}
    )
    assert evaluate(db_engine, run_id, strict).problems == [
        "only 4 relevance labels; at least 100 are needed"
    ]
    bad = tmp_path / "bad.csv"
    bad.write_text("family_key,relevant,segments\nnope,y,\n", encoding="utf-8")
    with pytest.raises(LabelError, match="not in this run"):
        import_labels(db_engine, run_id, bad, labeller="T")


def test_unreviewed_uncertain_families_block_publication(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    dataset, query_set = _prepared(db_engine, object_storage)
    run_id = _run(db_engine, dataset, query_set)
    row = evaluate(db_engine, run_id, SETTINGS.evaluation)
    assert row.publishable is False
    assert "1 families still need a person's review" in row.problems


def test_a_missing_query_set_is_an_error(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    dataset, _ = _prepared(db_engine, object_storage)
    with pytest.raises(ClassificationError, match="no query set"):
        _run(db_engine, dataset, uuid.uuid4())


def test_review_and_label_commands(
    db_engine: Engine,
    object_storage: ObjectStorageSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, query_set = _prepared(db_engine, object_storage)
    run_id = str(_run(db_engine, dataset, query_set))
    config = _cli_config(db_engine, object_storage, tmp_path, monkeypatch)

    def plr(*args: str, code: int = 0) -> str:
        result = CliRunner().invoke(app, [*args, "--config", str(config)])
        assert result.exit_code == code, result.output
        return result.stdout

    assert json.loads(plr("classify", "report", run_id))["summary"]["families"] == 4
    review = plr("classify", "review", run_id)
    assert review.startswith("relevance\t")
    assert "sampled judge disagrees" in review
    sample = tmp_path / "sample.csv"
    plr("label", "export", run_id, "--out", str(sample), "--size", "2")
    assert sample.read_text(encoding="utf-8").count("\n") == 3
    evaluated = json.loads(plr("classify", "evaluate", run_id, code=1))
    assert evaluated["publishable"] is False
