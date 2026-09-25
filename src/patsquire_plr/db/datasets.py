"""Build, store and report datasets (immutable analysis snapshots, Phase 4)."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.clean.dataset import COPY_RULE, InputDocument, plan_dataset
from patsquire_plr.clean.names import RULES_VERSION, load_aliases, similar_keys
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.documents import load_document
from patsquire_plr.db.models import (
    Dataset,
    DatasetApplicant,
    DatasetConflict,
    DatasetDocument,
    DatasetFamily,
    DatasetFamilyMember,
    IngestBatch,
    PatentDocumentRow,
    RawRecord,
)
from patsquire_plr.errors import PlrError


class DatasetError(PlrError):
    """A dataset cannot be built or found."""


def build_dataset(
    engine: Engine, *, name: str, batch_ids: list[uuid.UUID], alias_file: Path
) -> uuid.UUID:
    """Plan the dataset from every document of the given complete batches and store it."""
    if not batch_ids:
        raise DatasetError("a dataset needs at least one ingest batch")
    aliases = load_aliases(alias_file)
    with Session(engine) as session:
        batches = session.scalars(select(IngestBatch).where(IngestBatch.id.in_(batch_ids))).all()
        if missing := sorted(set(map(str, batch_ids)) - {str(b.id) for b in batches}):
            raise DatasetError(f"unknown ingest batches: {missing}")
        if incomplete := sorted(str(b.id) for b in batches if b.status != "complete"):
            raise DatasetError(f"batches are not complete (resume them first): {incomplete}")
        rows = session.execute(
            select(PatentDocumentRow.id, RawRecord.retrieved_at)
            .join(RawRecord, RawRecord.id == PatentDocumentRow.raw_record_id)
            .where(RawRecord.batch_id.in_(batch_ids))
            .order_by(PatentDocumentRow.id)
        ).all()
        inputs = [
            InputDocument(
                document_id=doc_id, retrieved_at=retrieved, document=load_document(session, doc_id)
            )
            for doc_id, retrieved in rows
        ]
    if not inputs:
        raise DatasetError("the batches contain no documents")
    plan = plan_dataset(inputs, aliases)
    config: dict[str, object] = {
        "copy_rule": COPY_RULE,
        "family_definition": "simple_family",
        "family_evidence": ["stated_member", "family_id", "application"],
        "name_rules_version": RULES_VERSION,
        "alias_file": str(alias_file),
        "alias_file_version": aliases.version,
        "alias_file_sha256": hashlib.sha256(alias_file.read_bytes()).hexdigest(),
    }
    with Session(engine) as session, session.begin():
        dataset = Dataset(
            name=name,
            batch_ids=sorted(str(b) for b in batch_ids),
            config=config,
            input_documents=len(inputs),
            publications=len(plan.selected),
            families=len(plan.families),
        )
        session.add(dataset)
        session.flush()
        session.add_all(
            DatasetDocument(
                dataset_id=dataset.id,
                document_id=d.document_id,
                publication=d.publication,
                decision=d.decision,
                reason=d.reason,
                detail=d.detail,
            )
            for d in plan.decisions
        )
        session.add_all(
            DatasetConflict(
                dataset_id=dataset.id,
                publication=c.publication,
                field=c.field,
                values=dict(c.values),
            )
            for c in plan.conflicts
        )
        session.add_all(
            DatasetFamily(
                dataset_id=dataset.id,
                family_key=f.key,
                evidence=list(f.evidence),
                publications_in_dataset=f.publications_in_dataset,
                stated_members_not_retrieved=f.stated_members_not_retrieved,
            )
            for f in plan.families
        )
        session.add_all(
            DatasetFamilyMember(
                dataset_id=dataset.id,
                publication=m.publication,
                family_key=m.family_key,
                in_dataset=m.in_dataset,
            )
            for m in plan.members
        )
        session.add_all(
            DatasetApplicant(
                dataset_id=dataset.id,
                publication=a.publication,
                sequence=a.sequence,
                name_raw=a.name_raw,
                name_key=a.name_key,
                entity_name=a.entity_name,
                rule_steps=list(a.rule_steps),
                alias_reason=a.alias_reason,
            )
            for a in plan.applicants
        )
        session.flush()
        append_event(
            session,
            step="clean",
            event_type="dataset_built",
            actor="system",
            payload={
                "dataset_id": str(dataset.id),
                "name": name,
                "input_documents": len(inputs),
                "publications": len(plan.selected),
                "families": len(plan.families),
                "excluded": sum(d.decision == "excluded" for d in plan.decisions),
                "conflicts": len(plan.conflicts),
            },
        )
        return dataset.id


class NameMerge(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity: str
    key: str
    spellings: list[str]
    rules: list[str]


class DatasetReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_id: uuid.UUID
    name: str
    input_documents: int
    selected: int
    excluded: dict[str, int]
    publications: int
    families: int
    family_sizes: dict[str, int]
    conflicts: list[dict[str, object]]
    name_merges: list[NameMerge]
    review_candidates: list[dict[str, object]]
    config: dict[str, object]


def report_dataset(engine: Engine, dataset_id: uuid.UUID) -> DatasetReport:
    """The normalisation report for human review (build prompt Phase 4 acceptance)."""
    with Session(engine) as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetError(f"dataset {dataset_id} does not exist")
        decisions = session.execute(
            select(DatasetDocument.decision, DatasetDocument.reason).where(
                DatasetDocument.dataset_id == dataset_id
            )
        ).all()
        families = session.scalars(
            select(DatasetFamily).where(DatasetFamily.dataset_id == dataset_id)
        ).all()
        conflicts = session.scalars(
            select(DatasetConflict).where(DatasetConflict.dataset_id == dataset_id)
        ).all()
        applicants = session.scalars(
            select(DatasetApplicant).where(DatasetApplicant.dataset_id == dataset_id)
        ).all()

    spellings: dict[str, set[str]] = defaultdict(set)
    entity: dict[str, str] = {}
    steps: dict[str, set[str]] = defaultdict(set)
    for a in applicants:
        spellings[a.name_key].add(a.name_raw)
        entity[a.name_key] = a.entity_name
        steps[a.name_key].update(str(s) for s in a.rule_steps)
    merges = [
        NameMerge(entity=entity[k], key=k, spellings=sorted(v), rules=sorted(steps[k]))
        for k, v in sorted(spellings.items())
        if len(v) > 1
    ]
    candidates = [
        {
            "key_a": a,
            "key_b": b,
            "similarity": ratio,
            "action": "review: add an alias group only if one entity",
        }
        for a, b, ratio in similar_keys(spellings)
    ]
    size_counts = Counter(
        str(f.publications_in_dataset + f.stated_members_not_retrieved) for f in families
    )
    return DatasetReport(
        dataset_id=dataset_id,
        name=dataset.name,
        input_documents=dataset.input_documents,
        selected=sum(d == "selected" for d, _ in decisions),
        excluded=dict(Counter(str(r) for d, r in decisions if d == "excluded")),
        publications=dataset.publications,
        families=dataset.families,
        family_sizes=dict(sorted(size_counts.items(), key=lambda kv: int(kv[0]))),
        conflicts=[
            {"publication": c.publication, "field": c.field, "values": c.values} for c in conflicts
        ],
        name_merges=merges,
        review_candidates=candidates,
        config=dataset.config,
    )


def render_report_markdown(report: DatasetReport) -> str:
    """The report as Markdown, for an analyst to review name merges and conflicts."""
    lines = [
        f"# Dataset report: {report.name}",
        "",
        f"Dataset `{report.dataset_id}`",
        "",
        "## Conservation",
        "",
        f"* Input documents: {report.input_documents}",
        f"* Selected (one per publication): {report.selected}",
        f"* Excluded: {sum(report.excluded.values())} "
        + (
            f"({', '.join(f'{k}: {v}' for k, v in sorted(report.excluded.items()))})"
            if report.excluded
            else ""
        ),
        f"* Publications: {report.publications} in {report.families} families",
        "* Family sizes (publications incl. stated members not retrieved): "
        + ", ".join(f"{size}: {count}" for size, count in report.family_sizes.items()),
        "",
        "## Applicant name merges (formatting rules and alias groups)",
        "",
    ]
    if report.name_merges:
        lines += ["| Entity | Spellings merged | Rules applied |", "|---|---|---|"]
        lines += [
            f"| {m.entity} | {'; '.join(m.spellings)} | {', '.join(m.rules)} |"
            for m in report.name_merges
        ]
    else:
        lines.append("No two different spellings were merged.")
    lines += ["", "## Look-alike names to review (not merged)", ""]
    if report.review_candidates:
        lines += ["| Name A | Name B | Similarity |", "|---|---|---|"]
        lines += [
            f"| {c['key_a']} | {c['key_b']} | {c['similarity']} |" for c in report.review_candidates
        ]
        lines += ["", "Merge a pair only by adding an alias group with a reason to the alias file."]
    else:
        lines.append("None found.")
    lines += ["", "## Conflicts between copies of the same publication", ""]
    if report.conflicts:
        lines += [
            f"* {c['publication']}: field `{c['field']}` differs between copies"
            for c in report.conflicts
        ]
    else:
        lines.append("None.")
    lines += ["", "## Configuration", "", "```json", json.dumps(report.config, indent=2), "```", ""]
    return "\n".join(lines)
