"""Host-only acceptance for a3-parallel-fix-three-brains."""

import astroid
from astroid import nodes


def test_uuid_instances_have_an_int_member():
    node = astroid.extract_node(
        """
    import uuid
    uuid.UUID('{12345678-1234-5678-1234-567812345678}').int #@
    """
    )
    assert isinstance(next(node.infer()), nodes.Const)


def test_imported_sample_name_is_inferred_as_a_list():
    node = astroid.extract_node(
        """
    from random import sample
    sample(['a', 'b', 'c'], 2) #@
    """
    )
    inferred = next(node.infer())
    assert isinstance(inferred, nodes.List)
    assert len(inferred.elts) == 2
    assert {element.value for element in inferred.elts} <= {"a", "b", "c"}


def test_attribute_sample_is_still_inferred():
    node = astroid.extract_node(
        """
    import random
    random.sample([1, 2, 3], 1) #@
    """
    )
    inferred = next(node.infer())
    assert isinstance(inferred, nodes.List) and len(inferred.elts) == 1


def test_popen_context_manager_yields_the_process():
    node = astroid.extract_node(
        """
    import subprocess
    with subprocess.Popen(['ls']) as proc:
        proc #@
    """
    )
    inferred = next(node.infer())
    assert isinstance(inferred, astroid.Instance)
    assert inferred.name == "Popen"
    assert "communicate" in inferred._proxied.locals
