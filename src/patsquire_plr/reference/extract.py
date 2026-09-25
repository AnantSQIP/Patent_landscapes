"""Structure extraction from reference Patent Landscape Report PDFs (build prompt Phase 1).

For each PDF this records:

* headings: taken from the PDF outline (bookmarks) when there is one, otherwise found by
  font size (lines set clearly larger than the body text);
* figure / table / box captions: lines that start with a numbered label such as
  ``Figure 3.2`` or ``Table A1``;
* pages without any extractable text (e.g. full-page images), so gaps are visible.

The output describes *structure* only, to help analysts study how professional PLRs are
organised. It holds short excerpts of third-party copyrighted text (headings, caption
lines), so it is written to a git-ignored directory and must never be copied into
templates or generated reports (build prompt principle 9).

No OCR is attempted. A PDF with no extractable text at all is an error, not an empty
result.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal

import pdfplumber
import pypdfium2 as pdfium
from pdfplumber.pdf import PDF
from pydantic import BaseModel, ConfigDict, Field

from patsquire_plr.errors import PlrError

EXTRACTOR_VERSION = "1"

# A heading must be at least this much larger than the body text size.
HEADING_SIZE_RATIO = 1.25
MAX_HEADING_LEVELS = 3
MAX_HEADING_CHARS = 120

# Label separators seen in reports: space, ".", ":", hyphen, en dash (U+2013), em dash (U+2014).
_CAPTION = re.compile(
    r"^(?P<kind>Figure|Fig\.|Table|Box)\s+(?P<number>[A-Z]?\d+(?:\.\d+)*[a-z]?)\b"
    r"[\s.:\u2013\u2014-]*(?P<rest>.*)$"
)
CaptionKind = Literal["figure", "table", "box"]
_KIND: dict[str, CaptionKind] = {
    "Figure": "figure",
    "Fig.": "figure",
    "Table": "table",
    "Box": "box",
}
_HAS_LETTERS = re.compile(r"[A-Za-z]{2,}")


class ExtractionError(PlrError):
    """A reference PDF could not be processed."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Heading(_Model):
    level: int = Field(ge=0)
    title: str = Field(min_length=1)
    page: int = Field(ge=1, description="1-based page number")
    source: Literal["bookmark", "font_size"]


class Caption(_Model):
    kind: CaptionKind
    label: str = Field(min_length=1, description='e.g. "Figure 3.2"')
    text: str = Field(description="the caption line after the label")
    page: int = Field(ge=1)


class ReferenceExtract(_Model):
    file_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    extractor_version: str
    heading_source: Literal["bookmark", "font_size"]
    headings: tuple[Heading, ...]
    captions: tuple[Caption, ...]
    pages_without_text: tuple[int, ...]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.split())


def bookmark_headings(pdf_path: Path) -> list[Heading]:
    """Headings from the PDF outline. Bookmarks without a resolvable page are an error."""
    document = pdfium.PdfDocument(pdf_path)
    try:
        headings: list[Heading] = []
        for bookmark in document.get_toc():
            title = _clean(bookmark.get_title() or "")
            if not title:
                continue
            destination = bookmark.get_dest()
            page_index = None if destination is None else destination.get_index()
            if page_index is None:
                raise ExtractionError(f"{pdf_path.name}: bookmark '{title}' has no target page")
            headings.append(
                Heading(level=bookmark.level, title=title, page=page_index + 1, source="bookmark")
            )
        return headings
    finally:
        document.close()


class _Line(_Model):
    page: int
    text: str
    size: float


def _lines(pdf: PDF) -> Iterator[_Line]:
    for number, page in enumerate(pdf.pages, start=1):
        for line in page.extract_text_lines(return_chars=True):
            chars = line["chars"]
            text = _clean(line["text"])
            if not chars or not text:
                continue
            size = round(sum(float(c["size"]) for c in chars) / len(chars), 1)
            yield _Line(page=number, text=text, size=size)


def body_text_size(lines: Iterable[_Line]) -> float:
    """The font size carrying the most characters, i.e. the body text size."""
    weights: Counter[float] = Counter()
    for line in lines:
        weights[line.size] += len(line.text)
    if not weights:
        raise ExtractionError("no text lines to measure")
    return weights.most_common(1)[0][0]


def font_size_headings(lines: list[_Line]) -> list[Heading]:
    """Headings found by size: lines at least ``HEADING_SIZE_RATIO`` x the body size.

    The largest distinct heading size is level 0, and so on down to ``MAX_HEADING_LEVELS``;
    smaller heading sizes all share the deepest level.
    """
    threshold = body_text_size(lines) * HEADING_SIZE_RATIO
    candidates = [
        line
        for line in lines
        if line.size >= threshold
        and len(line.text) <= MAX_HEADING_CHARS
        and _HAS_LETTERS.search(line.text)
    ]
    sizes = sorted({line.size for line in candidates}, reverse=True)
    level_of = {size: min(rank, MAX_HEADING_LEVELS - 1) for rank, size in enumerate(sizes)}
    return [
        Heading(level=level_of[line.size], title=line.text, page=line.page, source="font_size")
        for line in candidates
    ]


