"""Strict JSON host configuration for the Product surfaces.

The file selects names, bounds and local repositories.  It cannot contain a
Workflow node, edge, prompt or approval value; topology and authority remain in
the shipped Product contracts.  There is no default Profile and no legacy
schema fallback.

The schema is split in exactly one place, along the line that actually differs
between hosts: :data:`PRODUCT_HOST_SETTINGS_KEYS` is the path-free part - who
runs, under which bounds, verified by which frozen plan - and the rest of
:func:`load_product_host_file` adds the local repositories and roots a Chat host
must be told about.  The benchmark host in :mod:`traceh.evaluation` reuses the
first part verbatim and supplies the second itself, so there is one definition
of "what a Product Profile is" rather than two that drift.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from traceh.api.artifacts import PatchCaptureLimits
from traceh.api.budgets import BudgetLimits
from traceh.api.product import (
    ProductContextPolicy,
    ProductRoleProfile,
    ProductTaskProfile,
    ProductWrapUpReserve,
    RequestedTaskMode,
)
from traceh.api.promotion import (
    PromotionTargetBinding,
    VerificationPlan,
    VerifierCommand,
    VerifierEnvironmentPolicy,
)
from traceh.budgets.events import MAX_BUDGET_VALUE
from traceh.product.errors import ProductInputError
from traceh.product.host import ProductHostProfile
from traceh.promotion.models import freeze_verification_plan

PRODUCT_HOST_SETTINGS_KEYS = frozenset(
    {
        "profile_id",
        "approver_id",
        "default_mode",
        "roles",
        "task_budget",
        "verification",
        "capture_limits",
        "max_report_chars",
        "retained_tokens",
        "investigator_initial_tokens",
        "token_estimate",
    }
)
"""The keys that describe *what a Profile is*, whatever it is bound to.

