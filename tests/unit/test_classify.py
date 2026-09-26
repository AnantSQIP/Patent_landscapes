"""Phase 6 decision rules, similarity, judges' checks, evaluation maths and label files
(TEST-ONLY judgements and labels)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from patsquire_plr.classify.decide import (
    UsableJudgement,
    band,
    decide_relevance,
    decide_segment,
    usable,
)
from patsquire_plr.classify.evaluate import Counts, wilson
from patsquire_plr.classify.gold import LabelError, _parse
from patsquire_plr.classify.judges import (
    RelevanceJudgement,
    SegmentAssignment,
    SegmentJudgement,
    confident_enough,
    evidence_problem,
    unknown_segments,
)
from patsquire_plr.classify.texts import FamilyText
from patsquire_plr.classify.vectors import cosine, in_sample

HIGH, LOW = Decimal("0.5000"), Decimal("0.3500")


def _judgement(relevant: bool, confidence: str = "high") -> RelevanceJudgement:
    return RelevanceJudgement.model_validate(
        {"relevant": relevant, "confidence": confidence, "evidence": "e", "reason": "because"}
    )


# ------------------------------------------------------------------ similarity


def test_cosine_is_exact_to_four_places() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == Decimal("1.0000")
    assert cosine([1.0, 0.0], [0.0, 1.0]) == Decimal("0.0000")
    assert cosine([1.0, 1.0], [1.0, 0.0]) == Decimal("0.7071")
    assert cosine([3.0, 4.0], [4.0, 3.0]) == Decimal("0.9600")


@pytest.mark.parametrize(
    ("a", "b", "message"), [([1.0], [1.0, 2.0], "sizes"), ([0.0], [1.0], "zero")]
)
def test_cosine_refuses_bad_vectors(a: list[float], b: list[float], message: str) -> None:
    with pytest.raises(Exception, match=message):
        cosine(a, b)


def test_samples_are_fixed_and_about_the_right_size() -> None:
    keys = [f"family-{i}" for i in range(4000)]
    chosen = [k for k in keys if in_sample(7, k, Decimal("0.1000"))]
    assert chosen == [k for k in keys if in_sample(7, k, Decimal("0.1000"))]  # repeatable
    assert 330 < len(chosen) < 470  # about 10%
    assert chosen != [k for k in keys if in_sample(8, k, Decimal("0.1000"))]  # seed matters
    assert not any(in_sample(7, k, Decimal(0)) for k in keys)


def test_family_text_joins_title_and_abstract() -> None:
    family = FamilyText("f", "US1B1", "en", " Title ", "Abstract.")
    assert family.text == "Title\n\nAbstract."
    assert family.sha256 is not None
    assert FamilyText("f", None, None, None, " ").text is None
    assert FamilyText("f", None, None, None, None).sha256 is None


# ------------------------------------------------------------------ relevance rules


@pytest.mark.parametrize(
    ("score", "expected"),
    [(None, "no_text"), ("0.5000", "high"), ("0.4999", "middle"), ("0.3501", "middle"),
     ("0.3500", "low"), ("0.0100", "low")],
)  # fmt: skip
def test_bands_include_their_thresholds(score: str | None, expected: str) -> None:
    value = Decimal(score) if score is not None else None
    assert band(value, high=HIGH, low=LOW) == expected


def test_usable_judgements() -> None:
    assert usable(None, None, "medium").problem == "no judge answer"
    assert usable(None, "judge output invalid", "medium").problem == "judge output invalid"
    assert usable(_judgement(True), "evidence is not in", "medium").problem == "evidence is not in"
    assert usable(_judgement(True, "low"), None, "medium").problem == (
        "judge confidence low below medium"
    )
    assert usable(_judgement(True, "medium"), None, "medium").judgement is not None


@pytest.mark.parametrize(
    ("family_band", "judged", "final"),
    [
        ("no_text", None, "no_text"),
        ("high", None, "relevant"),
        ("low", None, "not_relevant"),
        ("high", UsableJudgement(_judgement(True), None), "relevant"),
        ("high", UsableJudgement(_judgement(False), None), "uncertain"),
        ("low", UsableJudgement(_judgement(True), None), "uncertain"),
        ("low", UsableJudgement(_judgement(False), None), "not_relevant"),
        ("high", UsableJudgement(None, "invalid"), "relevant"),  # the band stands
        ("middle", UsableJudgement(_judgement(True), None), "relevant"),
        ("middle", UsableJudgement(_judgement(False), None), "not_relevant"),
        ("middle", UsableJudgement(None, "invalid"), "uncertain"),
        ("middle", None, "uncertain"),
    ],
)
def test_relevance_decisions(family_band: str, judged: UsableJudgement | None, final: str) -> None:
    decided, reason = decide_relevance(family_band, Decimal("0.4200"), judged)  # type: ignore[arg-type]
    assert decided == final
    assert reason


@pytest.mark.parametrize(
    ("embedding", "judge", "final"),
    [
        (True, True, "assigned"),
        (False, False, "not_assigned"),
        (True, False, "uncertain"),
        (False, True, "uncertain"),
        (True, None, "uncertain"),
        (False, None, "not_assigned"),
    ],
)
def test_segment_decisions(embedding: bool, judge: bool | None, final: str) -> None:
    assert decide_segment(embedding, judge, None)[0] == final


# ------------------------------------------------------------------ judge checks


@pytest.mark.parametrize(
    ("evidence", "problem"),
    [
        ("large  LANGUAGE model", None),  # case and spacing do not matter
        ('"a large language model."', None),  # surrounding quotes and dot are ignored
        ("xy", "no evidence quoted"),
        ("a quantum computer", "evidence is not in the patent text"),
    ],
)
def test_evidence_must_be_quoted_from_the_text(evidence: str, problem: str | None) -> None:
    found = evidence_problem(evidence, "Training a large language model on text.")
    assert (found is None) if problem is None else (problem in (found or ""))


def test_confidence_order_and_unknown_segments() -> None:
    assert confident_enough("high", "medium")
    assert not confident_enough("low", "medium")
    judgement = SegmentJudgement(
        assignments=[SegmentAssignment(segment_id="a", evidence="x"),
                     SegmentAssignment(segment_id="zz", evidence="y")]
    )  # fmt: skip
    assert unknown_segments(judgement, {"a", "b"}) == ["zz"]


# ------------------------------------------------------------------ evaluation maths


def test_wilson_interval_matches_the_textbook_value() -> None:
    assert wilson(8, 10) == (Decimal("0.4902"), Decimal("0.9433"))
    assert wilson(0, 10) == (Decimal("0.0000"), Decimal("0.2775"))
    assert wilson(10, 10) == (Decimal("0.7225"), Decimal("1.0000"))
    assert wilson(0, 0) is None


def test_rates_come_from_counts() -> None:
    rates = Counts(tp=8, fp=2, fn=4, tn=86).rates()
    assert (rates["precision"], rates["recall"], rates["f1"]) == ("0.8000", "0.6667", "0.7273")
    assert rates["precision_ci95"] == ["0.4902", "0.9433"]
    empty = Counts(tp=0, fp=0, fn=0, tn=5).rates()
    assert (empty["precision"], empty["recall"], empty["f1"]) == (None, None, None)


# ------------------------------------------------------------------ label files


def _csv(*rows: str) -> bytes:
    return (
        "family_key,publication,title,abstract,relevant,segments,notes\n" + "\n".join(rows)
    ).encode()


def test_label_rows_become_relevance_and_segment_labels() -> None:
    labels, rows, blank = _parse(
        "﻿".encode() + _csv("f1,,,,y,a;b,", "f2,,,,n,,", "f3,,,,,,", "f4,,,,Yes,none,"),
        {"f1", "f2", "f3", "f4"},
        ["a", "b", "c"],
    )
    assert (rows, blank) == (4, 1)
    assert labels == [
        ("f1", "relevance", True),
        ("f1", "segment:a", True),
        ("f1", "segment:b", True),
        ("f1", "segment:c", False),
        ("f2", "relevance", False),
        ("f4", "relevance", True),
        ("f4", "segment:a", False),
        ("f4", "segment:b", False),
        ("f4", "segment:c", False),
    ]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("f9,,,,y,,", "not in this run"),
        ("f1,,,,maybe,,", "must be y or n"),
        ("f1,,,,n,a,", "not relevant"),
        ("f1,,,,y,zz,", "unknown segments"),
        ("f1,,,,,a,", "'relevant' is blank"),
    ],
)
def test_invalid_label_rows_reject_the_whole_file(row: str, message: str) -> None:
    with pytest.raises(LabelError, match=message):
        _parse(_csv("f2,,,,y,,", row), {"f1", "f2"}, ["a"])


def test_label_files_need_their_columns_and_some_labels() -> None:
    with pytest.raises(LabelError, match="lacks columns"):
        _parse(b"family_key,notes\nf1,x\n", {"f1"}, ["a"])
    with pytest.raises(LabelError, match="no labels"):
        _parse(_csv("f1,,,,,,"), {"f1"}, ["a"])
    with pytest.raises(LabelError, match="appears twice"):
        _parse(_csv("f1,,,,y,,", "f1,,,,n,,"), {"f1"}, ["a"])
