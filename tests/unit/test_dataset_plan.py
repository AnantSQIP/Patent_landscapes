"""Dataset planning: copies, families, applicants and conservation (TEST-ONLY documents)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from patsquire_plr.clean.dataset import (
    ConservationError,
    DatasetPlan,
    InputDocument,
    application_key,
    check_conservation,
    plan_dataset,
)
from patsquire_plr.clean.names import AliasFile, AliasGroup
from patsquire_plr.domain.patent import (
    CANONICAL_OPTIONAL_FIELDS,
    MissingReason,
    Party,
    PatentDocument,
    normalize_publication_number,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
NO_ALIASES = AliasFile(version=1, groups=())


def _doc(
    number: str,
    *,
    members: tuple[str, ...] | None = None,
    application: str | None = None,
    family_id: str | None = None,
    applicants: tuple[str, ...] = (),
    title: str = "A title",
) -> PatentDocument:
    values: dict[str, object] = dict.fromkeys(CANONICAL_OPTIONAL_FIELDS)
    values.update(
        family_members=members,
        application_number_raw=application,
        family_id_simple=family_id,
        title=title,
        applicants=tuple(
            Party(
                role="applicant",
                sequence=i,
                name_raw=n,
                country=None,
                country_missing=MissingReason.NOT_PROVIDED_BY_SOURCE,
            )
            for i, n in enumerate(applicants, 1)
        )
        or None,
    )
    missing = {k: MissingReason.NOT_PROVIDED_BY_SOURCE for k, v in values.items() if v is None}
    return PatentDocument(
        raw_record_id=uuid.uuid4(),
        source_id="test",
        publication=normalize_publication_number(number),
        publication_number_raw=number,
        missing=missing,  # type: ignore[arg-type]
        **values,  # type: ignore[arg-type]
    )


def _in(doc: PatentDocument, minutes: int = 0) -> InputDocument:
    return InputDocument(
        document_id=uuid.uuid4(), retrieved_at=T0 + timedelta(minutes=minutes), document=doc
    )


# ---------------------------------------------------------------- copies


def test_latest_copy_wins_and_differences_are_recorded() -> None:
    old = _in(_doc("US1B1", title="Old title"), minutes=0)
    new = _in(_doc("US1B1", title="New title"), minutes=5)

    plan = plan_dataset([old, new], NO_ALIASES)

    decisions = {d.document_id: d for d in plan.decisions}
    assert decisions[new.document_id].decision == "selected"
    assert decisions[old.document_id].reason == "superseded_copy"
    [conflict] = plan.conflicts
    assert conflict.field == "title"
    assert set(conflict.values.values()) == {'"Old title"', '"New title"'}


def test_identical_copies_record_no_conflict() -> None:
    plan = plan_dataset([_in(_doc("US1B1")), _in(_doc("US1B1"), 1)], NO_ALIASES)
    assert plan.conflicts == ()


# ---------------------------------------------------------------- families


def test_stated_members_join_publications_and_keep_unretrieved_members() -> None:
    a1 = _in(_doc("US20160266243A1", members=("US10000000B2", "EP3268771A1")))
    b2 = _in(_doc("US10000000B2", members=("US20160266243A1",)))
    other = _in(_doc("CN112345678A"))

    plan = plan_dataset([a1, b2, other], NO_ALIASES)

    families = {f.key: f for f in plan.families}
    assert set(families) == {"CN112345678A", "US10000000B2"}  # key: smallest number in the dataset
    joined = families["US10000000B2"]
    assert (joined.publications_in_dataset, joined.stated_members_not_retrieved) == (2, 1)
    assert joined.evidence == ("stated_member",)
    external = [m for m in plan.members if not m.in_dataset]
    assert [(m.family_key, m.publication) for m in external] == [("US10000000B2", "EP3268771A1")]
    assert families["CN112345678A"].evidence == ()


def test_same_application_joins_publications() -> None:
    plan = plan_dataset(
        [
            _in(_doc("US20160266243A1", application="US14/643,719")),
            _in(_doc("US10000000B2", application="US 14643719")),
        ],
        NO_ALIASES,
    )
    [family] = plan.families
    assert family.evidence == ("application",)
    assert (
        application_key("US14/643,719", "US")
        == application_key("US 14643719", "US")
        == "US|14643719"
    )


def test_same_source_family_id_joins_publications() -> None:
    plan = plan_dataset(
        [_in(_doc("EP1A1", family_id="54321")), _in(_doc("JP2A", family_id="54321"))], NO_ALIASES
    )
    [family] = plan.families
    assert family.evidence == ("family_id",)


def test_application_numbers_are_scoped_by_office() -> None:
    plan = plan_dataset(
        [_in(_doc("US1B1", application="12345")), _in(_doc("EP2A1", application="12345"))],
        NO_ALIASES,
    )
    assert len(plan.families) == 2


# ---------------------------------------------------------------- applicants


def test_applicant_display_names_and_aliases() -> None:
    aliases = AliasFile(
        version=1,
        groups=(
            AliasGroup(
                canonical="Alphabet", variants=("Google LLC",), reason="subsidiary, owner decision"
            ),
        ),
    )
    plan = plan_dataset(
        [
            _in(_doc("US1B1", applicants=("Raytheon Co", "Google Inc."))),
            _in(_doc("US2B1", applicants=("RAYTHEON COMPANY",))),
            _in(_doc("US3B1", applicants=("Raytheon Co",))),
        ],
        aliases,
    )
    by_raw = {a.name_raw: a for a in plan.applicants}
    assert by_raw["RAYTHEON COMPANY"].entity_name == "Raytheon Co"  # most common spelling
    assert by_raw["RAYTHEON COMPANY"].name_key == "raytheon"
    assert by_raw["Google Inc."].entity_name == "Alphabet"
    assert by_raw["Google Inc."].alias_reason == "subsidiary, owner decision"


# ---------------------------------------------------------------- conservation


def test_conservation_detects_a_lost_input() -> None:
    inputs = [_in(_doc("US1B1")), _in(_doc("US2B1"))]
    plan = plan_dataset(inputs, NO_ALIASES)
    broken = DatasetPlan(
        decisions=plan.decisions[:1],
        conflicts=plan.conflicts,
        families=plan.families,
        members=plan.members,
        applicants=plan.applicants,
        selected=plan.selected,
    )
    with pytest.raises(ConservationError, match="exactly one decision"):
        check_conservation(inputs, broken)


numbers = st.sampled_from([f"US{n}B1" for n in range(1, 13)])


@st.composite
def documents(draw: st.DrawFn) -> list[InputDocument]:
    items = []
    for minute in range(draw(st.integers(min_value=1, max_value=15))):
        number = draw(numbers)
        members = tuple(sorted(set(draw(st.lists(numbers, max_size=3))) - {number})) or None
        application = draw(st.sampled_from([None, "A1", "A2", "A3"]))
        items.append(_in(_doc(number, members=members, application=application), minutes=minute))
    return items


@given(documents())
@settings(max_examples=150)
def test_every_input_is_decided_once_and_every_publication_is_in_one_family(
    inputs: list[InputDocument],
) -> None:
    plan = plan_dataset(inputs, NO_ALIASES)  # raises ConservationError if not
    assert len(plan.decisions) == len(inputs)
    assert {m.publication for m in plan.members if m.in_dataset} == set(plan.selected)
    assert sum(f.publications_in_dataset for f in plan.families) == len(plan.selected)
    # replanning is deterministic
    again = plan_dataset(inputs, NO_ALIASES)
    assert (again.families, again.members) == (plan.families, plan.members)
