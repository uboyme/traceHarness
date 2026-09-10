"""Explicit current or paired local-source plans, resolved before execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import (
    FrozenFile,
    object_fields,
    read_input,
    referenced_input,
    text_field,
)
from traceh.llm.retry import ModelRetryPolicy

RETRY_FIELDS = frozenset(
    {
        "max_attempts",
        "max_elapsed_seconds",
        "base_delay_seconds",
        "max_delay_seconds",
        "retry_after_cap_seconds",
        "jitter_ratio",
    }
)
MODEL_FIELDS = frozenset({"provider", "model", "base_url", "api_key_env", "script", "retry_policy"})


def _retry_attribute(field: str) -> str:
    if field == "retry_after_cap_seconds":
        return "model_retry_after_cap_seconds"
    return "model_retry_" + field


@dataclass(frozen=True, slots=True)
class VariantSpec:
    variant_id: str
    role: str
    patch: FrozenFile | None


def comparison_policy(value):
    object_fields(
        value, {"format", "min_pass_gain", "max_token_ratio", "max_tool_call_delta"}, "comparison"
    )
    if type(value["format"]) is not int or value["format"] != 1:
        raise BenchmarkManifestError("evaluation-version-unsupported", "comparison")
    for name in ("min_pass_gain", "max_tool_call_delta"):
        v = value[name]
        if v is not None and (type(v) is not int or v < 0):
            raise BenchmarkManifestError("evaluation-manifest-invalid", name)
    v = value["max_token_ratio"]
    from math import isfinite

    if v is not None and (type(v) not in (int, float) or not isfinite(v) or v < 0):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "max_token_ratio")
    return value


@dataclass(frozen=True, slots=True)
class RunOptions:
    repetitions: int = 1
    max_trials: int | None = None
    timeout_seconds: float | None = None
    variant_id: str = "current"
    document: FrozenFile | None = None
    case_ids: tuple[str, ...] | None = None
    material_seeds: tuple[int, ...] | None = None
    variants: tuple[VariantSpec, ...] = ()

    def __post_init__(self):
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= 25:
            raise BenchmarkManifestError("evaluation-manifest-invalid", "repetitions")
        if self.max_trials is not None and (
            type(self.max_trials) is not int or self.max_trials < 1
        ):
            raise BenchmarkManifestError("evaluation-manifest-invalid", "max_trials")
        if self.timeout_seconds is not None:
            from math import isfinite

            if (
                type(self.timeout_seconds) not in (int, float)
                or not isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0
            ):
                raise BenchmarkManifestError("evaluation-manifest-invalid", "timeout_seconds")
        text_field(self.variant_id, "variant_id")
        for name, cls in (("case_ids", str), ("material_seeds", int)):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not tuple
                or not value
                or any(type(item) is not cls for item in value)
                or len(set(value)) != len(value)
            ):
                raise BenchmarkManifestError("evaluation-manifest-invalid", name)


def load_run_options(path: Path) -> RunOptions:
    doc = read_input(path.resolve().parent, path.name)
    raw = object_fields(
        doc.data,
        {"format", "benchmark_digest", "variants", "model", "execution", "trials", "comparison"},
        "run-plan",
    )
    if type(raw["format"]) is not int or raw["format"] != 1:
        raise BenchmarkManifestError("evaluation-version-unsupported", "run-plan")
    text_field(raw["benchmark_digest"], "benchmark_digest")
    if type(raw["variants"]) is not list or len(raw["variants"]) not in (1, 2):
        raise BenchmarkManifestError("evaluation-stage-unsupported", "variants")
    paired = len(raw["variants"]) == 2
    variants = []
    for index, entry in enumerate(raw["variants"]):
        variant = object_fields(entry, {"variant_id", "role", "source"}, "variant")
        identifier = text_field(variant["variant_id"], "variant_id")
        role = ("baseline", "candidate")[index] if paired else "current"
        if variant["role"] != role or identifier in {v.variant_id for v in variants}:
            raise BenchmarkManifestError("evaluation-manifest-invalid", "variant")
        patch = None
        if variant["source"] != "current":
            if role != "candidate":
                raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "source")
            patch = referenced_input(doc.root, variant["source"])
        variants.append(VariantSpec(identifier, role, patch))
    if paired:
        comparison_policy(raw["comparison"])
    elif raw["comparison"] is not None:
        raise BenchmarkManifestError("evaluation-stage-unsupported", "comparison")
    model = object_fields(raw["model"], MODEL_FIELDS, "model")
    for name in ("provider", "model", "api_key_env"):
        text_field(model[name], name)
    for name in ("script", "base_url"):
        if model[name] is not None:
            text_field(model[name], name)
    retry = object_fields(model["retry_policy"], RETRY_FIELDS, "retry")
    try:
        ModelRetryPolicy(**retry)
    except (TypeError, ValueError):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "retry") from None
    execution_fields = {"sandbox_config", "max_trials", "timeout_seconds"}
    if paired:
        execution_fields |= {"network_mode", "shutdown_seconds"}
    execution = object_fields(raw["execution"], execution_fields, "execution")
    if paired:
        from math import isfinite

        grace = execution["shutdown_seconds"]
        if (
            execution["network_mode"] != "direct"
            or type(grace) not in (int, float)
            or not isfinite(grace)
            or grace <= 0
            or execution["max_trials"] is None
            or execution["timeout_seconds"] is None
        ):
            raise BenchmarkManifestError("evaluation-manifest-invalid", "paired-execution")
    if execution["sandbox_config"] is not None:
        text_field(execution["sandbox_config"], "sandbox_config")
    fields = {"repetitions"}
    if type(raw["trials"]) is dict and "selection" in raw["trials"]:
        fields.add("selection")
    trials = object_fields(raw["trials"], fields, "trials")
    selection = trials.get("selection")
    if "selection" in fields:
        object_fields(selection, {"case_ids", "material_seeds"}, "selection")
        if any(type(selection[name]) is not list for name in selection):
            raise BenchmarkManifestError("evaluation-manifest-invalid", "selection")
    return RunOptions(
        trials["repetitions"],
        execution["max_trials"],
        execution["timeout_seconds"],
        variants[0].variant_id,
        doc,
        None if selection is None else tuple(selection["case_ids"]),
        None if selection is None else tuple(selection["material_seeds"]),
        tuple(variants) if paired else (),
    )


def configure_cli_plan(args):
    """Run before environment defaults; never load a key here."""
    if getattr(args, "run_plan", None) is None:
        return
    options = load_run_options(args.run_plan)
    raw = options.document.data
    fields = (
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "script",
        "sandbox_config",
        "repetitions",
        "max_trials",
        "eval_timeout_seconds",
    )
    fields += tuple(_retry_attribute(field) for field in RETRY_FIELDS)
    if any(getattr(args, name, None) is not None for name in fields):
        raise BenchmarkManifestError("evaluation-run-plan-conflict", "cli")
    for name, value in raw["model"].items():
        if name != "retry_policy":
            setattr(args, name, value)
    for name, value in raw["model"]["retry_policy"].items():
        setattr(args, _retry_attribute(name), value)
    args.sandbox_config = raw["execution"]["sandbox_config"]
    for name in ("script", "sandbox_config"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, (options.document.root / value).resolve())
    args._evaluation_options = options
