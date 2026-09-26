"""Google Patents page lookup: fetch one patent by number and read its schema.org microdata.

Scope and rules (ADR 0005):

* Only ``/patent/<number>/`` pages are requested (allowed by the site's robots.txt), at a
  configured polite rate. There is no searching; this source looks up known numbers.
* The page *without* a language suffix is fetched, because it shows the document in its
  original language. Title, abstract and claims are accepted only when the page marks them
  as coming from the patent office (``load-source="patent-office"``) or, for the title, when
  the page shows no machine translation. Google's own translations are never stored as
  document text.
* Google labels its priority date, assignees and legal status as unverified. They are
  stored exactly as shown (raw legal-status text kept), with Google Patents recorded as the
  source.
* The page gives no family ID and no list of this document's own priority claims; those
  fields are recorded as missing, never derived.
* Anything that cannot be parsed reliably quarantines the record with reasons. The raw page
  is always kept, so it can be re-normalised when the parser improves.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime

import httpx
from lxml import html
from lxml.html import HtmlElement
from pydantic import ValidationError

from patsquire_plr.config import GooglePatentsPageSettings
from patsquire_plr.domain.patent import (
    CANONICAL_OPTIONAL_FIELDS,
    CitedReference,
    ClassificationCode,
    ForwardCitations,
    LegalStatus,
    MissingReason,
    NormalizationError,
    Party,
    PatentDocument,
    PublicationNumber,
    normalize_classification_code,
    normalize_publication_number,
)
from patsquire_plr.ingest.sources import Fetched, Normalized, SourceInfo
from patsquire_plr.ratelimit import RateLimiter

# 2: family members captured; 3: unparseable list entries mark only that field;
# 4: a kind code that is not found is looked up once without it (ADR 0009)
ADAPTER_VERSION = "4"
SOURCE_API_VERSION = "patents.google.com patent page, schema.org microdata (verified 2026-09-25)"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
MAX_BACKOFF_S = 30.0
OFFICIAL_TEXT = "patent-office"

# Google's legal-status wording -> canonical category. Unlisted wording maps to "other";
# the verbatim text is always kept in ``status_raw``.
LEGAL_STATUS_CATEGORIES: dict[str, str] = {
    "Pending": "pending",
    "Granted": "granted",
    "Active": "active",
    "Expired - Lifetime": "expired",
    "Expired - Fee Related": "lapsed",
    "Abandoned": "withdrawn",
    "Withdrawn": "withdrawn",
    "Revoked": "revoked",
}
CITATION_ORIGIN = {"*": "examiner", "†": "third_party"}


class _ParseError(Exception):
    """A page element required for a reliable record is missing or malformed."""


# ------------------------------------------------------------------ microdata helpers


def _xpath(element: HtmlElement, expression: str) -> list[HtmlElement]:
    """Element results of an XPath query (lxml's return type also covers strings/numbers)."""
    result = element.xpath(expression)
    if not isinstance(result, list):  # pragma: no cover - element queries return lists
        raise _ParseError(f"xpath {expression!r} did not return elements")
    return [item for item in result if isinstance(item, HtmlElement)]


def _is_scope(element: HtmlElement) -> bool:
    return element.get("itemscope") is not None


def _props(scope: HtmlElement, name: str) -> list[HtmlElement]:
    """Elements carrying ``itemprop=name`` whose nearest enclosing item scope is ``scope``."""
    found = []
    for element in scope.iter():
        if element is scope or name not in (element.get("itemprop") or "").split():
            continue
        parent = element.getparent()
        while parent is not None and parent is not scope and not _is_scope(parent):
            parent = parent.getparent()
        if parent is scope:
            found.append(element)
    return found


def _value(element: HtmlElement) -> str:
    for attribute in ("content", "datetime"):
        if element.get(attribute) is not None and element.tag in ("meta", "time"):
            return str(element.get(attribute)).strip()
    return " ".join(element.text_content().split())


def _first_value(scope: HtmlElement, name: str) -> str | None:
    elements = _props(scope, name)
    if not elements:
        return None
    text = _value(elements[0])
    return text or None


def _date(text: str | None, field: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise _ParseError(f"{field}: not an ISO date: {text!r}") from exc


# ------------------------------------------------------------------ the adapter


class _Fields:
    """Accumulates canonical field values, missing reasons and non-fatal warnings."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.missing: dict[str, MissingReason] = {}
        self.warnings: list[str] = []

    def put(
        self,
        field: str,
        value: object,
        reason: MissingReason = MissingReason.NOT_PROVIDED_BY_SOURCE,
    ) -> None:
        if value is None or value == ():
            self.missing[field] = reason
            self.values[field] = None
        else:
            self.values[field] = value

    def lenient(self, field: str, parse: Callable[[], object]) -> None:
        """A list field with an unparseable entry becomes ``unparseable`` (with a warning
        naming the entry) instead of quarantining the whole record."""
        try:
            self.put(field, parse())
        except NormalizationError as exc:
            self.warnings.append(f"{field}: {exc}")
            self.put(field, None, MissingReason.UNPARSEABLE)


class GooglePatentsPageSource:
    max_texts_per_request: int | None = None

    def __init__(
        self,
        source_id: str,
        settings: GooglePatentsPageSettings,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._info = SourceInfo(
            source_id=source_id,
            source_type=settings.type,
            adapter_version=ADAPTER_VERSION,
            source_api_version=SOURCE_API_VERSION,
        )
        self._client = client or httpx.Client(
            timeout=settings.timeout_s,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._limiter = RateLimiter(settings.requests_per_minute, clock=clock, sleep=sleep)
        self._sleep = sleep
        self._now = now

    @property
    def info(self) -> SourceInfo:
        return self._info

    # ---------------------------------------------------------------- fetching

    def fetch(self, keys: Sequence[str]) -> Iterator[Fetched]:
        for key in keys:
            try:
                publication = normalize_publication_number(key)
            except NormalizationError as exc:
                yield Fetched(requested_key=key, status="invalid_request", detail=str(exc))
                continue
            fetched = self._fetch_one(key, f"{self._settings.base_url}/patent/{publication.text}/")
            if fetched.status == "not_found" and publication.kind is not None:
                fetched = self._fetch_without_kind(key, publication)
            yield fetched

    def _fetch_without_kind(self, key: str, publication: PublicationNumber) -> Fetched:
        """Kind codes are written differently in places (e.g. a reissue filed as "E" that
        Google lists as "E1"), so the bare number is tried once. The runner accepts the served
        document only if its kind has the same letter as the requested one (ADR 0009)."""
        bare = f"{publication.country}{publication.number}"
        note = f"kind {publication.kind} not found; looked up {bare}"
        retry = self._fetch_one(key, f"{self._settings.base_url}/patent/{bare}/")
        if retry.status == "ok":
            return retry.model_copy(update={"kind_fallback": True, "detail": note})
        return retry.model_copy(update={"detail": f"{note}: {retry.detail}"})

    def _fetch_one(self, key: str, url: str) -> Fetched:
        detail = ""
        for attempt in range(1, self._settings.max_retries + 2):
            self._limiter.acquire()
            try:
                outcome = self._classify(key, url, self._client.get(url))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                outcome = f"{type(exc).__name__}: {exc}"
            except httpx.HTTPError as exc:  # e.g. TooManyRedirects: not worth retrying
                outcome = Fetched(
                    requested_key=key, status="failed", detail=f"{type(exc).__name__}: {exc}"
                )
            if isinstance(outcome, Fetched):
                return outcome
            detail = outcome
            if attempt <= self._settings.max_retries:
                self._sleep(min(MAX_BACKOFF_S, 2.0**attempt))
        return Fetched(
            requested_key=key, status="failed", detail=f"gave up after retries: {detail}"
        )

    def _classify(self, key: str, url: str, response: httpx.Response) -> Fetched | str:
        """A final outcome, or a string describing a retryable condition."""
        final_path = response.url.path
        if not final_path.startswith("/patent/"):
            return Fetched(
                requested_key=key,
                status="failed",
                detail=f"redirected outside /patent/ to {final_path}; not followed further",
            )
        if response.status_code == HTTP_NOT_FOUND:
            return Fetched(requested_key=key, status="not_found", detail=url)
        if response.status_code == HTTP_OK:
            if b'itemprop="publicationNumber"' not in response.content:
                return Fetched(
                    requested_key=key,
                    status="failed",
                    detail="HTTP 200 but the page has no patent microdata",
                )
            return Fetched(
                requested_key=key,
                status="ok",
                content=response.content,
                content_type=response.headers.get("content-type", "text/html"),
                retrieved_at=self._now(),
            )
        retryable = (
            response.status_code == HTTP_TOO_MANY_REQUESTS
            or response.status_code >= HTTP_SERVER_ERROR
        )
        detail = f"HTTP {response.status_code}"
        return detail if retryable else Fetched(requested_key=key, status="failed", detail=detail)

    # ---------------------------------------------------------------- normalising

    def normalize(
        self, content: bytes, *, raw_record_id: uuid.UUID, retrieved_at: datetime
    ) -> Normalized:
        try:
            document, warnings = self._parse(
                content, raw_record_id, retrieved_at.astimezone(UTC).date()
            )
        except (_ParseError, NormalizationError) as exc:
            return Normalized(documents=(), quarantine_reasons=(str(exc),))
        except ValidationError as exc:
            reasons = tuple(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors(include_url=False, include_input=False)
            )
            return Normalized(documents=(), quarantine_reasons=reasons)
        return Normalized(documents=((document, "article[itemscope]"),), warnings=warnings)

    def _parse(
        self, content: bytes, raw_record_id: uuid.UUID, as_of: date
    ) -> tuple[PatentDocument, tuple[str, ...]]:
        # Decode explicitly: the pages are UTF-8, and guessing from bytes garbles non-Latin text.
        page = html.document_fromstring(content, parser=html.HTMLParser(encoding="utf-8"))
        roots = _xpath(page, "//article[@itemscope]")
        if len(roots) != 1:
            raise _ParseError(f"expected one article item scope, found {len(roots)}")
        root: HtmlElement = roots[0]
        publication_raw, publication = self._publication(root)
        fields = _Fields()
        self._read_text(root, fields)
        self._read_dates(root, publication.text, fields)
        for field in ("family_id_simple", "family_id_extended", "priorities", "ipc"):
            fields.put(field, None)  # the page does not provide these
        fields.lenient("family_members", lambda: self._family_members(root, publication.text))
        fields.put("applicants", self._parties(root, "assigneeOriginal", "applicant"))
        fields.put("inventors", self._parties(root, "inventor", "inventor"))
        fields.put("cpc", self._cpc(root))
        fields.put("legal_status", self._legal_status(root, as_of))
        fields.lenient("backward_citations", lambda: self._backward_citations(root))
        fields.lenient("forward_citations", lambda: self._forward_citations(root, as_of))

        unexpected = set(fields.values) ^ set(CANONICAL_OPTIONAL_FIELDS)
        if unexpected:  # pragma: no cover - guards the field list above
            raise _ParseError(f"parser field coverage mismatch: {sorted(unexpected)}")
        document = PatentDocument(
            raw_record_id=raw_record_id,
            source_id=self._info.source_id,
            publication=publication,
            publication_number_raw=publication_raw,
            missing=fields.missing,  # type: ignore[arg-type]
            **fields.values,  # type: ignore[arg-type]
        )
        return document, tuple(fields.warnings)

    @staticmethod
    def _publication(root: HtmlElement) -> tuple[str, PublicationNumber]:
        publication_raw = _first_value(root, "publicationNumber")
        if publication_raw is None:
            raise _ParseError("page has no publicationNumber")
        publication = normalize_publication_number(publication_raw)
        country, kind = _first_value(root, "countryCode"), _first_value(root, "kindCode")
        if (country, kind) != (publication.country, publication.kind):
            raise _ParseError(
                f"publication number {publication_raw} disagrees with countryCode={country} "
                f"kindCode={kind}"
            )
        return publication_raw, publication

    def _read_text(self, root: HtmlElement, fields: _Fields) -> None:
        if _xpath(root, './/*[@itemprop="translatedLanguage"]'):
            fields.put("title", None, MissingReason.UNPARSEABLE)  # only a machine translation
        else:
            fields.put("title", _first_value(root, "title"))
        abstract, abstract_lang, abstract_reason = self._official_text(
            root, "abstract", ".//abstract"
        )
        claims, claims_lang, claims_reason = self._official_text(
            root, "claims", './/*[contains(@class, "claims")]'
        )
        fields.put("abstract", abstract, abstract_reason)
        fields.put("claims", claims, claims_reason)
        language = abstract_lang or claims_lang
        valid = language if language and re.fullmatch(r"[a-z]{2}", language) else None
        fields.put("language", valid)

    def _read_dates(self, root: HtmlElement, number: str, fields: _Fields) -> None:
        fields.put("application_number_raw", _first_value(root, "applicationNumber"))
        fields.put(
            "earliest_priority_date", _date(_first_value(root, "priorityDate"), "priorityDate")
        )
        fields.put("filing_date", _date(_first_value(root, "filingDate"), "filingDate"))
        fields.put(
            "publication_date", _date(_first_value(root, "publicationDate"), "publicationDate")
        )
        grant_date, grant_reason = self._grant_date(root, number)
        fields.put("grant_date", grant_date, grant_reason)

    @staticmethod
    def _official_text(
        root: HtmlElement, section: str, xpath: str
    ) -> tuple[str | None, str | None, MissingReason]:
        """(text, language, reason-if-missing) for a section.

        Only the patent office's own text is accepted. Text the page marks as OCR output
        (e.g. ``WIPO-OCR``) or as a machine translation is present but not reliable, so the
        field is recorded as ``unparseable`` rather than stored.
        """
        sections = _props(root, section)
        blocks = _xpath(sections[0], xpath) if sections else []
        if not blocks:
            return None, None, MissingReason.NOT_PROVIDED_BY_SOURCE
        block: HtmlElement = blocks[0]
        if block.get("load-source") != OFFICIAL_TEXT:
            return None, None, MissingReason.UNPARSEABLE
        text = " ".join(block.text_content().split())
        lang = block.get("lang")
        return (
            (text or None),
            (lang.lower() if lang else None),
            MissingReason.NOT_PROVIDED_BY_SOURCE,
        )

    @staticmethod
    def _grant_date(root: HtmlElement, number: str) -> tuple[date | None, MissingReason]:
        """This publication's grant date, only if this publication *is* the grant.

        The page's events belong to the application: "Application granted" carries the date of
        the grant publication, which may be a different document (e.g. the B1 of this A1). The
        date is used only when it equals this publication's own publication event; a
        publication that is not the grant gets ``not_applicable``.
        """
        granted: date | None = None
        own_publication: date | None = None
        for event in _props(root, "events"):
            kind = _first_value(event, "type")
            if kind == "granted":
                granted = _date(_first_value(event, "date"), "events.granted.date")
            elif (
                kind == "publication" and _first_value(event, "title") == f"Publication of {number}"
            ):
                own_publication = _date(_first_value(event, "date"), "events.publication.date")
        if own_publication is None:
            return None, MissingReason.NOT_PROVIDED_BY_SOURCE
        if granted is not None and granted == own_publication:
            return granted, MissingReason.NOT_PROVIDED_BY_SOURCE
        return None, MissingReason.NOT_APPLICABLE

    @staticmethod
    def _family_members(root: HtmlElement, own: str) -> tuple[str, ...] | None:
        """The page's "Also Published As" table (microdata ``docdbFamily``): the other
        members of this publication's DOCDB simple family, as the page states them."""
        rows = _props(root, "docdbFamily")
        if not rows:
            return None
        members: list[str] = []
        for row in rows:
            raw = _first_value(row, "publicationNumber")
            if raw is None:
                raise _ParseError("family row without publicationNumber")
            number = normalize_publication_number(raw).text
            if number != own and number not in members:
                members.append(number)
        return tuple(members)

    @staticmethod
    def _parties(root: HtmlElement, prop: str, role: str) -> tuple[Party, ...]:
        names = [_value(e) for e in _props(root, prop)]
        return tuple(
            Party(
                role=role,  # type: ignore[arg-type]
                sequence=i,
                name_raw=name,
                country=None,
                country_missing=MissingReason.NOT_PROVIDED_BY_SOURCE,
            )
            for i, name in enumerate((n for n in names if n), start=1)
        )

    @staticmethod
    def _cpc(root: HtmlElement) -> tuple[ClassificationCode, ...]:
        codes = []
        for item in _xpath(root, './/*[@itemprop="classifications"][@itemscope]'):
            leaf = _first_value(item, "Leaf") == "true"
            is_cpc = _first_value(item, "IsCPC") == "true"
            raw = _first_value(item, "Code")
            if leaf and is_cpc and raw:
                codes.append(
                    ClassificationCode(
                        scheme="cpc", code=normalize_classification_code(raw), code_raw=raw
                    )
                )
        return tuple(codes)

    @staticmethod
    def _legal_status(root: HtmlElement, as_of: date) -> LegalStatus | None:
        scopes = _props(root, "legalStatusIfi")
        raw = _first_value(scopes[0], "status") if scopes else None
        if raw is None:
            return None
        category = LEGAL_STATUS_CATEGORIES.get(raw, "other")
        return LegalStatus(category=category, status_raw=raw, as_of=as_of)  # type: ignore[arg-type]

    @staticmethod
    def _backward_citations(root: HtmlElement) -> tuple[CitedReference, ...] | None:
        # Root-level lists are this publication's complete citations. The tables inside the
        # page's "family" section are family-level views (one row per family) and are not
        # used.
        patents = _props(root, "backwardReferences")
        npl = _props(root, "detailedNonPatentLiterature")
        if not patents and not npl:
            return None
        citations: list[CitedReference] = []
        for row in patents:
            raw = _first_value(row, "publicationNumber")
            if raw is None:
                raise _ParseError("backward citation row without publicationNumber")
            marker = _first_value(row, "examinerCited") or ""
            citations.append(
                CitedReference(
                    kind="patent",
                    publication_number=normalize_publication_number(raw).text,
                    publication_number_raw=raw,
                    npl_text=None,
                    origin=CITATION_ORIGIN.get(marker, "unknown"),  # type: ignore[arg-type]
                )
            )
        for row in npl:
            text = _first_value(row, "title")
            if text is None:
                raise _ParseError("non-patent citation row without text")
            marker = _first_value(row, "examinerCited") or ""
            citations.append(
                CitedReference(
                    kind="npl",
                    publication_number=None,
                    publication_number_raw=None,
                    npl_text=text,
                    origin=CITATION_ORIGIN.get(marker, "unknown"),  # type: ignore[arg-type]
                )
            )
        return tuple(citations)

    @staticmethod
    def _forward_citations(root: HtmlElement, as_of: date) -> ForwardCitations | None:
        # Every citing publication (root level), not the one-per-family view.
        rows = _props(root, "forwardReferences")
        if not rows:
            return None
        numbers = []
        for row in rows:
            raw = _first_value(row, "publicationNumber")
            if raw is None:
                raise _ParseError("forward citation row without publicationNumber")
            numbers.append(normalize_publication_number(raw).text)
        return ForwardCitations(citing_publication_numbers=tuple(numbers), as_of=as_of)
