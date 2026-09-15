"""The optional F3 host file selects values, never a Workflow graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceh.api.product import RequestedTaskMode
from traceh.cli.main import build_parser
from traceh.product.config import (
    PRODUCT_HOST_SETTINGS_KEYS,
    load_product_host_file,
    parse_product_host_settings,
)
from traceh.product.errors import ProductInputError


def _budget() -> dict[str, int]:
    return {
        "max_tokens": 20_000,
        "max_steps": 12,
        "max_tool_calls": 20,
        "max_wall_milliseconds": 120_000,
        "max_children": 4,
        "max_depth": 1,
        "max_processes": 4,
    }


def _configuration(tmp_path: Path) -> dict[str, object]:
    role = {
        "preset": "coding-role",
        "capability_grants": ["list_files", "read_file", "search_text"],
        "max_output_tokens": 4_096,
        "budget": _budget(),
        "max_turn_wall_milliseconds": 60_000,
    }
    return {
        "protocol_version": 6,
        "retained_tokens": 4_000,
        "investigator_initial_tokens": 4_000,
        "token_estimate": None,
        "profile_id": "local-product-profile",
        "approver_id": "local-human",
        "provider_id": "scripted",
        "model_id": "scripted-model",
        "default_mode": "single",
        "source": {
            "source_id": "local-source",
            "repository": str((tmp_path / "source").absolute()),
            "revision": "main",
        },
        "promotion_target": {
            "target_id": "local-target",
            "repository": str((tmp_path / "target.git").absolute()),
            "ref": "refs/heads/main",
        },
        "managed_workspace_root": str((tmp_path / "managed").absolute()),
        "cas_root": str((tmp_path / "cas").absolute()),
        "roles": {
            "patch_author": None,
            "investigator": role,
            "coder": {
                **role,
                "capability_grants": [
                    "list_files",
                    "read_file",
                    "search_text",
                    "apply_patch",
                    "shell",
                ],
            },
        },
        "task_budget": _budget(),
        "verification": {
            "plan_id": "local-verification",
            "plan_version": 1,
            "commands": [
                {
                    "public_requirement": None,
                    "command_id": "tests",
                    "argv": ["python", "-m", "pytest", "-q"],
                    "timeout_ms": 60_000,
                }
            ],
            "environment": {
                "policy_id": "minimal",
                "passthrough": [],
                "overrides": {},
            },
            "max_output_bytes": 1_048_576,
            "protocol_version": 3,
        },
        "capture_limits": {
            "max_changed_paths": 100,
            "max_path_bytes": 1_024,
            "max_file_bytes": 1_048_576,
            "max_total_file_bytes": 4_194_304,
            "max_patch_bytes": 4_194_304,
        },
        "max_report_chars": 4_096,
    }


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "product.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_exact_host_configuration_selects_values_without_a_graph(
    tmp_path: Path,
) -> None:
    resolved = load_product_host_file(_write(tmp_path, _configuration(tmp_path)))

    assert resolved.host_profile.profile.default_mode is RequestedTaskMode.SINGLE
    assert resolved.source_id == "local-source"
    assert resolved.promotion_target.target_ref == "refs/heads/main"


@pytest.mark.parametrize("forbidden", ["nodes", "edges", "prompt", "approval_digest"])
def test_topology_and_authority_values_cannot_enter_the_host_file(
    tmp_path: Path, forbidden: str
) -> None:
    payload = _configuration(tmp_path)
    payload[forbidden] = []

    with pytest.raises(ProductInputError, match="product root is not usable"):
        load_product_host_file(_write(tmp_path, payload))


def test_a_host_states_whether_it_counts_tokens_before_reserving(tmp_path: Path) -> None:
    """The counting decision is declared, never inferred from the model name."""

    payload = _configuration(tmp_path)
    payload["token_estimate"] = {"encoding": "cl100k_base", "margin_percent": 30}
    loaded = load_product_host_file(_write(tmp_path, payload))
    assert loaded.token_estimate is not None
    assert (loaded.token_estimate.encoding, loaded.token_estimate.margin_percent) == (
        "cl100k_base",
        30,
    )

    payload["token_estimate"] = None
    assert load_product_host_file(_write(tmp_path, payload)).token_estimate is None


def test_a_host_document_without_the_counting_decision_is_refused(tmp_path: Path) -> None:
    """An older document is refused outright rather than silently guessed.

    Both entry points reject it: the file host on the document's key set, and the
    shared settings parser by naming the absent key, which is what a benchmark
    host reports.
    """

    payload = _configuration(tmp_path)
    del payload["token_estimate"]
    with pytest.raises(ProductInputError) as refused:
        load_product_host_file(_write(tmp_path, payload))
    assert refused.value.code == "product-host-config-shape-invalid"

    shared = {key: payload[key] for key in PRODUCT_HOST_SETTINGS_KEYS if key in payload}
    shared["verification"] = payload["verification"]
    with pytest.raises(ProductInputError) as named:
        parse_product_host_settings(
            shared,
            provider_id="scripted",
            model_id="scripted-model",
            source_id="s",
            source_revision="r",
            promotion_target_id="t",
        )
    assert named.value.code == "product-host-config-shape-invalid"
    assert named.value.field == "token_estimate"


@pytest.mark.parametrize(
    "invalid",
    [
        {"encoding": "", "margin_percent": 10},
        {"encoding": "  ", "margin_percent": 10},
        {"encoding": 7, "margin_percent": 10},
        {"encoding": "cl100k_base", "margin_percent": -1},
        {"encoding": "cl100k_base", "margin_percent": 101},
        {"encoding": "cl100k_base", "margin_percent": 1.5},
        {"encoding": "cl100k_base"},
        {"encoding": "cl100k_base", "margin_percent": 10, "window_tokens": 1},
    ],
)
def test_an_unusable_counting_decision_is_refused(tmp_path: Path, invalid: object) -> None:
    payload = _configuration(tmp_path)
    payload["token_estimate"] = invalid
    with pytest.raises(ProductInputError):
        load_product_host_file(_write(tmp_path, payload))


def test_relative_host_paths_fail_before_any_resource_is_opened(tmp_path: Path) -> None:
    payload = _configuration(tmp_path)
    source = payload["source"]
    assert isinstance(source, dict)
    source["repository"] = "relative/source"

    with pytest.raises(ProductInputError) as caught:
        load_product_host_file(_write(tmp_path, payload))

    assert caught.value.code == "product-host-config-path-invalid"


def test_chat_parser_exposes_one_optional_product_configuration() -> None:
    args = build_parser().parse_args(["chat", ".", "--product-config", "product.json"])

    assert args.product_config == Path("product.json")


def test_legacy_role_shape_without_a_request_output_limit_is_refused(
    tmp_path: Path,
) -> None:
    payload = _configuration(tmp_path)
    roles = payload["roles"]
    assert isinstance(roles, dict)
    coder = roles["coder"]
    assert isinstance(coder, dict)
    coder.pop("max_output_tokens")

    with pytest.raises(ProductInputError) as caught:
        load_product_host_file(_write(tmp_path, payload))

    assert caught.value.code == "product-host-config-shape-invalid"


def test_request_output_limit_must_be_a_positive_explicit_integer(
    tmp_path: Path,
) -> None:
    payload = _configuration(tmp_path)
    roles = payload["roles"]
    assert isinstance(roles, dict)
    roles["coder"]["max_output_tokens"] = 0

    with pytest.raises(ProductInputError) as caught:
        load_product_host_file(_write(tmp_path, payload))

    assert caught.value.code == "product-host-config-value-invalid"


@pytest.mark.parametrize("legacy", ("auto", "adaptive"))
def test_removed_execution_modes_are_not_silently_converted(tmp_path, legacy):
    payload = _configuration(tmp_path)
    payload["default_mode"] = legacy
    with pytest.raises(ProductInputError):
        load_product_host_file(_write(tmp_path, payload))
    assert payload["default_mode"] == legacy


@pytest.mark.parametrize("version", (1, 2, 3, 4, 5))
def test_legacy_host_protocol_requires_an_explicit_new_configuration(tmp_path, version):
    payload = _configuration(tmp_path)
    payload["protocol_version"] = version
    with pytest.raises(ProductInputError):
        load_product_host_file(_write(tmp_path, payload))


@pytest.mark.parametrize("field", ("router", "parent", "reviewer"))
def test_removed_router_and_fixed_roles_are_rejected(tmp_path, field):
    payload = _configuration(tmp_path)
    if field == "router":
        payload[field] = {}
    else:
        payload["roles"][field] = dict(payload["roles"]["investigator"])
    with pytest.raises(ProductInputError) as caught:
        load_product_host_file(_write(tmp_path, payload))
    assert caught.value.code == "product-host-config-shape-invalid"
