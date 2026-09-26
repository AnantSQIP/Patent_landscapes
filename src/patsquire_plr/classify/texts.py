"""The text a family is judged on (ADR 0011).

A family is judged on one representative publication. It is chosen by fixed rules, so the
choice is reproducible:
1. publications with an abstract first;
2. then English text (the models are English-centred);
3. then the earliest publication date;
4. then the lowest publication number.

A family whose publications have neither title nor abstract has no text, and is counted as
``no_text``. It is never guessed.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.db.models import DatasetDocument, DatasetFamilyMember, PatentDocumentRow


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FamilyText:
    family_key: str
    publication: str | None
    language: str | None
    title: str | None
    abstract: str | None

    @property
    def text(self) -> str | None:
        parts = [p.strip() for p in (self.title, self.abstract) if p and p.strip()]
        return "\n\n".join(parts) if parts else None

    @property
    def sha256(self) -> str | None:
        text = self.text
        return text_sha256(text) if text else None


@dataclass(frozen=True)
class _Candidate:
    publication: str
    language: str | None
    title: str | None
    abstract: str | None
    published: date | None

    def rank(self) -> tuple[bool, bool, date, str]:
        return (
            not (self.abstract and self.abstract.strip()),
            self.language != "en",
            self.published or date.max,
            self.publication,
        )


def family_texts(engine: Engine, dataset_id: uuid.UUID) -> list[FamilyText]:
    """One text per family of the dataset, in family-key order."""
    with Session(engine) as session:
        rows = session.execute(
            select(
                DatasetFamilyMember.family_key,
                DatasetDocument.publication,
                PatentDocumentRow.language,
                PatentDocumentRow.title,
                PatentDocumentRow.abstract,
                PatentDocumentRow.publication_date,
            )
            .join(
                DatasetDocument,
                (DatasetDocument.dataset_id == DatasetFamilyMember.dataset_id)
                & (DatasetDocument.publication == DatasetFamilyMember.publication),
            )
            .join(PatentDocumentRow, PatentDocumentRow.id == DatasetDocument.document_id)
            .where(
                DatasetFamilyMember.dataset_id == dataset_id,
                DatasetFamilyMember.in_dataset.is_(True),
                DatasetDocument.decision == "selected",
            )
        ).all()
    families: dict[str, list[_Candidate]] = {}
    for key, publication, language, title, abstract, published in rows:
        families.setdefault(key, []).append(
            _Candidate(publication, language, title, abstract, published)
        )
    texts = []
    for key in sorted(families):
        best = min(families[key], key=_Candidate.rank)
        texts.append(
            FamilyText(
                family_key=key,
                publication=best.publication,
                language=best.language,
                title=best.title,
                abstract=best.abstract,
            )
        )
    return texts
