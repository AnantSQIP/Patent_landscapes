"""Gold-set labelling by people, through a CSV file that opens in Excel (ADR 0011).

**Export.** A uniform random sample of the run's families, fixed by a seed. Families that
already have a relevance label are skipped, so repeated exports add new ones. The file
does not show the system's decisions, so the labels are not biased by them.

**Import.** One row per family:
* ``relevant``: y/n (blank skips the row);
* ``segments``: segment ids separated by ";", or ``none``. Only for relevant families;
  blank records no segment labels.

For a relevant family with segments given, every segment not listed is labelled "no".
Labels are kept per dataset family, so they survive new runs. The newest label per family
and task counts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import ClassificationRun, FamilyText, GoldLabel
from patsquire_plr.errors import PlrError
from patsquire_plr.landscape.store import get_taxonomy

COLUMNS = ("family_key", "publication", "title", "abstract", "relevant", "segments", "notes")
YES, NO = {"y", "yes", "1", "true"}, {"n", "no", "0", "false"}


class LabelError(PlrError):
    """A label file is invalid; nothing from it is stored."""


def _run(session: Session, run_id: uuid.UUID) -> ClassificationRun:
    run = session.get(ClassificationRun, run_id)
    if run is None:
        raise LabelError(f"no classification run {run_id}")
    return run


def export_sample(engine: Engine, run_id: uuid.UUID, *, size: int, seed: int) -> str:
    """CSV text (UTF-8 with BOM for Excel) with ``size`` unlabelled families."""
    with Session(engine) as session:
        run = _run(session, run_id)
        labelled = set(
            session.scalars(
                select(GoldLabel.family_key).where(
                    GoldLabel.dataset_id == run.dataset_id, GoldLabel.task == "relevance"
                )
            )
        )
        texts = session.scalars(select(FamilyText).where(FamilyText.run_id == run_id)).all()
    pool = [t for t in texts if t.family_key not in labelled and (t.title or t.abstract)]
    pool.sort(key=lambda t: hashlib.sha256(f"{seed}:{t.family_key}".encode()).hexdigest())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for t in pool[:size]:
        writer.writerow((t.family_key, t.publication, t.title or "", t.abstract or "", "", "", ""))
    return "﻿" + buffer.getvalue()


@dataclass(frozen=True)
class ImportResult:
    rows_read: int
    families_labelled: int
    labels_stored: int
    skipped_blank: int


def import_labels(engine: Engine, run_id: uuid.UUID, path: Path, *, labeller: str) -> ImportResult:
    if not labeller.strip():
        raise LabelError("labels must name who labelled them")
    content = path.read_bytes()
    with Session(engine) as session:
        run = _run(session, run_id)
        dataset_id = run.dataset_id
        families = set(
            session.scalars(select(FamilyText.family_key).where(FamilyText.run_id == run_id))
        )
    _, taxonomy = get_taxonomy(engine, version_id=run.taxonomy_version_id)
    segment_ids = [s.id for s in taxonomy.spec.segments]
    labels, rows, blank = _parse(content, families, segment_ids)
    digest = hashlib.sha256(content).hexdigest()
    with Session(engine) as session, session.begin():
        session.add_all(
            GoldLabel(
                dataset_id=dataset_id,
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
            event_type="gold_labels_imported",
            actor=labeller.strip(),
            payload={"run_id": str(run_id), "labels": len(labels), "file_sha256": digest},
        )
    return ImportResult(
        rows_read=rows,
        families_labelled=len({f for f, _, _ in labels}),
        labels_stored=len(labels),
        skipped_blank=blank,
    )


def _parse(
    content: bytes, families: set[str], segment_ids: list[str]
) -> tuple[list[tuple[str, str, bool]], int, int]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    missing = {"family_key", "relevant", "segments"} - set(reader.fieldnames or ())
    if missing:
        raise LabelError(f"the file lacks columns {sorted(missing)}")
    labels: list[tuple[str, str, bool]] = []
    problems: list[str] = []
    rows = blank = 0
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        rows += 1
        family = (row.get("family_key") or "").strip()
        relevant = (row.get("relevant") or "").strip().casefold()
        segments = (row.get("segments") or "").strip()
        if not relevant:
            blank += 1
            if segments:
                problems.append(f"line {line}: segments given but 'relevant' is blank")
            continue
        if family not in families:
            problems.append(f"line {line}: family {family!r} is not in this run")
            continue
        if family in seen:
            problems.append(f"line {line}: family {family} appears twice")
            continue
        seen.add(family)
        if relevant not in YES | NO:
            problems.append(f"line {line}: relevant must be y or n, not {relevant!r}")
            continue
        is_relevant = relevant in YES
        labels.append((family, "relevance", is_relevant))
        if not segments:
            continue
        if not is_relevant:
            problems.append(f"line {line}: segments given for a family labelled not relevant")
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
    return labels, rows, blank
