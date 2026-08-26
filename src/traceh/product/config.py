"""Strict JSON host configuration for the optional F3 Chat surface.

The file selects names, bounds and local repositories.  It cannot contain a
Workflow node, edge, prompt or approval value; topology and authority remain in
the shipped Product contracts.  There is no default Profile and no legacy
schema fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from traceh.api.artifacts import PatchCaptureLimits
from traceh.api.budgets import BudgetLimits
from traceh.api.product import (
    ProductRoleProfile,
    ProductRouterProfile,
    ProductTaskProfile,
    RequestedTaskMode,
)
from traceh.api.promotion import (
    PromotionTargetBinding,
    VerificationPlan,
    VerifierCommand,
    VerifierEnvironmentPolicy,
)
from traceh.product.errors import ProductInputError
from traceh.product.host import ProductHostProfile

_TOP_KEYS = frozenset(
    {
        "protocol_version",
        "profile_id",
        "approver_id",
        "provider_id",
        "model_id",
        "default_mode",
        "source",
        "promotion_target",
        "managed_workspace_root",
        "cas_root",
        "roles",
        "router",
        "task_budget",
        "verification",
        "capture_limits",
        "max_report_chars",
    }
)
_BUDGET_KEYS = frozenset(
    {
        "max_tokens",
        "max_steps",
        "max_tool_calls",
        "max_wall_milliseconds",
        "max_children",
        "max_depth",
        "max_processes",
    }
)


@dataclass(frozen=True, slots=True)
class ProductHostFileConfiguration:
    host_profile: ProductHostProfile
    approver_id: str
    managed_workspace_root: Path
    cas_root: Path
    source_id: str
    source_repository: Path
    promotion_target_id: str
    promotion_target: PromotionTargetBinding
    capture_limits: PatchCaptureLimits
    max_report_chars: int


def load_product_host_file(path: Path) -> ProductHostFileConfiguration:
    """Read schema 1 exactly; malformed or partial input has one stable verdict."""

    try:
        raw_path = Path(path)
        with raw_path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        root = _object(raw, _TOP_KEYS, "root")
        if _integer(root["protocol_version"], "protocol_version") != 1:
            raise ValueError
        profile_id = _text(root["profile_id"], "profile_id")
        provider_id = _text(root["provider_id"], "provider_id")
        model_id = _text(root["model_id"], "model_id")
        source = _object(
            root["source"],
            frozenset({"source_id", "repository", "revision"}),
            "source",
        )
        target = _object(
            root["promotion_target"],
            frozenset({"target_id", "repository", "ref"}),
            "promotion_target",
        )
        roles = _object(
            root["roles"], frozenset({"parent", "reviewer", "coder"}), "roles"
        )
        role_values = {
            name: _role(roles[name], name) for name in ("parent", "reviewer", "coder")
        }
        router_raw = _object(
            root["router"],
            frozenset(
                {"preset", "budget", "timeout_milliseconds", "max_response_bytes"}
            ),
            "router",
        )
        verification = _verification(root["verification"])
        profile = ProductTaskProfile(
            profile_version=1,
            default_mode=RequestedTaskMode(_text(root["default_mode"], "default_mode")),
            provider_id=provider_id,
            model_id=model_id,
            parent=role_values["parent"],
            reviewer=role_values["reviewer"],
            coder=role_values["coder"],
            router=ProductRouterProfile(
                preset=_text(router_raw["preset"], "router.preset"),
                budget=_budget(router_raw["budget"], "router.budget"),
                timeout_milliseconds=_integer(
                    router_raw["timeout_milliseconds"], "router.timeout_milliseconds"
                ),
                max_response_bytes=_integer(
                    router_raw["max_response_bytes"], "router.max_response_bytes"
                ),
            ),
            task_budget=_budget(root["task_budget"], "task_budget"),
            source_id=_text(source["source_id"], "source.source_id"),
            source_revision=_text(source["revision"], "source.revision"),
            verification_plan_id=verification.plan_id,
            promotion_target_id=_text(target["target_id"], "target.target_id"),
        )
        source_path = _absolute(source["repository"], "source.repository")
        target_path = _absolute(target["repository"], "target.repository")
        return ProductHostFileConfiguration(
            host_profile=ProductHostProfile(profile_id, profile, verification),
            approver_id=_text(root["approver_id"], "approver_id"),
            managed_workspace_root=_absolute(
                root["managed_workspace_root"], "managed_workspace_root"
            ),
            cas_root=_absolute(root["cas_root"], "cas_root"),
            source_id=profile.source_id,
            source_repository=source_path,
            promotion_target_id=profile.promotion_target_id,
            promotion_target=PromotionTargetBinding(
                repository_path=target_path,
                target_ref=_text(target["ref"], "target.ref"),
            ),
            capture_limits=_capture_limits(root["capture_limits"]),
            max_report_chars=_positive(root["max_report_chars"], "max_report_chars"),
        )
    except ProductInputError:
        raise
    except Exception:
        raise ProductInputError("product-host-config-invalid", "product_config") from None


def _role(value: object, field: str) -> ProductRoleProfile:
    item = _object(
        value,
        frozenset({"preset", "capability_grants", "budget"}),
        field,
    )
    grants = item["capability_grants"]
    if type(grants) is not list or any(type(value) is not str for value in grants):
        raise ValueError
    return ProductRoleProfile(
        preset=_text(item["preset"], f"{field}.preset"),
        capability_grants=tuple(grants),
        budget=_budget(item["budget"], f"{field}.budget"),
    )


def _budget(value: object, field: str) -> BudgetLimits:
    item = _object(value, _BUDGET_KEYS, field)
    parsed: dict[str, int | None] = {}
    for name in _BUDGET_KEYS:
        raw = item[name]
        if raw is None:
            parsed[name] = None
        else:
            parsed[name] = _integer(raw, f"{field}.{name}")
    return BudgetLimits(**parsed)  # type: ignore[arg-type]


def _verification(value: object) -> VerificationPlan:
    item = _object(
        value,
        frozenset(
            {
                "plan_id",
                "plan_version",
                "commands",
                "environment",
                "max_output_bytes",
                "protocol_version",
            }
        ),
        "verification",
    )
    commands = item["commands"]
    if type(commands) is not list or not commands:
        raise ValueError
    resolved_commands = []
    for index, raw in enumerate(commands):
        command = _object(
            raw,
            frozenset({"command_id", "argv", "timeout_ms"}),
            f"verification.commands.{index}",
        )
        argv = command["argv"]
        if type(argv) is not list or not argv or any(type(arg) is not str for arg in argv):
            raise ValueError
        resolved_commands.append(
            VerifierCommand(
                command_id=_text(command["command_id"], "command_id"),
                argv=tuple(argv),
                timeout_ms=_positive(command["timeout_ms"], "timeout_ms"),
            )
        )
    environment = _object(
        item["environment"],
        frozenset({"policy_id", "passthrough", "overrides"}),
        "verification.environment",
    )
    passthrough = environment["passthrough"]
    overrides = environment["overrides"]
    if type(passthrough) is not list or any(type(name) is not str for name in passthrough):
        raise ValueError
    if type(overrides) is not dict or any(
        type(name) is not str or type(setting) is not str
        for name, setting in overrides.items()
    ):
        raise ValueError
    return VerificationPlan(
        plan_id=_text(item["plan_id"], "verification.plan_id"),
        plan_version=_positive(item["plan_version"], "verification.plan_version"),
        commands=tuple(resolved_commands),
        environment=VerifierEnvironmentPolicy(
            policy_id=_text(environment["policy_id"], "environment.policy_id"),
            passthrough=tuple(passthrough),
            overrides=tuple(sorted(overrides.items())),
        ),
        max_output_bytes=_positive(item["max_output_bytes"], "max_output_bytes"),
        protocol_version=_positive(item["protocol_version"], "protocol_version"),
    )


def _capture_limits(value: object) -> PatchCaptureLimits:
    keys = frozenset(
        {
            "max_changed_paths",
            "max_path_bytes",
            "max_file_bytes",
            "max_total_file_bytes",
            "max_patch_bytes",
        }
    )
    item = _object(value, keys, "capture_limits")
    return PatchCaptureLimits(
        **{name: _positive(item[name], f"capture_limits.{name}") for name in keys}
    )


def _object(value: object, keys: frozenset[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ProductInputError("product-host-config-shape-invalid", field)
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductInputError("product-host-config-value-invalid", field)
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ProductInputError("product-host-config-value-invalid", field)
    return value


def _positive(value: object, field: str) -> int:
    result = _integer(value, field)
    if result < 1:
        raise ProductInputError("product-host-config-value-invalid", field)
    return result


def _absolute(value: object, field: str) -> Path:
    text = _text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise ProductInputError("product-host-config-path-invalid", field)
    return path


__all__ = ["ProductHostFileConfiguration", "load_product_host_file"]
