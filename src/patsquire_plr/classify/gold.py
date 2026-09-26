"""Labelling by people, through CSV files that open in Excel (ADR 0011).

**Two kinds of export, both recorded in ``label_export``:**
* ``sample``: a uniform random sample of the run's families, fixed by a seed. Families
  already labelled in an earlier sample are skipped. Only these labels measure accuracy,
  because only they represent the whole population.
* ``review``: the review queue (families with an uncertain decision). These labels
  resolve the uncertain decisions but never enter the accuracy figures, since they are the
  hardest cases, not a random sample.

Neither file shows the system's decisions, so the labels are not biased by them.

**Import.** The file keeps its ``export_id`` column, and every family must be in that
export. Per row:
* ``relevant``: y/n (blank skips the row);
* ``segments``: segment ids separated by ";", or ``none``. Only for relevant families;
  blank records no segment labels.

Segment labels:
* for a relevant family with segments given, every segment not listed is labelled "no";
* a family labelled not relevant gets "no" for every segment, so older segment labels
  never outlive a correction.

Files saved by Excel in other locales (";" or tab as separator) are read. A file that is
not UTF-8 is rejected with the reason. A file with any invalid row is rejected whole.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import (
    ClassificationRun,
    FamilyText,
    GoldLabel,
    LabelExport,
    RelevanceDecision,
    SegmentDecision,
)
from patsquire_plr.errors import PlrError
from patsquire_plr.landscape.store import get_taxonomy

COLUMNS = (
    "export_id",
    "family_key",
    "publication",
    "title",
    "abstract",
    "relevant",
    "segments",
    "notes",
)
REQUIRED = {"export_id", "family_key", "relevant", "segments"}
YES, NO = {"y", "yes", "1", "true"}, {"n", "no", "0", "false"}
Purpose = Literal["sample", "review"]


class LabelError(PlrError):
    """A label file is invalid; nothing from it is stored."""


def _run(session: Session, run_id: uuid.UUID) -> ClassificationRun:
    run = session.get(ClassificationRun, run_id)
    if run is None:
        raise LabelError(f"no classification run {run_id}")
    return run


def export_sample(engine: Engine, run_id: uuid.UUID, *, size: int, seed: int) -> str:
    """CSV text with ``size`` families not yet labelled in any sample, recorded as an export."""
    with Session(engine) as session:
        run = _run(session, run_id)
        sampled = set(
            session.scalars(
                select(GoldLabel.family_key)
                .join(LabelExport, LabelExport.id == GoldLabel.export_id)
                .where(
                    GoldLabel.dataset_id == run.dataset_id,
                    GoldLabel.task == "relevance",
                    LabelExport.purpose == "sample",
                )
            )
        )
        texts = session.scalars(select(FamilyText).where(FamilyText.run_id == run_id)).all()
    pool = [t for t in texts if t.family_key not in sampled and (t.title or t.abstract)]
    pool.sort(key=lambda t: hashlib.sha256(f"{seed}:{t.family_key}".encode()).hexdigest())
    return _write_export(engine, run_id, "sample", seed, pool[:size])


def export_review(engine: Engine, run_id: uuid.UUID) -> str:
    """CSV text with every family that has an uncertain decision, recorded as an export."""
    with Session(engine) as session:
        _run(session, run_id)
        uncertain = set(
            session.scalars(
                select(RelevanceDecision.family_key).where(
                    RelevanceDecision.run_id == run_id, RelevanceDecision.final == "uncertain"
                )
            )
        ) | set(
            session.scalars(
                select(SegmentDecision.family_key).where(
                    SegmentDecision.run_id == run_id, SegmentDecision.final == "uncertain"
                )
            )
        )
        texts = session.scalars(
            select(FamilyText)
            .where(FamilyText.run_id == run_id, FamilyText.family_key.in_(uncertain))
            .order_by(FamilyText.family_key)
        ).all()
    if not texts:
        raise LabelError(f"run {run_id} has nothing to review")
    return _write_export(engine, run_id, "review", None, list(texts))


def _write_export(
    engine: Engine, run_id: uuid.UUID, purpose: Purpose, seed: int | None, texts: list[FamilyText]
) -> str:
    with Session(engine) as session, session.begin():
        export = LabelExport(
            run_id=run_id, purpose=purpose, seed=seed, family_keys=[t.family_key for t in texts]
        )
        session.add(export)
        session.flush()
        export_id = str(export.id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for t in texts:
        writer.writerow(
            (export_id, t.family_key, t.publication, t.title or "", t.abstract or "", "", "", "")
        )
    return "﻿" + buffer.getvalue()


@dataclass(frozen=True)
class ImportResult:
    export_id: str
    purpose: str
    rows_read: int
    families_labelled: int
    labels_stored: int
    skipped_blank: int


def import_labels(engine: Engine, run_id: uuid.UUID, path: Path, *, labeller: str) -> ImportResult:
    if not labeller.strip():
        raise LabelError("labels must name who labelled them")
    content = path.read_bytes()
    rows = _read_rows(content)
    export_ids = {(row.get("export_id") or "").strip() for row in rows}
    if len(export_ids) != 1:
        raise LabelError(
            f"the file must come from one export; found export ids {sorted(export_ids)}"
        )
    [export_text] = export_ids
    try:
        export_id = uuid.UUID(export_text)
    except ValueError as exc:
        raise LabelError(f"export_id {export_text!r} is not valid") from exc
    with Session(engine) as session:
        run = _run(session, run_id)
        export = session.get(LabelExport, export_id)
        if export is None or export.run_id != run_id:
            raise LabelError(f"export {export_id} does not belong to run {run_id}")
        families = {str(f) for f in export.family_keys}
        purpose, dataset_id = export.purpose, run.dataset_id
    _, taxonomy = get_taxonomy(engine, version_id=run.taxonomy_version_id)
    segment_ids = [s.id for s in taxonomy.spec.segments]
    labels, blank = _parse(rows, families, segment_ids)
    digest = hashlib.sha256(content).hexdigest()
    with Session(engine) as session, session.begin():
        session.add_all(
            GoldLabel(
                dataset_id=dataset_id,
                export_id=export_id,
                family_key=family,
                task=task,
                label=value,
                labeller=labeller.strip(),
                source_sha256=digest,
            )
            for family, task, value in labels
        )
        append_event(
            session,
            step="classify",
            event_type="labels_imported",
            actor=labeller.strip(),
            payload={
                "run_id": str(run_id),
                "export_id": str(export_id),
                "purpose": purpose,
                "labels": len(labels),
                "file_sha256": digest,
            },
        )
    return ImportResult(
        export_id=str(export_id),
        purpose=purpose,
        rows_read=len(rows),
        families_labelled=len({f for f, _, _ in labels}),
        labels_stored=len(labels),
        skipped_blank=blank,
    )


def _read_rows(content: bytes) -> list[dict[str, str]]:
    """Rows of a CSV saved by Excel: UTF-8 (with or without BOM), separated by ",", ";" or
    tab. Header names are trimmed."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LabelError(
            "the file is not UTF-8. In Excel choose Save As > 'CSV UTF-8 (Comma delimited)'"
        ) from exc
    first_line = text.split("\n", 1)[0]
    delimiter = max(",;\t", key=first_line.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration as exc:
        raise LabelError("the file is empty") from exc
    if missing := REQUIRED - set(header):
        raise LabelError(f"the file lacks columns {sorted(missing)} (found {header})")
    return [dict(zip(header, row, strict=False)) for row in reader if any(c.strip() for c in row)]


def _parse(
    rows: list[dict[str, str]], families: set[str], segment_ids: list[str]
) -> tuple[list[tuple[str, str, bool]], int]:
    labels: list[tuple[str, str, bool]] = []
    problems: list[str] = []
    blank = 0
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        family = (row.get("family_key") or "").strip()
        relevant = (row.get("relevant") or "").strip().casefold()
        segments = (row.get("segments") or "").strip()
        if not relevant:
            blank += 1
            if segments:
                problems.append(f"line {line}: segments given but 'relevant' is blank")
            continue
        if family not in families:
            problems.append(f"line {line}: family {family!r} is not in this export")
            continue
        if family in seen:
            problems.append(f"line {line}: family {family} appears twice")
            continue
        seen.add(family)
        if relevant not in YES | NO:
            problems.append(f"line {line}: relevant must be y or n, not {relevant!r}")
            continue
        if relevant in NO:
            if segments and segments.casefold() != "none":
                problems.append(f"line {line}: segments given for a family labelled not relevant")
                continue
            labels.append((family, "relevance", False))
            labels += [(family, f"segment:{s}", False) for s in segment_ids]
            continue
        labels.append((family, "relevance", True))
        if not segments:
            continue
        chosen = (
            set()
            if segments.casefold() == "none"
            else {s.strip() for s in segments.split(";") if s.strip()}
        )
        if unknown := sorted(chosen - set(segment_ids)):
            problems.append(f"line {line}: unknown segments {unknown} (valid: {segment_ids})")
            continue
        labels += [(family, f"segment:{s}", s in chosen) for s in segment_ids]
    if problems:
        raise LabelError("label file rejected, nothing stored:\n" + "\n".join(problems))
    if not labels:
        raise LabelError("the file contains no labels")
    return labels, blank
