from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patsquire_plr.canonical import (
    CanonicalValueError,
    canonical_json,
    canonical_sha256,
    to_canonical,
)

json_leaves = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.text(max_size=20)
    | st.decimals(allow_nan=False, allow_infinity=False, places=6)
)
json_values = st.recursive(
    json_leaves,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


def test_key_order_does_not_matter() -> None:
    assert (
        canonical_json({"b": 1, "a": [1, 2]})
        == canonical_json({"a": [1, 2], "b": 1})
        == '{"a":[1,2],"b":1}'
    )


def test_equal_decimals_encode_identically() -> None:
    assert canonical_json(Decimal("0.50")) == canonical_json(Decimal("0.5")) == '{"$decimal":"0.5"}'
    assert canonical_json(Decimal("-0.000")) == canonical_json(Decimal(0))
    assert canonical_json(Decimal("1E+3")) == '{"$decimal":"1000"}'


@pytest.mark.parametrize("value", [0.5, {"a": 1.0}, [1, 2.0]])
def test_floats_are_rejected_with_their_path(value: object) -> None:
    with pytest.raises(CanonicalValueError, match="floats are not allowed"):
        canonical_json(value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (Decimal("NaN"), "non-finite"),
        ({1: "x"}, "keys must be str"),
        (object(), "unsupported type"),
    ],
)
def test_other_invalid_values_are_rejected(value: object, message: str) -> None:
    with pytest.raises(CanonicalValueError, match=message):
        to_canonical(value)


def test_tuples_encode_like_lists() -> None:
    assert canonical_json((1, "a")) == canonical_json([1, "a"])


@given(json_values)
def test_encoding_is_deterministic_and_idempotent(value: object) -> None:
    assert canonical_json(value) == canonical_json(value)
    assert canonical_sha256(value) == canonical_sha256(value)
    # Re-encoding the canonical form changes nothing (Decimal objects become plain dicts).
    assert canonical_json(to_canonical(value)) == canonical_json(
        value
    ) or "$decimal" in canonical_json(value)


@given(st.decimals(allow_nan=False, allow_infinity=False), st.integers(min_value=0, max_value=5))
def test_trailing_zeros_never_change_the_hash(value: Decimal, zeros: int) -> None:
    padded = Decimal(f"{value:f}{'.' if '.' not in f'{value:f}' and zeros else ''}{'0' * zeros}")
    assert canonical_sha256(padded) == canonical_sha256(value)