def captions_in(lines: Iterable[_Line]) -> list[Caption]:
    captions: list[Caption] = []
    for line in lines:
        match = _CAPTION.match(line.text)
        if match is None:
            continue
        kind = _KIND[match["kind"]]
        label = f"{kind.capitalize()} {match['number']}"
        captions.append(Caption(kind=kind, label=label, text=match["rest"], page=line.page))
    return captions


def extract_reference(pdf_path: Path) -> ReferenceExtract:
    if not pdf_path.is_file():
        raise ExtractionError(f"PDF not found: {pdf_path}")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            lines = list(_lines(pdf))
            pages_with_text = {line.page for line in lines}
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"{pdf_path.name}: cannot be read as a PDF: {exc}") from exc

    if not lines:
        raise ExtractionError(
            f"{pdf_path.name}: no extractable text on any of {page_count} pages "
            "(scanned document?). OCR is out of scope; supply a text-based PDF."
        )

    headings = bookmark_headings(pdf_path)
    heading_source: Literal["bookmark", "font_size"] = "bookmark"
    if not headings:
        headings = font_size_headings(lines)
        heading_source = "font_size"

    return ReferenceExtract(
        file_name=pdf_path.name,
        sha256=sha256_of(pdf_path),
        page_count=page_count,
        extractor_version=EXTRACTOR_VERSION,
        heading_source=heading_source,
        headings=tuple(headings),
        captions=tuple(captions_in(lines)),
        pages_without_text=tuple(p for p in range(1, page_count + 1) if p not in pages_with_text),
    )


def extract_directory(pdf_dir: Path, out_dir: Path) -> list[Path]:
    """Extract every PDF under ``pdf_dir`` (recursive, any case of ``.pdf``) to
    ``<out_dir>/<stem>.json``. Anything already inside ``out_dir`` is skipped, and two PDFs
    with the same file stem are an error rather than one silently overwriting the other.
    """
    resolved_out = out_dir.resolve()
    pdfs = sorted(
        p
        for p in pdf_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() == ".pdf"
        and not p.resolve().is_relative_to(resolved_out)
    )
    if not pdfs:
        raise ExtractionError(f"No PDF files found under {pdf_dir}")
    stems = Counter(p.stem for p in pdfs)
    if clashes := sorted(stem for stem, n in stems.items() if n > 1):
        raise ExtractionError(f"Several PDFs share a file name stem, output would clash: {clashes}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for pdf_path in pdfs:
        target = out_dir / f"{pdf_path.stem}.json"
        target.write_text(extract_reference(pdf_path).model_dump_json(indent=2), encoding="utf-8")
        written.append(target)
    return written


def render_pages(
    pdf_path: Path, pages: Iterable[int], out_dir: Path, *, scale: float = 1.5
) -> list[Path]:
    """Render 1-based ``pages`` to PNG, for inspecting charts that text extraction misses."""
    if not pdf_path.is_file():
        raise ExtractionError(f"PDF not found: {pdf_path}")
    document = pdfium.PdfDocument(pdf_path)
    try:
        written: list[Path] = []
        out_dir.mkdir(parents=True, exist_ok=True)
        for page_number in pages:
            if not 1 <= page_number <= len(document):
                raise ExtractionError(
                    f"{pdf_path.name}: page {page_number} out of range 1..{len(document)}"
                )
            image = document[page_number - 1].render(scale=scale).to_pil()
            target = out_dir / f"{pdf_path.stem}-p{page_number:03d}.png"
            image.save(target)
            written.append(target)
        return written
    finally:
        document.close()


SHINGLE_WORDS = 8
# Headings shorter than this ("Introduction", "Top applicants") are generic, not authorship.
MIN_HEADING_WORDS = 4
_WORD = re.compile(r"[a-z0-9]+")


def _shingles(text: str, size: int) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def copied_phrases(
    text: str, extracts: Iterable[ReferenceExtract], *, size: int = SHINGLE_WORDS
) -> list[tuple[str, str]]:
    """Phrases ``text`` shares with the reference extracts, as ``(file_name, phrase)`` pairs.

    Two checks are made. Any run of ``size`` consecutive words shared with a heading or
    caption is reported, and so is any whole heading of ``MIN_HEADING_WORDS`` or more words
    that appears in ``text``. Used to check principle 9 (no copied text). Only headings and
    captions are compared, since those are all the extracts hold, so an empty result is
    evidence, not proof, that no text was copied.
    """
    ours = _shingles(text, size)
    our_text = " " + " ".join(_WORD.findall(text.lower())) + " "
    found: set[tuple[str, str]] = set()
    for extract in extracts:
        # Headings are usually shorter than a shingle, so also match them whole. Cover-page
        # headings (page 1) are the report's title, which we quote on purpose to cite it.
        for heading in extract.headings:
            if heading.page == 1:
                continue
            words = _WORD.findall(heading.title.lower())
            if len(words) >= MIN_HEADING_WORDS and f" {' '.join(words)} " in our_text:
                found.add((extract.file_name, " ".join(words)))
        pieces = [h.title for h in extract.headings] + [
            f"{c.label} {c.text}" for c in extract.captions
        ]
        for piece in pieces:
            for shingle in _shingles(piece, size) & ours:
                found.add((extract.file_name, " ".join(shingle)))
    return sorted(found)
