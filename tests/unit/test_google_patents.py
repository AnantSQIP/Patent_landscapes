"""Google Patents adapter: contract tests on real recorded pages and fetch behaviour.

The pages in tests/fixtures/google_patents are real (see its README). If Google changes the
page structure, these tests fail, which is the intended alarm.
"""

from __future__ import annotations

import gzip
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from patsquire_plr.config import GooglePatentsPageSettings
from patsquire_plr.domain.patent import MissingReason, PatentDocument
from patsquire_plr.ingest.google_patents import (
    LEGAL_STATUS_CATEGORIES,
    SOURCE_API_VERSION,
    GooglePatentsPageSource,
)
from patsquire_plr.ingest.sources import build_source

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "google_patents"
RETRIEVED = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SETTINGS = GooglePatentsPageSettings(
    type="google_patents_page",
    base_url="https://patents.google.com",
    user_agent="tests",
    requests_per_minute=60,
    timeout_s=5,
    max_retries=2,
)


def _page(number: str) -> bytes:
    return gzip.decompress((FIXTURES / f"{number}.html.gz").read_bytes())


def _source(
    handler: httpx.MockTransport | None = None, sleeps: list[float] | None = None
) -> GooglePatentsPageSource:
    log = sleeps if sleeps is not None else []
    return GooglePatentsPageSource(
        "google_patents",
        SETTINGS,
        client=httpx.Client(
            transport=handler or httpx.MockTransport(lambda r: httpx.Response(500)),
            follow_redirects=True,
        ),
        clock=lambda: 0.0,
        sleep=log.append,
        now=lambda: RETRIEVED,
    )


def _parse(number: str) -> PatentDocument:
    result = _source().normalize(
        _page(number), raw_record_id=uuid.UUID(int=7), retrieved_at=RETRIEVED
    )
    assert result.quarantine_reasons == ()
    [(document, pointer)] = result.documents
    assert pointer == "article[itemscope]"
    return document


# ---------------------------------------------------------------- contract tests (real pages)


def test_us_grant_full_record() -> None:
    d = _parse("US10000000B2")
    assert d.publication.text == "US10000000B2"
    assert d.title == "Coherent LADAR using intra-pixel quadrature detection"
    assert (d.earliest_priority_date, d.filing_date, d.publication_date, d.grant_date) == (
        date(2015, 3, 10), date(2015, 3, 10), date(2018, 6, 19), date(2018, 6, 19)
    )  # fmt: skip
    assert d.application_number_raw == "US14/643,719"
    assert d.language == "en"
    assert d.abstract is not None
    assert d.abstract.startswith("A frequency modulated (coherent) laser detection")
    assert d.claims is not None
    assert len(d.claims) > 8000
    assert [p.name_raw for p in d.applicants or ()] == ["Raytheon Co"]
    assert [p.name_raw for p in d.inventors or ()] == ["Joseph Marron"]
    assert all(
        p.country_missing is MissingReason.NOT_PROVIDED_BY_SOURCE for p in d.applicants or ()
    )
    assert [c.code for c in d.cpc or ()][:2] == ["G01S7/4863", "G01S13/89"]
    assert d.legal_status is not None
    assert (d.legal_status.category, d.legal_status.status_raw, d.legal_status.as_of) == (
        "active",
        "Active",
        date(2026, 9, 25),
    )
    citations = d.backward_citations or ()
    assert sum(c.kind == "patent" for c in citations) == 5
    assert sum(c.kind == "npl" for c in citations) == 7
    assert citations[0].publication_number == "US5093563A"
    assert citations[0].origin == "examiner"
    assert d.forward_citations is not None
    # every citing publication (22), not the one-row-per-family view (14)
    assert len(d.forward_citations.citing_publication_numbers) == 22
    assert "US10845468B2" in d.forward_citations.citing_publication_numbers


def test_fields_the_page_never_provides_are_explicitly_missing() -> None:
    d = _parse("US10000000B2")
    for field in ("family_id_simple", "family_id_extended", "priorities", "ipc"):
        assert d.missing[field] is MissingReason.NOT_PROVIDED_BY_SOURCE


def test_original_language_text_is_decoded_as_utf8() -> None:
    d = _parse("CN112345678A")
    assert d.title == "变压器故障率预测模型获取方法及系统、可读存储介质"
    assert d.language == "zh"
    assert d.abstract is not None
    assert d.abstract.startswith("本发明公开了一种变压器故障率预测模型获取方法")
    assert len(d.applicants or ()) == 3
    assert len(d.inventors or ()) == 12


