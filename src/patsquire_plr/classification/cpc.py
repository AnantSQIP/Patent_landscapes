"""The official CPC scheme, from the EPO/USPTO "CPC Title List" bulk file (ADR 0010).

Source: https://www.cooperativepatentclassification.org/cpcSchemeAndDefinitions/bulk, file
``CPCTitleList<YYYYMM>.zip``. It holds one text file per section (``cpc-section-G_20260801.txt``);
each line is ``SYMBOL<TAB>LEVEL<TAB>TITLE``:

* ``LEVEL`` is empty for sections (``G``), classes (``G06``) and subclasses (``G06N``);
* ``0`` for a main group (``G06N3/00``), and the dot level ``1``, ``2``, ... for subgroups;
* titles in braces (``{Details}``) are CPC-only entries without an IPC counterpart;
* a subgroup's title continues its parent's (``G06N3/044`` "Recurrent networks" sits under
  "Architecture, e.g. interconnection topology"), so the full meaning is the title path.

A subgroup's parent is the nearest preceding group of the same subclass one dot level up; the
file lists groups in hierarchy order, so this is computed exactly while reading. Anything
unexpected in the file is an error, never skipped.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from patsquire_plr.domain.patent import NormalizationError, normalize_classification_code
from patsquire_plr.errors import PlrError

EntryKind = Literal["section", "class", "subclass", "main_group", "subgroup"]

_SECTION_FILE = re.compile(r"^cpc-section-(?P<section>[A-HY])_(?P<date>\d{8})\.txt$")
_SECTION = re.compile(r"^[A-HY]$")
_CLASS = re.compile(r"^[A-HY]\d{2}$")
_SUBCLASS = re.compile(r"^[A-HY]\d{2}[A-Z]$")
_WORD = re.compile(r"[a-z0-9]+")
_REFERENCE = re.compile(r"\([^()]*\)")


class CpcSchemeError(PlrError):
    """The CPC title list cannot be read exactly as documented."""


@dataclass(frozen=True, slots=True)
class CpcEntry:
    symbol: str
    kind: EntryKind
    dot_level: int | None  # main group 0, subgroups 1..n; None above group level
    title: str  # verbatim, including CPC-only braces
    parent: str | None

    @property
    def plain_title(self) -> str:
        return self.title.replace("{", "").replace("}", "")


@dataclass(frozen=True, slots=True)
class CpcMatch:
    symbol: str
    title_path: str
    matched_terms: tuple[str, ...]
    score: int


@dataclass(frozen=True, slots=True)
class CpcCheck:
    """The result of checking one suggested code against the scheme."""

    code_raw: str
    symbol: str | None  # normalised symbol when the code exists in the scheme
    problem: str | None  # why it was rejected
    title_path: str | None


def normalize_cpc_symbol(raw: str) -> str:
    """A section, class, subclass or full group symbol, e.g. ``g06n`` -> ``G06N``,
    ``G06N 3/0455`` -> ``G06N3/0455``."""
    compact = re.sub(r"\s+", "", raw.strip().upper())
    if _SECTION.fullmatch(compact) or _CLASS.fullmatch(compact) or _SUBCLASS.fullmatch(compact):
        return compact
    return normalize_classification_code(compact)


class CpcScheme:
    """One version of the scheme, held in memory (about 254,000 entries)."""

    def __init__(self, version: str, entries: Sequence[CpcEntry]) -> None:
        self.version = version
        self._entries = {e.symbol: e for e in entries}
        if len(self._entries) != len(entries):
            raise CpcSchemeError("the title list repeats a symbol")
        orphans = [
            e.symbol for e in entries if e.parent is not None and e.parent not in self._entries
        ]
        if orphans:
            raise CpcSchemeError(f"entries whose parent is not in the list: {orphans[:10]}")
        self._children: dict[str, list[str]] = {}
        for entry in entries:
            if entry.parent is not None:
                self._children.setdefault(entry.parent, []).append(entry.symbol)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, symbol: str) -> CpcEntry | None:
        return self._entries.get(symbol)

    def path(self, symbol: str) -> list[CpcEntry]:
        """The entry and its ancestors, from the section down."""
        chain = []
        entry = self._entries.get(symbol)
        while entry is not None:
            chain.append(entry)
            entry = self._entries.get(entry.parent) if entry.parent else None
        return chain[::-1]

    def title_path(self, symbol: str) -> str:
        """Titles from the main group (or the entry itself above group level) down, which is
        how a subgroup's title reads in full."""
        chain = self.path(symbol)
        groups = [e for e in chain if e.dot_level is not None] or chain[-1:]
        return " > ".join(e.plain_title for e in groups)

    def child_count(self, symbol: str) -> int:
        return len(self._children.get(symbol, ()))

    def descendants(self, symbol: str) -> list[str]:
        """Every entry below ``symbol``, in scheme order (not including ``symbol``)."""
        found: list[str] = []
        stack = list(reversed(self._children.get(symbol, ())))
        while stack:
            current = stack.pop()
            found.append(current)
            stack.extend(reversed(self._children.get(current, ())))
        return found

    def is_within(self, code: str, symbol: str) -> bool | None:
        """Whether ``code`` is ``symbol`` or below it; None if ``code`` is not in the scheme."""
        if code not in self._entries:
            return None
        return any(e.symbol == symbol for e in self.path(code))

    def check(self, code_raw: str) -> CpcCheck:
        try:
            symbol = normalize_cpc_symbol(code_raw)
        except NormalizationError as exc:
            return CpcCheck(code_raw=code_raw, symbol=None, problem=str(exc), title_path=None)
        if symbol not in self._entries:
            return CpcCheck(
                code_raw=code_raw,
                symbol=None,
                problem=f"{symbol} is not in CPC version {self.version}",
                title_path=None,
            )
        return CpcCheck(
            code_raw=code_raw, symbol=symbol, problem=None, title_path=self.title_path(symbol)
        )

    def search(
        self, terms: Iterable[str], *, within: Sequence[str] = (), limit: int = 20
    ) -> list[CpcMatch]:
        """Group entries whose own title contains a term as whole words, case-insensitively.

        Parenthesised references to other places ("(speech recognition G10L)") are ignored,
        since they say where a subject is *not* classified.

        Words match whole, case-insensitively, and a plural matches its singular when the
        singular has at least four letters ("networks" finds "network"; "news" does not find
        "new").

        Ranking: an entry scores the number of words in the distinct terms found along its
        title path, so a matched phrase counts for more than a single word. Ties go to the
        lower symbol, so the order is deterministic. ``within`` limits the search to symbols
        starting with those prefixes (e.g. subclasses ``["G06F", "G06N"]``).
        """
        phrases = {t: _words(t) for t in terms}
        phrases = {t: w for t, w in phrases.items() if w}
        scope = [
            (symbol, entry)
            for symbol, entry in self._entries.items()
            if entry.dot_level is not None and (not within or symbol.startswith(tuple(within)))
        ]
        matches: list[CpcMatch] = []
        for symbol, entry in scope:
            own = _words(entry.plain_title)
            if not any(_contains(own, w) for w in phrases.values()):
                continue
            path_words = tuple(
                w
                for e in self.path(symbol)
                if e.dot_level is not None
                for w in _words(e.plain_title)
            )
            matched = tuple(t for t, w in phrases.items() if _contains(path_words, w))
            score = sum(len(phrases[t]) for t in matched)
            matches.append(CpcMatch(symbol, self.title_path(symbol), matched, score))
        matches.sort(key=lambda m: (-m.score, m.symbol))
        return matches[:limit]