They are exactly the decisions that must stay identical for two runs to be
comparable: the three role slots, the Router bounds, the aggregate task Budget,
the frozen verification plan and the capture limits.  Nothing here names a
provider, a model, a repository or a root - those are bindings, and every caller
supplies its own, which is why reusing this part cannot smuggle a path into a
host that owns its own repositories.
"""

_LOCATION_KEYS = frozenset(
    {
        "protocol_version",
        "provider_id",
        "model_id",
        "source",
        "promotion_target",
        "managed_workspace_root",
        "cas_root",
    }
)

_TOP_KEYS = PRODUCT_HOST_SETTINGS_KEYS | _LOCATION_KEYS
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
class ProductTokenEstimate:
    """How this host counts request tokens before reserving Budget.

    Without it a reservation must assume the account's whole remaining balance,
    because an uncounted request has no upper bound the host can prove. The
    encoding is stated, never inferred from a model name, and the margin is the
    host's explicit allowance for its tokenizer disagreeing with the provider's.
    """

    encoding: str
    margin_percent: int


@dataclass(frozen=True, slots=True)
class ProductHostSettings:
    """The path-free part of a schema-1 Product host configuration."""

    host_profile: ProductHostProfile
    approver_id: str
    capture_limits: PatchCaptureLimits
    max_report_chars: int
    token_estimate: ProductTokenEstimate | None


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
    token_estimate: ProductTokenEstimate | None


def parse_product_host_settings(
    root: Mapping[str, object],
    *,
    provider_id: str,
    model_id: str,
    source_id: str,
    source_revision: str,
    promotion_target_id: str,
) -> ProductHostSettings:
    """Read the shared keys of one already shape-checked configuration object.

    The caller owns its own exact top-level key set, because that is what makes
    an unknown key a rejection rather than an ignored second instruction
    channel.  What this function owns is the meaning of the shared keys, so a
    second host cannot accept a Profile the Chat host would refuse.

    The provider, model and source/target identities are parameters rather than
    keys.  A Chat host reads them from its file; a host that creates its own
    throwaway repositories and takes its model from the command line states them
    directly.  Neither can be talked into a value the other's file supplied.
    """

    try:
        missing = PRODUCT_HOST_SETTINGS_KEYS - set(root)
        if missing:
            raise ProductInputError("product-host-config-shape-invalid", sorted(missing)[0])
        roles = _object(
            root["roles"], frozenset({"coder", "investigator", "patch_author"}), "roles"
        )
        role_values = {name: _role(roles[name], name) for name in ("coder", "investigator")}
        verification = freeze_verification_plan(_verification(root["verification"]))
        profile = ProductTaskProfile(
            profile_version=7,
            default_mode=RequestedTaskMode(_text(root["default_mode"], "default_mode")),
            provider_id=_text(provider_id, "provider_id"),
            model_id=_text(model_id, "model_id"),
            coder=role_values["coder"],
            investigator=role_values["investigator"],
            patch_author=None
            if roles["patch_author"] is None
            else _role(roles["patch_author"], "patch_author"),
            retained_tokens=_integer(root["retained_tokens"], "retained_tokens"),
            investigator_initial_tokens=_integer(
                root["investigator_initial_tokens"], "investigator_initial_tokens"
            ),
            task_budget=_budget(root["task_budget"], "task_budget"),
            source_id=_text(source_id, "source.source_id"),
            source_revision=_text(source_revision, "source.revision"),
            verification_plan_id=verification.plan_id,
            promotion_target_id=_text(promotion_target_id, "target.target_id"),
        )
        return ProductHostSettings(
            host_profile=ProductHostProfile(
                _text(root["profile_id"], "profile_id"), profile, verification
            ),
            approver_id=_text(root["approver_id"], "approver_id"),
            capture_limits=_capture_limits(root["capture_limits"]),
            max_report_chars=_positive(root["max_report_chars"], "max_report_chars"),
            token_estimate=_token_estimate(root["token_estimate"]),
        )
    except ProductInputError:
        raise
    except Exception:
        raise ProductInputError("product-host-config-invalid", "product_config") from None


def load_product_host_file(path: Path) -> ProductHostFileConfiguration:
    """Read Product host protocol 6 exactly; malformed or partial input has one stable verdict."""

    try:
        raw_path = Path(path)
        with raw_path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        return parse_product_host_config(raw)
    except ProductInputError:
        raise
    except Exception:
        raise ProductInputError("product-host-config-invalid", "product_config") from None


def parse_product_host_config(raw: object) -> ProductHostFileConfiguration:
    """Validate the same host document from disk or an unsaved settings form."""
    try:
        root = _object(raw, _TOP_KEYS, "root")
        if _integer(root["protocol_version"], "protocol_version") != 6:
            raise ValueError
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
        settings = parse_product_host_settings(
            root,
            provider_id=_text(root["provider_id"], "provider_id"),
            model_id=_text(root["model_id"], "model_id"),
            source_id=_text(source["source_id"], "source.source_id"),
            source_revision=_text(source["revision"], "source.revision"),
            promotion_target_id=_text(target["target_id"], "target.target_id"),
        )
        source_path = _absolute(source["repository"], "source.repository")
        target_path = _absolute(target["repository"], "target.repository")
        profile = settings.host_profile.profile
        return ProductHostFileConfiguration(
            host_profile=settings.host_profile,
            approver_id=settings.approver_id,
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
            capture_limits=settings.capture_limits,
            max_report_chars=settings.max_report_chars,
            token_estimate=settings.token_estimate,
        )
    except ProductInputError:
        raise
    except Exception:
        raise ProductInputError("product-host-config-invalid", "product_config") from None


def _token_estimate(value: object) -> ProductTokenEstimate | None:
    """Parse the stated counting decision; `null` is an explicit "do not count"."""

    if value is None:
        return None
    item = _object(value, frozenset({"encoding", "margin_percent"}), "token_estimate")
    encoding = item["encoding"]
    margin = item["margin_percent"]
    if type(encoding) is not str or not encoding.strip():
        raise ProductInputError("product-token-estimate-invalid", "token_estimate.encoding")
    if type(margin) is not int or not 0 <= margin <= 100:
        raise ProductInputError("product-token-estimate-invalid", "token_estimate.margin_percent")
    return ProductTokenEstimate(encoding, margin)


def _role(value: object, field: str) -> ProductRoleProfile:
    item = _object(
        value,
        frozenset(
            {
                "preset",
                "capability_grants",
                "max_output_tokens",
                "budget",
                "max_turn_wall_milliseconds",
            }
        ),
        field,
        # Governing the request window is opt-in, so an absent key means the
        # historical behaviour: no metering and no compaction. Requiring it
        # would invalidate every frozen experiment profile already on disk,
        # and those are evidence this project does not rewrite. Presence is
        # what is explicit, and the assembly digest covers it either way.
        optional=frozenset({"context_policy", "wrap_up_reserve"}),
    )
    grants = item["capability_grants"]
    if type(grants) is not list or any(type(value) is not str for value in grants):
        raise ValueError
    return ProductRoleProfile(
        max_turn_wall_milliseconds=_positive(
            item["max_turn_wall_milliseconds"], f"{field}.max_turn_wall_milliseconds"
        ),
        preset=_text(item["preset"], f"{field}.preset"),
        capability_grants=tuple(grants),
        max_output_tokens=_output_limit(item["max_output_tokens"], f"{field}.max_output_tokens"),
        budget=_budget(item["budget"], f"{field}.budget"),
        context_policy=_context_policy(item.get("context_policy"), f"{field}.context_policy"),
        wrap_up_reserve=_wrap_up_reserve(
            item.get("wrap_up_reserve"), f"{field}.wrap_up_reserve"
        ),
    )


def _wrap_up_reserve(value: object, field: str) -> ProductWrapUpReserve | None:
    """All three held-back amounts, or an explicit absence."""

    if value is None:
        return None
    item = _object(value, frozenset({"steps", "tool_calls", "wall_milliseconds"}), field)
    return ProductWrapUpReserve(
        steps=_integer(item["steps"], f"{field}.steps"),
        tool_calls=_integer(item["tool_calls"], f"{field}.tool_calls"),
        wall_milliseconds=_integer(item["wall_milliseconds"], f"{field}.wall_milliseconds"),
    )


def _context_policy(value: object, field: str) -> ProductContextPolicy | None:
    """All ten fields or an explicit null; there is no partial governance."""

    if value is None:
        return None
    item = _object(
        value,
        frozenset(
            {
                "encoding",
                "window_tokens",
                "output_reserve_tokens",
                "safety_margin_tokens",
                "trigger_percent",
                "fold_relief_percent",
                "fold_protect_recent_groups",
                "fold_protect_readback_utf8_bytes",
                "compaction_trigger_utf8_bytes",
                "compaction_max_summary_utf8_bytes",
                "compaction_keep_recent_turns",
            }
        ),
        field,
    )
    optional = {}
    for name in ("fold_relief_percent", "fold_protect_recent_groups"):
        raw = item[name]
        if raw is not None and (type(raw) is not int or type(raw) is bool or raw < 0):
            raise ProductInputError("product-context-policy-invalid", f"{field}.{name}")
        optional[name] = raw
    keep = item["compaction_keep_recent_turns"]
    if type(keep) is not int or type(keep) is bool or keep < 0:
        raise ProductInputError(
            "product-context-policy-invalid", f"{field}.compaction_keep_recent_turns"
        )
    try:
        return ProductContextPolicy(
            encoding=_text(item["encoding"], f"{field}.encoding"),
            window_tokens=_positive(item["window_tokens"], f"{field}.window_tokens"),
            output_reserve_tokens=_positive(
                item["output_reserve_tokens"], f"{field}.output_reserve_tokens"
            ),
            safety_margin_tokens=_positive(
                item["safety_margin_tokens"], f"{field}.safety_margin_tokens"
            ),
            trigger_percent=_positive(item["trigger_percent"], f"{field}.trigger_percent"),
            compaction_trigger_utf8_bytes=_positive(
                item["compaction_trigger_utf8_bytes"], f"{field}.compaction_trigger_utf8_bytes"
            ),
            compaction_max_summary_utf8_bytes=_positive(
                item["compaction_max_summary_utf8_bytes"],
                f"{field}.compaction_max_summary_utf8_bytes",
            ),
            compaction_keep_recent_turns=keep,
            fold_protect_readback_utf8_bytes=_positive(
                item["fold_protect_readback_utf8_bytes"],
                f"{field}.fold_protect_readback_utf8_bytes",
            )
            if item["fold_protect_readback_utf8_bytes"]
            else 0,
            **optional,
        )
    except ProductInputError:
        raise
    except Exception:
        raise ProductInputError("product-context-policy-invalid", field) from None


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
            frozenset({"command_id", "argv", "timeout_ms", "public_requirement"}),
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
                public_requirement=command["public_requirement"],
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
        type(name) is not str or type(setting) is not str for name, setting in overrides.items()
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


def _object(
    value: object,
    keys: frozenset[str],
    field: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Exact required keys; ``optional`` names may appear but never anything else.

    An unrecognised key is still a rejection rather than an ignored second
    instruction channel - that property is the point of this check and the
    allowance does not weaken it.
    """

    if type(value) is not dict or not keys <= set(value) or not set(value) <= keys | optional:
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


def _output_limit(value: object, field: str) -> int:
    result = _positive(value, field)
    if result > MAX_BUDGET_VALUE:
        raise ProductInputError("product-host-config-value-invalid", field)
    return result


def _absolute(value: object, field: str) -> Path:
    text = _text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise ProductInputError("product-host-config-path-invalid", field)
    return path


__all__ = [
    "PRODUCT_HOST_SETTINGS_KEYS",
    "ProductHostFileConfiguration",
    "ProductHostSettings",
    "ProductTokenEstimate",
    "load_product_host_file",
    "parse_product_host_config",
    "parse_product_host_settings",
]