@pytest.mark.parametrize("number", ["EP3123456A1", "WO2020123456A1"])
def test_ocr_claims_are_not_stored(number: str) -> None:
    d = _parse(number)
    assert d.claims is None
    assert d.missing["claims"] is MissingReason.UNPARSEABLE


def test_application_publication_carries_its_applications_status() -> None:
    d = _parse("US20160266243A1")
    assert d.legal_status is not None
    assert d.legal_status.category == "granted"  # the application behind this A1 was granted
    # ...but this A1 is not the grant publication (US10000000B2 is), so no grant date here
    assert d.grant_date is None
    assert d.missing["grant_date"] is MissingReason.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("number", "grant_date"),
    [("US10000000B2", date(2018, 6, 19)), ("US5093563A", date(1992, 3, 3))],
)
def test_grant_date_only_on_the_grant_publication(number: str, grant_date: date) -> None:
    assert _parse(number).grant_date == grant_date


@pytest.mark.parametrize(
    "number", ["US20160266243A1", "CN112345678A", "EP3123456A1", "WO2020123456A1"]
)
def test_non_grant_publications_have_no_grant_date(number: str) -> None:
    d = _parse(number)
    assert d.grant_date is None
    assert d.missing["grant_date"] is MissingReason.NOT_APPLICABLE


def test_unmapped_legal_status_is_other_with_raw_text_kept() -> None:
    d = _parse("WO2020123456A1")
    assert d.legal_status is not None
    assert (d.legal_status.category, d.legal_status.status_raw) == ("other", "Ceased")
    assert "Ceased" not in LEGAL_STATUS_CATEGORIES


def test_old_patent_and_expired_status() -> None:
    d = _parse("US5093563A")
    assert d.legal_status is not None
    assert d.legal_status.category == "expired"
    assert d.forward_citations is not None
    assert len(d.forward_citations.citing_publication_numbers) == 155


def test_every_fixture_parses_without_quarantine() -> None:
    for page in sorted(FIXTURES.glob("*.html.gz")):
        number = page.name.removesuffix(".html.gz")
        assert _parse(number).publication.text == number


# ---------------------------------------------------------------- quarantine paths


def test_mismatched_publication_metadata_is_quarantined() -> None:
    page = _page("US10000000B2").replace(
        b'itemprop="kindCode" content="B2"', b'itemprop="kindCode" content="B1"'
    )
    result = _source().normalize(page, raw_record_id=uuid.UUID(int=1), retrieved_at=RETRIEVED)
    assert result.documents == ()
    assert "disagrees with countryCode=US kindCode=B1" in result.quarantine_reasons[0]


def test_page_without_article_scope_is_quarantined() -> None:
    result = _source().normalize(
        b"<html><body>No patent here</body></html>",
        raw_record_id=uuid.UUID(int=1),
        retrieved_at=RETRIEVED,
    )
    assert result.quarantine_reasons == ("expected one article item scope, found 0",)


def test_malformed_cpc_code_quarantines_rather_than_guessing() -> None:
    page = _page("US10000000B2").replace(b'itemprop="Code">G01S7/4863<', b'itemprop="Code">G01S7<')
    result = _source().normalize(page, raw_record_id=uuid.UUID(int=1), retrieved_at=RETRIEVED)
    assert result.documents == ()
    assert "CPC/IPC" in result.quarantine_reasons[0]


# ---------------------------------------------------------------- fetching


def test_fetch_outcomes() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/patent/US10000000B2/":
            return httpx.Response(
                200,
                content=_page("US10000000B2"),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404)

    results = list(
        _source(httpx.MockTransport(handler)).fetch(
            ["US 10,000,000 B2", "US1234B1", "not a number"]
        )
    )

    assert [(r.requested_key, r.status) for r in results] == [
        ("US 10,000,000 B2", "ok"),
        ("US1234B1", "not_found"),
        ("not a number", "invalid_request"),
    ]
    assert results[0].retrieved_at == RETRIEVED
    assert results[0].content_type == "text/html; charset=utf-8"
    assert requested == [
        "https://patents.google.com/patent/US10000000B2/",
        "https://patents.google.com/patent/US1234B1/",
    ]


def test_rate_limited_and_server_errors_are_retried_with_backoff() -> None:
    replies = [
        httpx.Response(429),
        httpx.Response(503),
        httpx.Response(200, content=_page("US10000000B2")),
    ]
    sleeps: list[float] = []
    source = _source(httpx.MockTransport(lambda r: replies.pop(0)), sleeps)

    [result] = source.fetch(["US10000000B2"])

    assert result.status == "ok"
    assert sleeps == [2.0, 4.0]


