"""Reference-PDF extraction, tested on small PDFs generated here with fpdf2 (TEST-ONLY data)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fpdf import FPDF
from typer.testing import CliRunner

from patsquire_plr.cli import app
from patsquire_plr.reference.extract import (
    ExtractionError,
    ReferenceExtract,
    extract_directory,
    extract_reference,
    render_pages,
    sha256_of,
)

runner = CliRunner()
BODY = "Body text line that is long enough to dominate the size statistics of this page."


def _pdf(path: Path, *, bookmarks: bool, blank_last_page: bool = False) -> Path:
    """Two text pages: a chapter heading, body text, a subheading, captions."""
    pdf = FPDF()
    pdf.set_font("Helvetica", size=10)

    pdf.add_page()
    if bookmarks:
        pdf.start_section("1. Overview", level=0)
    pdf.set_font("Helvetica", size=20)
    pdf.cell(text="1. Overview", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    for _ in range(8):
        pdf.cell(text=BODY, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(text="Figure 1.2 Filing trend by priority year", new_x="LMARGIN", new_y="NEXT")

    pdf.add_page()
    if bookmarks:
        pdf.start_section("1.1 Top applicants", level=1)
    pdf.set_font("Helvetica", size=14)
    pdf.cell(text="1.1 Top applicants", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    for _ in range(8):
        pdf.cell(text=BODY, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(text="Table A1: Search strings", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(text="Box 3 - Data co-operation", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(text="Tablet devices are not a caption", new_x="LMARGIN", new_y="NEXT")

    if blank_last_page:
        pdf.add_page()
    pdf.output(str(path))
    return path


def test_bookmarks_are_the_heading_source_when_present(tmp_path: Path) -> None:
    result = extract_reference(_pdf(tmp_path / "r.pdf", bookmarks=True))

    assert result.heading_source == "bookmark"
    assert [(h.level, h.title, h.page) for h in result.headings] == [
        (0, "1. Overview", 1),
        (1, "1.1 Top applicants", 2),
    ]


def test_font_size_fallback_ranks_levels_by_size(tmp_path: Path) -> None:
    result = extract_reference(_pdf(tmp_path / "r.pdf", bookmarks=False))

    assert result.heading_source == "font_size"
    assert [(h.level, h.title, h.page) for h in result.headings] == [
        (0, "1. Overview", 1),
        (1, "1.1 Top applicants", 2),
    ]


def test_captions_are_found_with_kind_label_and_page(tmp_path: Path) -> None:
    result = extract_reference(_pdf(tmp_path / "r.pdf", bookmarks=True))

    assert [(c.kind, c.label, c.text, c.page) for c in result.captions] == [
        ("figure", "Figure 1.2", "Filing trend by priority year", 1),
        ("table", "Table A1", "Search strings", 2),
        ("box", "Box 3", "Data co-operation", 2),
    ]


def test_pages_without_text_are_reported(tmp_path: Path) -> None:
    result = extract_reference(_pdf(tmp_path / "r.pdf", bookmarks=True, blank_last_page=True))

    assert result.page_count == 3
    assert result.pages_without_text == (3,)


def test_metadata_identifies_the_exact_file(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "r.pdf", bookmarks=True)
    result = extract_reference(path)

    assert result.file_name == "r.pdf"
    assert result.sha256 == sha256_of(path)
    assert result.extractor_version == "1"


def test_pdf_without_any_text_is_an_error(tmp_path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.add_page()
    path = tmp_path / "scanned.pdf"
    pdf.output(str(path))

    with pytest.raises(ExtractionError, match="no extractable text on any of 2 pages"):
        extract_reference(path)


def test_non_pdf_is_an_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.pdf"
    path.write_text("not a pdf", encoding="utf-8")

    with pytest.raises(ExtractionError, match=r"notes\.pdf: cannot be read as a PDF"):
        extract_reference(path)


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="PDF not found"):
        extract_reference(tmp_path / "absent.pdf")


def test_extract_directory_writes_valid_json_per_pdf(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    _pdf(pdf_dir / "a.pdf", bookmarks=True)
    _pdf(pdf_dir / "b.pdf", bookmarks=False)

    written = extract_directory(pdf_dir, tmp_path / "out")

    assert [p.name for p in written] == ["a.json", "b.json"]
    parsed = ReferenceExtract.model_validate_json(written[1].read_text(encoding="utf-8"))
    assert parsed.heading_source == "font_size"


def test_extract_directory_without_pdfs_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="No PDF files found"):
        extract_directory(tmp_path, tmp_path / "out")


def test_render_pages_writes_pngs_and_rejects_bad_pages(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "r.pdf", bookmarks=True)

    written = render_pages(path, [1, 2], tmp_path / "png", scale=0.5)

    assert [p.name for p in written] == ["r-p001.png", "r-p002.png"]
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in written)
    with pytest.raises(ExtractionError, match=r"page 3 out of range 1\.\.2"):
        render_pages(path, [3], tmp_path / "png")


def test_cli_extract_and_render(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    path = _pdf(pdf_dir / "a.pdf", bookmarks=True)

    extracted = runner.invoke(
        app,
        ["reference", "extract", "--pdf-dir", str(pdf_dir), "--out-dir", str(tmp_path / "out")],
    )
    rendered = runner.invoke(
        app, ["reference", "render", str(path), "1", "--out-dir", str(tmp_path / "png")]
    )

    assert extracted.exit_code == 0, extracted.output
    assert json.loads((tmp_path / "out" / "a.json").read_text(encoding="utf-8"))["page_count"] == 2
    assert rendered.exit_code == 0, rendered.output
    assert (tmp_path / "png" / "a-p001.png").is_file()


def test_cli_reports_extraction_errors_with_exit_1(tmp_path: Path) -> None:
    extracted = runner.invoke(app, ["reference", "extract", "--pdf-dir", str(tmp_path)])
    rendered = runner.invoke(app, ["reference", "render", str(tmp_path / "absent.pdf"), "1"])

    assert extracted.exit_code == 1
    assert "No PDF files found" in extracted.stderr
    assert rendered.exit_code == 1
    assert "PDF not found" in rendered.stderr


def test_extract_directory_is_recursive_and_case_insensitive(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    (pdf_dir / "nested").mkdir(parents=True)
    _pdf(pdf_dir / "a.pdf", bookmarks=True)
    _pdf(pdf_dir / "nested" / "b.PDF", bookmarks=True)

    written = extract_directory(pdf_dir, pdf_dir / "extracted")

    assert [p.name for p in written] == ["a.json", "b.json"]


def test_extract_directory_rejects_clashing_stems(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "pdfs"
    (pdf_dir / "nested").mkdir(parents=True)
    _pdf(pdf_dir / "report.pdf", bookmarks=True)
    _pdf(pdf_dir / "nested" / "report.pdf", bookmarks=True)

    with pytest.raises(ExtractionError, match=r"share a file name stem.*report"):
        extract_directory(pdf_dir, tmp_path / "out")
