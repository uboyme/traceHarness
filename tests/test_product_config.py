"""The optional F3 host file selects values, never a Workflow graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceh.api.product import RequestedTaskMode
from traceh.cli.main import build_parser
from traceh.product.config import load_product_host_file
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
        "budget": _budget(),
    }
    return {
        "protocol_version": 1,
        "profile_id": "local-product-profile",
        "approver_id": "local-human",
        "provider_id": "scripted",
        "model_id": "scripted-model",
        "default_mode": "auto",
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
            "parent": role,
            "reviewer": role,
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
        "router": {
            "preset": "mode-router",
            "budget": _budget(),
            "timeout_milliseconds": 30_000,
            "max_response_bytes": 2_048,
        },
        "task_budget": _budget(),
        "verification": {
            "plan_id": "local-verification",
            "plan_version": 1,
            "commands": [
                {
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
            "protocol_version": 1,
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

    assert resolved.host_profile.profile.default_mode is RequestedTaskMode.AUTO
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


def test_relative_host_paths_fail_before_any_resource_is_opened(tmp_path: Path) -> None:
    payload = _configuration(tmp_path)
    source = payload["source"]
    assert isinstance(source, dict)
    source["repository"] = "relative/source"

    with pytest.raises(ProductInputError) as caught:
        load_product_host_file(_write(tmp_path, payload))

    assert caught.value.code == "product-host-config-path-invalid"


def test_chat_parser_exposes_one_optional_product_configuration() -> None:
    args = build_parser().parse_args(
        ["chat", ".", "--product-config", "product.json"]
    )

    assert args.product_config == Path("product.json")
