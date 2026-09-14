"""Only an explicit strategy comparison may pair distinct execution modes."""

import copy
import json

import pytest
from test_evaluation_comparison import pair_plan

from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.plan import comparison_policy, load_run_options


@pytest.mark.parametrize(
    "kind,modes,valid",
    [
        ("text_candidate", None, True),
        ("text_candidate", ["multi", "multi"], True),
        ("text_candidate", ["single", "multi"], False),
        ("execution_strategy", ["single", "multi"], True),
        ("execution_strategy", ["single", "single"], True),
        ("execution_strategy", None, False),
        ("execution_strategy", ["auto", "multi"], False),
        ("execution_strategy", [], False),
        ("unknown", ["single", "multi"], False),
    ],
)
def test_comparison_kind_controls_exact_allowed_difference(tmp_path, kind, modes, valid):
    raw = json.loads(pair_plan(tmp_path).read_text())["comparison"]
    raw.update(kind=kind, requested_modes=modes)
    if valid:
        assert comparison_policy(raw) == raw
    else:
        with pytest.raises(BenchmarkManifestError):
            comparison_policy(raw)


def test_strategy_may_not_change_source_and_old_policy_is_refused(tmp_path):
    path = pair_plan(tmp_path, patch=True)
    raw = json.loads(path.read_text())
    raw["comparison"].update(kind="execution_strategy", requested_modes=["single", "multi"])
    path.write_text(json.dumps(raw))
    with pytest.raises(BenchmarkManifestError) as refused:
        load_run_options(path)
    assert refused.value.code == "evaluation-candidate-scope-invalid"
    legacy = copy.deepcopy(raw["comparison"])
    legacy["format"] = 1
    with pytest.raises(BenchmarkManifestError) as refused:
        comparison_policy(legacy)
    assert refused.value.code == "evaluation-version-unsupported"
