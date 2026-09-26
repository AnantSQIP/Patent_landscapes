"""A folder of patent PDFs supplied by the user (ADR 0005, ``local_pdf``).

Scanned patent PDFs are usually images without a text layer, and OCR is out of scope. The
folder therefore supplies *which* patents to use: each file name must be a publication number
(e.g. ``US10521786B2.pdf``). The structured record is then looked up by number from a
configured source. Every file is recorded with the batch (name, size, SHA-256, whether it has a
text layer), so each record traces back to the exact file the user supplied.

Checks, reported rather than guessed:

* a file whose name is not a publication number is listed and not requested;
* where a PDF has a text layer, its first pages must contain the digits of the file name's
  number as a whole number (digit-group separators such as "10,521,786" are ignored);
  otherwise the file is flagged. This confirms the number appears, not where: a cover page
  listing cited patents can still let a misnamed file pass.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pypdfium2 as pdfium
from pydantic import BaseModel, ConfigDict

from patsquire_plr.domain.patent import NormalizationError, normalize_publication_number
from patsquire_plr.errors import PlrError

TEXT_PAGES_CHECKED = 2


class FolderError(PlrError):
    """The folder cannot be scanned."""


class SuppliedFile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    name: str
    size_bytes: int
    sha256: str
    publication: str | None
    problem: str | None
    has_text: bool
    number_in_text: bool | None  # None when the file has no text layer


class FolderScan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    folder: str
    files: tuple[SuppliedFile, ...]

    @property
    def keys(self) -> list[str]:
        """File-name stems of the files that name a publication, in path order."""
        return [Path(f.name).stem for f in self.files if f.publication is not None]

    def provenance(self) -> dict[str, object]:
        """Recorded with the lookup batch, so each record traces back to the file that asked
        for it."""
        return {
            "type": "user_folder",
            "folder": self.folder,
            "files": [f.model_dump() for f in self.files],
        }

    def summary(self) -> dict[str, object]:
        return {
            "files": len(self.files),
            "requestable": len(self.keys),
            "not_a_publication_number": [f.name for f in self.files if f.publication is None],
            "with_text_layer": sum(f.has_text for f in self.files),
            "number_missing_from_text": [f.name for f in self.files if f.number_in_text is False],
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _first_pages_text(path: Path) -> str:
    document = pdfium.PdfDocument(path)
    try:
        return " ".join(
            document[i].get_textpage().get_text_range()
            for i in range(min(len(document), TEXT_PAGES_CHECKED))
        )
    finally:
        document.close()


def _number_in_text(number: str, text: str) -> bool:
    """The number's digits appear in ``text`` as a whole number, e.g. ``RE48951`` in
    "Re. 48,951" or ``10521786`` in "US 10,521,786 B2", but not inside "110521786"."""
    digits = re.sub(r"\D", "", number)
    joined = re.sub(r"(?<=\d)[\s,./-](?=\d)", "", text)
    return re.search(rf"(?<!\d){digits}(?!\d)", joined) is not None


def scan_folder(folder: Path) -> FolderScan:
    folder = folder.resolve()
    if not folder.is_dir():
        raise FolderError(f"not a folder: {folder}")
    paths = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    if not paths:
        raise FolderError(f"no PDF files in {folder}")
    files = []
    for path in paths:
        try:
            publication = normalize_publication_number(path.stem)
            number, problem = publication.text, None
        except NormalizationError as exc:
            publication, number, problem = None, None, str(exc)
        try:
            text = _first_pages_text(path)
            size_bytes, sha256 = path.stat().st_size, _sha256(path)
        except (pdfium.PdfiumError, OSError) as exc:
            raise FolderError(f"{path.name}: not a readable PDF: {exc}") from exc
        has_text = bool(text.strip())
        in_text = None
        if has_text and publication is not None:
            in_text = _number_in_text(publication.number, text)
        files.append(
            SuppliedFile(
                name=str(path.relative_to(folder)),
                size_bytes=size_bytes,
                sha256=sha256,
                publication=number,
                problem=problem,
                has_text=has_text,
                number_in_text=in_text,
            )
        )
    return FolderScan(folder=str(folder), files=tuple(files))