def _words(text: str) -> tuple[str, ...]:
    """Lower-case words, without parenthesised references."""
    previous = None
    while previous != text:
        previous, text = text, _REFERENCE.sub(" ", text)
    return tuple(_WORD.findall(text.lower()))


MIN_PLURAL_BASE = 4  # "news" is not the plural of "new"


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= MIN_PLURAL_BASE and long_ in (short + "s", short + "es")


def _contains(words: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(
        all(_same_word(w, p) for w, p in zip(words[i : i + n], phrase, strict=True))
        for i in range(len(words) - n + 1)
    )


INDEXING_OFFSET = 2000  # CPC "2000-series" indexing codes mirror main group N as 2000 + N


def _same_family(parent: str, child: str) -> bool:
    """A subgroup belongs to its parent's main group, or is a 2000-series indexing code
    placed under it (the file lists e.g. A01C2001/048 under A01C1/04)."""
    (p_sub, p_group), (c_sub, c_group) = _main_group(parent), _main_group(child)
    return p_sub == c_sub and c_group in (p_group, p_group + INDEXING_OFFSET)


def _main_group(symbol: str) -> tuple[str, int]:
    head = symbol.split("/", maxsplit=1)[0]
    return head[:4], int(head[4:])


def parse_title_list(content: bytes) -> CpcScheme:
    """Read ``CPCTitleList<YYYYMM>.zip``. The version is ``YYYY.MM`` from the file dates."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise CpcSchemeError(f"not a zip file: {exc}") from exc
    names = sorted(archive.namelist())
    matches = [_SECTION_FILE.fullmatch(n) for n in names]
    if not names or any(m is None for m in matches):
        raise CpcSchemeError(f"unexpected files in the title list: {names}")
    dates = {m["date"] for m in matches if m is not None}
    if len(dates) != 1:
        raise CpcSchemeError(f"section files have different dates: {sorted(dates)}")
    [stamp] = dates
    entries: list[CpcEntry] = []
    for name in names:
        entries += _parse_section(name, archive.read(name).decode("utf-8"))
    return CpcScheme(f"{stamp[:4]}.{stamp[4:6]}", entries)


def _parse_section(name: str, text: str) -> list[CpcEntry]:
    entries: list[CpcEntry] = []
    stack: list[CpcEntry] = []  # open groups of the current subclass, by dot level
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0] or not parts[2]:  # noqa: PLR2004
            raise CpcSchemeError(f"{name}:{number}: expected SYMBOL<TAB>LEVEL<TAB>TITLE")
        symbol, level, title = parts
        entry = _entry(f"{name}:{number}: {symbol}", symbol, level, title, stack)
        if entry.dot_level is not None:
            del stack[entry.dot_level :]
            stack.append(entry)
        else:
            stack.clear()
        entries.append(entry)
    return entries


def _entry(where: str, symbol: str, level: str, title: str, stack: list[CpcEntry]) -> CpcEntry:
    if not level:
        for kind, pattern, parent in (
            ("section", _SECTION, None),
            ("class", _CLASS, symbol[:1]),
            ("subclass", _SUBCLASS, symbol[:3]),
        ):
            if pattern.fullmatch(symbol):
                return CpcEntry(symbol, kind, None, title, parent)  # type: ignore[arg-type]
        raise CpcSchemeError(f"{where}: no level but not a section, class or subclass")
    if not level.isdigit():
        raise CpcSchemeError(f"{where}: level {level!r} is not a number")
    try:
        if normalize_classification_code(symbol) != symbol:
            raise CpcSchemeError(f"{where}: symbol is not in normalised form")
    except NormalizationError as exc:
        raise CpcSchemeError(f"{where}: {exc}") from exc
    dot_level = int(level)
    if dot_level == 0:
        return CpcEntry(symbol, "main_group", 0, title, symbol[:4])
    if len(stack) < dot_level or not _same_family(stack[dot_level - 1].symbol, symbol):
        raise CpcSchemeError(f"{where}: no parent group at dot level {dot_level - 1}")
    return CpcEntry(symbol, "subgroup", dot_level, title, stack[dot_level - 1].symbol)
