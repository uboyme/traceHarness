"""Host-only acceptance for a1-small-hashlib-blake2."""

from astroid import MANAGER


def _init(name):
    return MANAGER.ast_from_module_name("hashlib")[name]["__init__"]


def test_blake2b_constructor_keeps_its_keyword_only_parameters():
    arguments = _init("blake2b").args
    names = [argument.name for argument in arguments.kwonlyargs]
    assert names[:4] == ["digest_size", "key", "salt", "person"]
    assert arguments.kw_defaults[0].value == 64


def test_blake2s_constructor_keeps_its_keyword_only_parameters():
    arguments = _init("blake2s").args
    names = [argument.name for argument in arguments.kwonlyargs]
    assert names[:4] == ["digest_size", "key", "salt", "person"]
    assert arguments.kw_defaults[0].value == 32


def test_other_algorithms_keep_the_value_parameter():
    for name in ("md5", "sha256", "sha3_512", "shake_128"):
        arguments = _init(name).args
        assert [argument.name for argument in arguments.args] == ["self", "value"]
        assert not arguments.kwonlyargs
