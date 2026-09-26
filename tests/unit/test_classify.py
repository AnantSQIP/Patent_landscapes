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
from patsquire_plr.classify.evaluate import Counts, _problems, wilson
from patsquire_plr.classify.gold import LabelError, _parse, _read_rows
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
from patsquire_plr.config import EvaluationSettings

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
        (False, None, "uncertain"),  # one method alone cannot rule a segment out
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
        ("xy", "shorter than 15 characters"),
        ("language", "shorter than 15 characters"),  # a fragment proves nothing
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


# ------------------------------------------------------------------ publishable rule


def _settings(**overrides: object) -> EvaluationSettings:
    values: dict[str, object] = {
        "min_gold_labels": 10,
        "min_gold_positives": 5,
        "min_segment_labels": 1,
        "min_precision": "0.8000",
        "min_recall": "0.8000",
        "min_segment_f1": "0.7000",
        "gate_on": "point",
        **overrides,
    }
    return EvaluationSettings.model_validate(values)


def test_gates_compare_unrounded_values() -> None:
    almost = Counts(tp=79996, fp=20004, fn=0, tn=0)  # precision 0.79996 rounds to 0.8000
    assert almost.rates()["precision"] == "0.8000"
    assert not almost.meets("precision", Decimal("0.8000"), "point")
    assert Counts(tp=8, fp=2, fn=0, tn=0).meets("precision", Decimal("0.8000"), "point")
    # The 95% lower bound of 8/10 is 0.4902: far below 0.80 with such a small sample.
    assert not Counts(tp=8, fp=2, fn=0, tn=0).meets("precision", Decimal("0.8"), "lower_bound")
    assert Counts(tp=950, fp=50, fn=0, tn=0).meets("precision", Decimal("0.9"), "lower_bound")
    assert not Counts(tp=0, fp=0, fn=0, tn=5).meets("recall", Decimal("0.1"), "point")


def test_publishable_rule() -> None:
    good = Counts(tp=9, fp=1, fn=1, tn=9)
    assert _problems(good, segments={"segment:a": Counts(9, 1, 1, 0)}, segment_families=9,
                     settings=_settings(), unresolved=[], conflicts=[]) == []  # fmt: skip
    problems = _problems(
        Counts(tp=2, fp=0, fn=0, tn=3),
        segments={"segment:a": Counts(tp=0, fp=30, fn=0, tn=0), "segment:b": Counts(0, 0, 0, 5)},
        segment_families=0,
        settings=_settings(),
        unresolved=["f1"],
        conflicts=[("f2", "relevance")],
    )
    assert problems == [
        "only 5 sampled relevance labels; at least 10 are needed",
        "only 2 sampled families labelled relevant; at least 5 are needed",
        "only 0 sampled relevant families have segment labels; at least 1 are needed",
        "segment:a F1 0.0000 is below 0.7000",  # wrong assignments count even with no positives
        "1 labels differ between labellers; reconcile them",
        "1 families still need a person's review",
    ]
    strict = _problems(
        good,
        segments={},
        segment_families=9,
        settings=_settings(gate_on="lower_bound"),
        unresolved=[],
        conflicts=[],
    )
    assert strict == [
        "relevance precision (95% lower bound) is below 0.8000",
        "relevance recall (95% lower bound) is below 0.8000",
    ]


# ------------------------------------------------------------------ label files


def _csv(*rows: str, delimiter: str = ",") -> bytes:
    header = delimiter.join(("export_id", "family_key", "relevant", "segments"))
    return (header + "\n" + "\n".join(rows)).encode()


def _labels(content: bytes, families: set[str], segments: list[str]) -> list[tuple[str, str, bool]]:
    labels, _ = _parse(_read_rows(content), families, segments)
    return labels


def test_label_rows_become_relevance_and_segment_labels() -> None:
    labels = _labels(
        "\ufeff".encode() + _csv("e,f1,y,a;b", "e,f2,n,", "e,f3,,", "e,f4,Yes ,none"),
        {"f1", "f2", "f3", "f4"},
        ["a", "b"],
    )
    assert labels == [
        ("f1", "relevance", True),
        ("f1", "segment:a", True),
        ("f1", "segment:b", True),
        ("f2", "relevance", False),  # not relevant: every segment is "no" too
        ("f2", "segment:a", False),
        ("f2", "segment:b", False),
        ("f4", "relevance", True),
        ("f4", "segment:a", False),
        ("f4", "segment:b", False),
    ]


def test_excel_files_from_other_locales_are_read() -> None:
    semicolons = _csv("e;f1;y;a", delimiter=";")
    assert _labels(semicolons, {"f1"}, ["a"])[0] == ("f1", "relevance", True)
    tabs = _csv("e\tf1\tn\t", delimiter="\t")
    assert _labels(tabs, {"f1"}, ["a"])[0] == ("f1", "relevance", False)
    with pytest.raises(LabelError, match="CSV UTF-8"):
        _read_rows("export_id,family_key,relevant,segments\ne,f1,y,caf\u00e9".encode("cp1252"))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("e,f9,y,", "not in this export"),
        ("e,f1,maybe,", "must be y or n"),
        ("e,f1,n,a", "not relevant"),
        ("e,f1,y,zz", "unknown segments"),
        ("e,f1,,a", "'relevant' is blank"),
    ],
)
def test_invalid_label_rows_reject_the_whole_file(row: str, message: str) -> None:
    with pytest.raises(LabelError, match=message):
        _labels(_csv("e,f2,y,", row), {"f1", "f2"}, ["a"])


def test_label_files_need_their_columns_and_some_labels() -> None:
    with pytest.raises(LabelError, match="lacks columns"):
        _read_rows(b"family_key,notes\nf1,x\n")
    with pytest.raises(LabelError, match="empty"):
        _read_rows(b"")
    with pytest.raises(LabelError, match="no labels"):
        _labels(_csv("e,f1,,"), {"f1"}, ["a"])
    with pytest.raises(LabelError, match="appears twice"):
        _labels(_csv("e,f1,y,", "e,f1,n,"), {"f1"}, ["a"])
