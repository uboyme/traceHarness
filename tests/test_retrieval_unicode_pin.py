"""The retrieval policy is bound to one Unicode database; that binding runs on every interpreter.

Tests that run the shipped retrieval-episode benchmark are skipped where the
interpreter's Unicode database differs from the one the benchmark froze
(``conftest.py``). This module keeps the reason for that skip honest: it is
the loader refusing a mismatch by design, and it never is skipped itself.
"""

import json
import unicodedata

import pytest
from conftest import RETRIEVAL_EPISODES, _frozen_unicode_versions

from traceh.api.retrieval import ReferenceRetrievalPolicy


def _shipped_profile() -> dict:
    data = json.loads((RETRIEVAL_EPISODES / "benchmark.json").read_text(encoding="utf-8"))
    return data["task_settings"]["contexts"]["skill"]["skills"]


def test_the_policy_loads_on_the_interpreters_own_unicode_database():
    profile = {**_shipped_profile(), "unicode_version": unicodedata.unidata_version}
    policy = ReferenceRetrievalPolicy.from_dict(profile)
    assert policy.unicode_version == unicodedata.unidata_version


def test_retrieval_policy_refuses_another_unicode_database():
    profile = {**_shipped_profile(), "unicode_version": "0.0.0-not-this-interpreter"}
    with pytest.raises(ValueError, match="retrieval-unicode-version-unsupported"):
        ReferenceRetrievalPolicy.from_dict(profile)


def test_the_skip_condition_reads_the_versions_the_benchmark_froze():
    assert _frozen_unicode_versions() == {_shipped_profile()["unicode_version"]}
