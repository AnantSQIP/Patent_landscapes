"""Store and load canonical ``PatentDocument`` records.

``load_document(store_document(doc)) == doc`` holds exactly (tested). Nothing is lost or
defaulted on the way through the database.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from patsquire_plr.db.models import (
    DocumentCitation,
    DocumentClassification,
    DocumentForwardCitation,
    DocumentParty,
    DocumentPriority,
    PatentDocumentRow,
)
from patsquire_plr.domain.patent import (
    CitedReference,
    ClassificationCode,
    ForwardCitations,
    LegalStatus,
    MissingReason,
    Party,
    PatentDocument,
    Priority,
    PublicationNumber,
)
from patsquire_plr.errors import PlrError


class DocumentNotFoundError(PlrError):
    pass


def store_document(session: Session, document: PatentDocument, *, raw_pointer: str) -> uuid.UUID:
    """Insert ``document`` and its child rows. ``raw_pointer`` locates it within the raw
    payload (e.g. an XML path or a CSV row number)."""
    document_id = uuid.uuid4()
    legal = document.legal_status
    session.add(
        PatentDocumentRow(
            id=document_id,
            raw_record_id=document.raw_record_id,
            raw_pointer=raw_pointer,
            source_id=document.source_id,
            publication_country=document.publication.country,
            publication_number=document.publication.number,
            publication_kind=document.publication.kind,
            publication_number_raw=document.publication_number_raw,
            application_number_raw=document.application_number_raw,
            family_id_simple=document.family_id_simple,
            family_id_extended=document.family_id_extended,
            earliest_priority_date=document.earliest_priority_date,
            filing_date=document.filing_date,
            publication_date=document.publication_date,
            grant_date=document.grant_date,
            title=document.title,
            abstract=document.abstract,
            claims=document.claims,
            language=document.language,
            legal_status_category=None if legal is None else legal.category,
            legal_status_raw=None if legal is None else legal.status_raw,
            legal_status_as_of=None if legal is None else legal.as_of,
            forward_citations_as_of=(
                None if document.forward_citations is None else document.forward_citations.as_of
            ),
            missing={field: reason.value for field, reason in sorted(document.missing.items())},
        )
    )
    session.flush()  # parent row first: children reference it
    for party in (*(document.applicants or ()), *(document.inventors or ())):
        session.add(
            DocumentParty(
                document_id=document_id,
                role=party.role,
                sequence=party.sequence,
                name_raw=party.name_raw,
                country=party.country,
                country_missing=None
                if party.country_missing is None
                else party.country_missing.value,
            )
        )
    for ordinal, priority in enumerate(document.priorities or (), start=1):
        session.add(
            DocumentPriority(
                document_id=document_id,
                ordinal=ordinal,
                number_raw=priority.number_raw,
                country=priority.country,
                priority_date=priority.date,
            )
        )
    for codes in (document.cpc or (), document.ipc or ()):
        for ordinal, code in enumerate(codes, start=1):
            session.add(
                DocumentClassification(
                    document_id=document_id,
                    scheme=code.scheme,
                    code=code.code,
                    code_raw=code.code_raw,
                    ordinal=ordinal,
                )
            )
    for ordinal, citation in enumerate(document.backward_citations or (), start=1):
        session.add(
            DocumentCitation(
                document_id=document_id,
                ordinal=ordinal,
                kind=citation.kind,
                publication_number=citation.publication_number,
                publication_number_raw=citation.publication_number_raw,
                npl_text=citation.npl_text,
                origin=citation.origin,
            )
        )
    if document.forward_citations is not None:
        for ordinal, number in enumerate(document.forward_citations.citing_publication_numbers, 1):
            session.add(
                DocumentForwardCitation(
                    document_id=document_id, citing_publication_number=number, ordinal=ordinal
                )
            )
    session.flush()
    return document_id


def load_document(session: Session, document_id: uuid.UUID) -> PatentDocument:
    row = session.get(PatentDocumentRow, document_id)
    if row is None:
        raise DocumentNotFoundError(f"patent_document {document_id} does not exist")
    missing = {str(k): MissingReason(str(v)) for k, v in row.missing.items()}

    def present(field: str) -> bool:
        return field not in missing

    parties = session.scalars(
        select(DocumentParty)
        .where(DocumentParty.document_id == document_id)
        .order_by(DocumentParty.role, DocumentParty.sequence)
    ).all()
    codes = session.scalars(
        select(DocumentClassification)
        .where(DocumentClassification.document_id == document_id)
        .order_by(DocumentClassification.scheme, DocumentClassification.ordinal)
    ).all()

    def party_tuple(role: str) -> tuple[Party, ...]:
        return tuple(
            Party(
                role=p.role,  # type: ignore[arg-type]
                sequence=p.sequence,
                name_raw=p.name_raw,
                country=p.country,
                country_missing=None
                if p.country_missing is None
                else MissingReason(p.country_missing),
            )
            for p in parties
            if p.role == role
        )

    def code_tuple(scheme: str) -> tuple[ClassificationCode, ...]:
        return tuple(
            ClassificationCode(scheme=c.scheme, code=c.code, code_raw=c.code_raw)  # type: ignore[arg-type]
            for c in codes
            if c.scheme == scheme
        )

    priorities = session.scalars(
        select(DocumentPriority)
        .where(DocumentPriority.document_id == document_id)
        .order_by(DocumentPriority.ordinal)
    ).all()
    citations = session.scalars(
        select(DocumentCitation)
        .where(DocumentCitation.document_id == document_id)
        .order_by(DocumentCitation.ordinal)
    ).all()
    citing = session.scalars(
        select(DocumentForwardCitation.citing_publication_number)
        .where(DocumentForwardCitation.document_id == document_id)
        .order_by(DocumentForwardCitation.ordinal)
    ).all()

    legal = None
    if row.legal_status_category is not None:
        if row.legal_status_raw is None or row.legal_status_as_of is None:
            raise PlrError(f"patent_document {document_id}: incomplete legal status row")
        legal = LegalStatus(
            category=row.legal_status_category,  # type: ignore[arg-type]
            status_raw=row.legal_status_raw,
            as_of=row.legal_status_as_of,
        )

    forward = None
    if present("forward_citations"):
        if row.forward_citations_as_of is None:
            raise PlrError(
                f"patent_document {document_id}: forward citations present without as_of"
            )
        forward = ForwardCitations(
            citing_publication_numbers=tuple(citing), as_of=row.forward_citations_as_of
        )

    return PatentDocument(
        raw_record_id=row.raw_record_id,
        source_id=row.source_id,
        publication=PublicationNumber(
            country=row.publication_country,
            number=row.publication_number,
            kind=row.publication_kind,
        ),
        publication_number_raw=row.publication_number_raw,
        application_number_raw=row.application_number_raw,
        family_id_simple=row.family_id_simple,
        family_id_extended=row.family_id_extended,
        earliest_priority_date=row.earliest_priority_date,
        priorities=(
            tuple(
                Priority(number_raw=p.number_raw, country=p.country, date=p.priority_date)
                for p in priorities
            )
            if present("priorities")
            else None
        ),
        filing_date=row.filing_date,
        publication_date=row.publication_date,
        grant_date=row.grant_date,
        title=row.title,
        abstract=row.abstract,
        claims=row.claims,
        language=row.language,
        applicants=party_tuple("applicant") if present("applicants") else None,
        inventors=party_tuple("inventor") if present("inventors") else None,
        cpc=code_tuple("cpc") if present("cpc") else None,
        ipc=code_tuple("ipc") if present("ipc") else None,
        legal_status=legal,
        backward_citations=(
            tuple(
                CitedReference(
                    kind=c.kind,  # type: ignore[arg-type]
                    publication_number=c.publication_number,
                    publication_number_raw=c.publication_number_raw,
                    npl_text=c.npl_text,
                    origin=c.origin,  # type: ignore[arg-type]
                )
                for c in citations
            )
            if present("backward_citations")
            else None
        ),
        forward_citations=forward,
        missing=missing,  # type: ignore[arg-type]
    )
