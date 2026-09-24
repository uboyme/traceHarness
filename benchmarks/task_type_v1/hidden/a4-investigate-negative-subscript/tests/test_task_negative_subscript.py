"""Host-only acceptance for a4-investigate-negative-subscript."""

import astroid
import pytest
from astroid import nodes, util
from astroid.exceptions import InferenceError


def _value(code):
    return next(astroid.extract_node(code).infer())


@pytest.mark.parametrize(
    "code,expected",
    [
        ("[1, 2, 3][-1] #@", 3),
        ("[1, 2, 3][-3] #@", 1),
        ("(1, 2)[-2] #@", 1),
        ("x = ['a', 'b']\nx[-1] #@", "b"),
    ],
)
def test_negative_indexes_of_literal_sequences_infer(code, expected):
    inferred = _value(code)
    assert isinstance(inferred, nodes.Const) and inferred.value == expected


def test_positive_indexes_and_slices_still_infer():
    assert _value("[1, 2, 3][1] #@").value == 2
    sliced = _value("[1, 2, 3][-2:] #@")
    assert isinstance(sliced, nodes.List)
    assert [element.value for element in sliced.elts] == [2, 3]


@pytest.mark.parametrize("code", ["[1, 2][-3] #@", "(1,)[5] #@"])
def test_out_of_range_indexes_fail_gracefully(code):
    # astroid's documented failure modes; a raw IndexError would be a crash.
    try:
        inferred = _value(code)
    except InferenceError:
        return
    assert inferred is util.Uninferable
