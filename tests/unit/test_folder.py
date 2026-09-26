"""Folder of user-supplied patent PDFs (TEST-ONLY PDFs generated with fpdf2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fpdf import FPDF
from typer.testing import CliRunner

from patsquire_plr.cli import app
from patsquire_plr.ingest.folder import FolderError, scan_folder
from tests.support import WriteConfig, base_config


def _pdf(path: Path, text: str | None) -> Path:
    pdf = FPDF()
    pdf.add_page()
    if text is not None:
        pdf.set_font("Helvetica", size=10)
        pdf.cell(text=text)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def test_scan_reads_numbers_hashes_and_text_checks(tmp_path: Path) -> None:
    _pdf(tmp_path / "US10521786B2.pdf", "(10) Patent No.: US 10,521,786 B2")
    _pdf(tmp_path / "US11227687B2.pdf", None)  # scanned image: no text layer
    _pdf(tmp_path / "US9288556B2.pdf", "(10) Patent No.: US 8,000,001 B2")  # wrong document
    _pdf(tmp_path / "notes.pdf", "meeting notes")
    _pdf(tmp_path / "sub" / "EP2134658B1.pdf", None)

    scan = scan_folder(tmp_path)

    by_name = {f.name: f for f in scan.files}
    ok = by_name["US10521786B2.pdf"]
    assert (ok.publication, ok.has_text, ok.number_in_text) == ("US10521786B2", True, True)
    assert ok.sha256 == hashlib.sha256((tmp_path / "US10521786B2.pdf").read_bytes()).hexdigest()
    assert (by_name["US11227687B2.pdf"].has_text, by_name["US11227687B2.pdf"].number_in_text) == (
        False,
        None,
    )
    assert by_name["US9288556B2.pdf"].number_in_text is False
    assert by_name["notes.pdf"].publication is None
    assert "notes" in (by_name["notes.pdf"].problem or "")
    assert str(Path("sub") / "EP2134658B1.pdf") in by_name
    assert scan.keys == ["US10521786B2", "US11227687B2", "US9288556B2", "EP2134658B1"]  # path order
    summary = scan.summary()
    assert summary["not_a_publication_number"] == ["notes.pdf"]
    assert summary["number_missing_from_text"] == ["US9288556B2.pdf"]
    assert summary["with_text_layer"] == 3


def test_empty_or_missing_folders_fail(tmp_path: Path) -> None:
    with pytest.raises(FolderError, match="no PDF files"):
        scan_folder(tmp_path)
    with pytest.raises(FolderError, match="not a folder"):
        scan_folder(tmp_path / "absent")


def test_unreadable_pdf_fails_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "US1B1.pdf").write_bytes(b"not a pdf")
    with pytest.raises(FolderError, match=r"US1B1\.pdf: not a readable PDF"):
        scan_folder(tmp_path)


@pytest.mark.usefixtures("secrets_env")
def test_cli_scan_only_fetches_nothing(tmp_path: Path, write_config: WriteConfig) -> None:
    folder = tmp_path / "pdfs"
    _pdf(folder / "US10521786B2.pdf", None)
    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "folder",
            str(folder),
            "--scan-only",
            "--config",
            str(write_config(base_config())),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stderr)["folder_scan"]["requestable"] == 1
