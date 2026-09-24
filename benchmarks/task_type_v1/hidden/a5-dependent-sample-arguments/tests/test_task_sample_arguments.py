"""Host-only acceptance for a5-dependent-sample-arguments."""

import astroid
import pytest
from astroid import nodes


def _value(code):
    return next(astroid.extract_node(code).infer())


def _sampled(code, size, population):
    inferred = _value(code)
    assert isinstance(inferred, nodes.List)
    values = [element.value for element in inferred.elts]
    assert len(values) == size and set(values) <= set(population)


def test_positional_call_still_works():
    _sampled("import random\nrandom.sample([1, 2, 3], 2) #@", 2, [1, 2, 3])


def test_k_by_keyword():
    _sampled("import random\nrandom.sample([1, 2, 3], k=2) #@", 2, [1, 2, 3])


def test_population_and_k_by_keyword():
    _sampled("import random\nrandom.sample(population=[1, 2], k=1) #@", 1, [1, 2])


def test_k_from_an_expression_inferring_to_an_int():
    _sampled(
        "import random\nitems = ['a', 'b', 'c']\nn = 2\nrandom.sample(items, n) #@",
        2,
        ["a", "b", "c"],
    )


@pytest.mark.parametrize(
    "call",
    [
        "random.sample([1, 2]) #@",
        "random.sample([1, 2], 3) #@",
        "random.sample([1, 2], k='2') #@",
        "random.sample([1, 2], k=1, extra=2) #@",
        "random.sample(unknown, 1) #@",
    ],
)
def test_unsatisfiable_calls_fall_back_without_raising(call):
    node = astroid.extract_node("import random\n" + call)
    for inferred in node.infer():  # Must not raise.
        if isinstance(inferred, nodes.List) and inferred.elts:
            # Normal inference of the stdlib body is fine; a sample fabricated from
            # the literal population elements is not.
            assert not all(
                isinstance(e, nodes.Const) and e.value in (1, 2) for e in inferred.elts
            )