def test_retries_are_bounded_and_reported() -> None:
    source = _source(httpx.MockTransport(lambda r: httpx.Response(503)))
    [result] = source.fetch(["US10000000B2"])
    assert result.status == "failed"
    assert result.detail == "gave up after retries: HTTP 503"


def test_client_errors_fail_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403)

    [result] = _source(httpx.MockTransport(handler)).fetch(["US10000000B2"])
    assert (result.status, result.detail) == ("failed", "HTTP 403")
    assert len(calls) == 1


def test_timeouts_are_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    [result] = _source(httpx.MockTransport(handler)).fetch(["US10000000B2"])
    assert result.status == "failed"
    assert "ConnectTimeout" in (result.detail or "")


def test_redirects_outside_patent_pages_are_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/patent/"):
            return httpx.Response(
                302, headers={"location": "https://patents.google.com/?q=US10000000B2"}
            )
        return httpx.Response(200, content=b"search page")

    [result] = _source(httpx.MockTransport(handler)).fetch(["US10000000B2"])
    assert result.status == "failed"
    assert "outside /patent/" in (result.detail or "")


def test_200_without_patent_microdata_is_a_failure() -> None:
    [result] = _source(
        httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>consent page</html>"))
    ).fetch(["US10000000B2"])
    assert (result.status, result.detail) == (
        "failed",
        "HTTP 200 but the page has no patent microdata",
    )


def test_source_identifies_its_format_version() -> None:
    info = _source().info
    assert (info.source_id, info.source_type, info.adapter_version) == (
        "google_patents",
        "google_patents_page",
        "2",
    )
    assert info.source_api_version == SOURCE_API_VERSION


def test_build_source_constructs_the_configured_adapter() -> None:
    source = build_source("google_patents", SETTINGS)
    assert source.info.source_type == "google_patents_page"
    with pytest.raises(TypeError, match="unsupported settings"):
        build_source("x", object())


HEADING = re.compile(rb"<h2>(Patent Citations|Non-Patent Citations|Cited By) \((\d+)\)</h2>")


@pytest.mark.parametrize(
    "number", sorted(p.name.removesuffix(".html.gz") for p in FIXTURES.glob("*.html.gz"))
)
def test_citation_counts_match_the_pages_own_headings(number: str) -> None:
    headings = {m[0].decode(): int(m[1]) for m in HEADING.findall(_page(number))}
    citations = _parse(number).backward_citations or ()
    assert sum(c.kind == "patent" for c in citations) == headings.get("Patent Citations", 0)
    assert sum(c.kind == "npl" for c in citations) == headings.get("Non-Patent Citations", 0)


def test_npl_citation_origin_is_kept() -> None:
    npl = [c for c in _parse("CN112345678A").backward_citations or () if c.kind == "npl"]
    assert len(npl) == 4
    assert {c.origin for c in npl} == {"examiner"}


def test_machine_translated_title_is_not_stored() -> None:
    page = _page("US10000000B2").replace(
        b'<span itemprop="title">',
        b'<span itemprop="translatedLanguage">German</span><span itemprop="title">',
        1,
    )
    result = _source().normalize(page, raw_record_id=uuid.UUID(int=1), retrieved_at=RETRIEVED)
    [(d, _)] = result.documents
    assert d.title is None
    assert d.missing["title"] is MissingReason.UNPARSEABLE


def test_too_many_redirects_fails_the_item_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(302, headers={"location": str(request.url)})

    [result] = _source(httpx.MockTransport(handler)).fetch(["US10000000B2"])
    assert result.status == "failed"
    assert "TooManyRedirects" in (result.detail or "")


def test_family_members_are_the_also_published_as_table() -> None:
    d = _parse("US10000000B2")
    assert d.family_members is not None
    assert len(d.family_members) == 14
    assert {"US20160266243A1", "EP3268771B1", "WO2016144528A1"} <= set(d.family_members)
    assert "US10000000B2" not in d.family_members
    # the A1 of the same application lists the B2 as a family member
    a1 = _parse("US20160266243A1")
    assert a1.family_members is not None
    assert "US10000000B2" in a1.family_members


@pytest.mark.parametrize(
    "number", sorted(p.name.removesuffix(".html.gz") for p in FIXTURES.glob("*.html.gz"))
)
def test_family_members_never_include_the_document_itself(number: str) -> None:
    d = _parse(number)
    assert d.family_members is None or number not in d.family_members
