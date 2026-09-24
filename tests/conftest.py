"""Suite-wide conditions that depend on the interpreter, not on the code under test."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

RETRIEVAL_EPISODES = Path(__file__).parents[1] / "benchmarks" / "retrieval_episodes_v1"


def _frozen_unicode_versions() -> frozenset[str]:
    """The Unicode versions the shipped retrieval-episode benchmark froze.

    Read from the benchmark itself so the condition follows the data: a
    re-frozen benchmark changes this without touching the tests.
    """

    found: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "unicode_version" and isinstance(item, str):
                    found.add(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads((RETRIEVAL_EPISODES / "benchmark.json").read_text(encoding="utf-8")))
    return frozenset(found)


def pytest_collection_modifyitems(config, items) -> None:
    frozen = _frozen_unicode_versions()
    if unicodedata.unidata_version in frozen:
        return
    # ReferenceRetrievalPolicy refuses a Unicode database other than the one
    # its data was normalised with, so on this interpreter the shipped
    # benchmark is refused by design (retrieval-unicode-version-unsupported).
    # Only tests that run that benchmark are skipped; the refusal itself is
    # covered on every interpreter by test_retrieval_policy_refuses_*.
    skip = pytest.mark.skip(
        reason=(
            f"retrieval_episodes_v1 froze Unicode {', '.join(sorted(frozen))}; "
            f"this interpreter has {unicodedata.unidata_version} and the loader refuses it"
        )
    )
    for item in items:
        if item.get_closest_marker("frozen_unicode") is not None:
            item.add_marker(skip)
