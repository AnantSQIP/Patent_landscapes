"""Build an analysis dataset: one copy per publication, families, normalised applicants.

This module is a pure, deterministic function of its inputs (``plan_dataset``), so the
result can be re-derived and property-tested; ``db/datasets.py`` persists it.

Rules (build prompt §5, §7):

* **Copies:** several documents for one publication (re-fetched, or from several sources)
  keep the most recently retrieved copy; the others are excluded as ``superseded_copy``.
  Every field on which the copies disagree is recorded as a conflict, never silently
  resolved.
* **Families** (simple families): union-find over three kinds of evidence:
  * ``stated_member``: a source lists the other publication as a family member;
  * ``family_id``: two documents carry the same source family ID;
  * ``application``: two publications of the same application.

  The family key is the smallest publication number in the dataset that belongs to the
  family. Stated members that were not retrieved are kept, marked as not in the dataset, so
  family size and office counts stay complete.
* **Applicants:** each raw name gets a key from the formatting rules, then an alias group if
  one exists. The display name is the alias group's canonical name, or else the most common
  raw spelling of that key in the dataset (ties broken alphabetically).
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from patsquire_plr.clean.names import AliasFile, normalize_name
from patsquire_plr.domain.patent import PatentDocument
from patsquire_plr.errors import PlrError

COPY_RULE = "latest_retrieval"
# Stated honestly: a transitive union of what sources state, which can be wider than a strict
# DOCDB simple family (e.g. Google lists continuations as family members).
FAMILY_DEFINITION = (
    "source_stated_family: transitive union of stated family members, equal source family "
    "IDs and shared applications"
)
Evidence = Literal["stated_member", "family_id", "application"]
# Fields that legitimately differ between copies and are not treated as conflicts.
_IDENTITY_FIELDS = frozenset({"raw_record_id", "source_id"})


class ConservationError(PlrError):
    """The dataset does not account for every input exactly once."""


@dataclass(frozen=True)
class InputDocument:
    document_id: uuid.UUID
    retrieved_at: datetime
    document: PatentDocument


@dataclass(frozen=True)
class DocumentDecision:
    document_id: uuid.UUID
    publication: str
    decision: Literal["selected", "excluded"]
    reason: str | None
    detail: str | None


@dataclass(frozen=True)
class Conflict:
    publication: str
    field: str
    values: dict[str, str]  # document_id -> value as JSON text


@dataclass(frozen=True)
class FamilyMember:
    family_key: str
    publication: str
    in_dataset: bool


@dataclass(frozen=True)
class Family:
    key: str
    evidence: tuple[Evidence, ...]
    publications_in_dataset: int
    stated_members_not_retrieved: int


@dataclass(frozen=True)
class Applicant:
    publication: str
    sequence: int
    name_raw: str
    name_key: str
    entity_name: str
    rule_steps: tuple[str, ...]
    alias_reason: str | None


@dataclass(frozen=True)
class DatasetPlan:
    decisions: tuple[DocumentDecision, ...]
    conflicts: tuple[Conflict, ...]
    families: tuple[Family, ...]
    members: tuple[FamilyMember, ...]
    applicants: tuple[Applicant, ...]
    selected: dict[str, InputDocument] = field(repr=False)
    family_warnings: tuple[str, ...] = ()


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, node: str) -> str:
        self.parent.setdefault(node, node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:  # path compression
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def application_key(raw: str, country: str) -> str:
    """Formatting-free application number, scoped by office ("US14/643,719" -> "US|14643719")."""
    compact = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    return f"{country}|{compact.removeprefix(country)}"


def plan_dataset(inputs: list[InputDocument], aliases: AliasFile) -> DatasetPlan:
    decisions, conflicts, selected = _choose_copies(inputs)
    families, members, family_warnings = _group_families(selected)
    applicants = _normalise_applicants(selected, aliases)
    plan = DatasetPlan(
        decisions=tuple(decisions),
        conflicts=tuple(conflicts),
        families=tuple(families),
        members=tuple(members),
        applicants=tuple(applicants),
        selected=selected,
        family_warnings=tuple(family_warnings),
    )
    check_conservation(inputs, plan)
    return plan


def _choose_copies(
    inputs: list[InputDocument],
) -> tuple[list[DocumentDecision], list[Conflict], dict[str, InputDocument]]:
    by_publication: dict[str, list[InputDocument]] = defaultdict(list)
    for item in inputs:
        by_publication[item.document.publication.text].append(item)
    decisions: list[DocumentDecision] = []
    conflicts: list[Conflict] = []
    selected: dict[str, InputDocument] = {}
    for publication in sorted(by_publication):
        copies = sorted(
            by_publication[publication], key=lambda c: (c.retrieved_at, str(c.document_id))
        )
        keep = copies[-1]
        selected[publication] = keep
        decisions.append(DocumentDecision(keep.document_id, publication, "selected", None, None))
        for other in copies[:-1]:
            decisions.append(
                DocumentDecision(
                    other.document_id,
                    publication,
                    "excluded",
                    "superseded_copy",
                    f"{COPY_RULE}: kept {keep.document_id} "
                    f"(retrieved {keep.retrieved_at.isoformat()})",
                )
            )
        conflicts += _conflicts(publication, copies)
    return decisions, conflicts, selected


def _conflicts(publication: str, copies: list[InputDocument]) -> list[Conflict]:
    if len(copies) < 2:  # noqa: PLR2004 - a conflict needs two copies
        return []
    dumps = {str(c.document_id): c.document.model_dump(mode="json") for c in copies}
    fields = sorted(set().union(*(d.keys() for d in dumps.values())) - _IDENTITY_FIELDS)
    found = []
    for name in fields:
        values = {doc_id: _json_text(d.get(name)) for doc_id, d in dumps.items()}
        if len(set(values.values())) > 1:
            found.append(Conflict(publication, name, values))
    return found


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _group_families(
    selected: dict[str, InputDocument],
) -> tuple[list[Family], list[FamilyMember], list[str]]:
    uf = _UnionFind()
    stated_edges: list[tuple[str, str]] = []
    attachments: dict[str, set[str]] = defaultdict(set)  # "#..." evidence node -> publications
    for publication, item in selected.items():
        doc = item.document
        uf.find(publication)
        for member in doc.family_members or ():
            uf.union(publication, member)
            stated_edges.append((publication, member))
        if doc.family_id_simple is not None:
            node = f"#family_id:{doc.source_id}:{doc.family_id_simple}"  # IDs are per source
            uf.union(publication, node)
            attachments[node].add(publication)
        if doc.application_number_raw is not None:
            node = "#application:" + application_key(doc.application_number_raw, doc.filing_office)
            uf.union(publication, node)
            attachments[node].add(publication)

    # Evidence is claimed only where it actually linked two publications.
    evidence: dict[str, set[Evidence]] = defaultdict(set)  # component root -> kinds
    for a, _ in stated_edges:
        evidence[uf.find(a)].add("stated_member")
    for node, publications in attachments.items():
        if len(publications) > 1:
            kind: Evidence = "family_id" if node.startswith("#family_id:") else "application"
            evidence[uf.find(node)].add(kind)

    components: dict[str, list[str]] = defaultdict(list)
    for node in list(uf.parent):
        if not node.startswith("#"):
            components[uf.find(node)].append(node)
    families: list[Family] = []
    members: list[FamilyMember] = []
    for root, nodes in components.items():
        in_dataset = sorted(n for n in nodes if n in selected)
        if (
            not in_dataset
        ):  # pragma: no cover - external nodes only exist attached to a dataset node
            continue
        key = in_dataset[0]
        external = sorted(n for n in nodes if n not in selected)
        families.append(
            Family(key, tuple(sorted(evidence.get(root, set()))), len(in_dataset), len(external))
        )
        members += [FamilyMember(key, p, True) for p in in_dataset]
        members += [FamilyMember(key, p, False) for p in external]
    families.sort(key=lambda f: f.key)
    members.sort(key=lambda m: (m.family_key, not m.in_dataset, m.publication))
    return families, members, _asymmetric_statements(selected)


def _asymmetric_statements(selected: dict[str, InputDocument]) -> list[str]:
    """A lists B, B was retrieved with a known member list, but B does not list A: one of the
    two statements may be wrong, and it may bridge two families. Reported, not resolved."""
    warnings = []
    for publication, item in sorted(selected.items()):
        for member in item.document.family_members or ():
            other = selected.get(member)
            if other is None or other.document.family_members is None:
                continue
            if publication not in other.document.family_members:
                warnings.append(
                    f"{publication} lists {member} as a family member, but {member} does not "
                    f"list {publication}"
                )
    return warnings


def _normalise_applicants(
    selected: dict[str, InputDocument], aliases: AliasFile
) -> list[Applicant]:
    alias_map = aliases.canonical_for()
    rows: list[tuple[str, int, str, str, tuple[str, ...]]] = []
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for publication, item in selected.items():
        for party in item.document.applicants or ():
            name = normalize_name(party.name_raw)
            rows.append((publication, party.sequence, party.name_raw, name.key, name.steps))
            spellings[name.key][party.name_raw.strip()] += 1
    display: dict[str, str] = {
        key: sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for key, counts in spellings.items()
    }
    applicants = []
    for publication, sequence, raw, key, steps in rows:
        alias = alias_map.get(key)
        applicants.append(
            Applicant(
                publication=publication,
                sequence=sequence,
                name_raw=raw,
                name_key=key,
                entity_name=alias[0] if alias else display[key],
                rule_steps=steps,
                alias_reason=alias[1] if alias else None,
            )
        )
    return applicants


def check_conservation(inputs: list[InputDocument], plan: DatasetPlan) -> None:
    """Layer 2: every input and every selected publication is accounted for exactly once."""
    problems = []
    decided = Counter(d.document_id for d in plan.decisions)
    if set(decided) != {i.document_id for i in inputs} or any(n != 1 for n in decided.values()):
        problems.append("every input document must have exactly one decision")
    selected = [d.publication for d in plan.decisions if d.decision == "selected"]
    if len(selected) != len(set(selected)) or set(selected) != set(plan.selected):
        problems.append("each publication must be selected exactly once")
    placed = Counter(m.publication for m in plan.members if m.in_dataset)
    if set(placed) != set(plan.selected) or any(n != 1 for n in placed.values()):
        problems.append("each selected publication must belong to exactly one family")
    externals = [m.publication for m in plan.members if not m.in_dataset]
    if len(externals) != len(set(externals)) or set(externals) & set(plan.selected):
        problems.append("stated members outside the dataset must appear once, in one family")
    problems += _family_count_problems(plan)
    expected = Counter(
        (publication, party.sequence)
        for publication, item in plan.selected.items()
        for party in item.document.applicants or ()
    )
    actual = Counter((a.publication, a.sequence) for a in plan.applicants)
    if actual != expected:
        problems.append("applicant rows do not match the selected documents' applicants")
    if problems:
        raise ConservationError("; ".join(problems))


def _family_count_problems(plan: DatasetPlan) -> list[str]:
    problems = []
    keys = [f.key for f in plan.families]
    if len(keys) != len(set(keys)) or set(keys) != {m.family_key for m in plan.members}:
        problems.append("families and family members must name the same family keys, once each")
    inside = Counter(m.family_key for m in plan.members if m.in_dataset)
    outside = Counter(m.family_key for m in plan.members if not m.in_dataset)
    for family in plan.families:
        counts = (family.publications_in_dataset, family.stated_members_not_retrieved)
        if counts != (inside[family.key], outside[family.key]):
            problems.append(f"family {family.key}: stored counts disagree with its members")
    if sum(f.publications_in_dataset for f in plan.families) != len(plan.selected):
        problems.append("family sizes do not add up to the selected publications")
    return problems
