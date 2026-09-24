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
MODEL_FIELDS = frozenset(
    {"provider", "model", "base_url", "api_key_env", "script", "retry_policy", "timeout_seconds"}
)


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
        value,
        {
            "format",
            "kind",
            "requested_modes",
            "min_pass_gain",
            "max_token_ratio",
            "max_tool_call_delta",
        },
        "comparison",
    )
    if type(value["format"]) is not int or value["format"] != 3:
        raise BenchmarkManifestError("evaluation-version-unsupported", "comparison")
    if value["kind"] not in {"text_candidate", "execution_strategy"}:
        raise BenchmarkManifestError("evaluation-manifest-invalid", "comparison.kind")
    modes = value["requested_modes"]
    if modes is not None and (
        type(modes) is not list
        or len(modes) != 2
        or any(type(mode) is not str or mode not in {"single", "multi"} for mode in modes)
    ):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "comparison.requested_modes")
    if (
        value["kind"] == "execution_strategy"
        and modes is None
        or value["kind"] == "text_candidate"
        and modes is not None
        and modes[0] != modes[1]
    ):
        raise BenchmarkManifestError("evaluation-comparison-incompatible", "requested_modes")
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
    requested_modes: tuple[str, ...] | None = None

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
        for name, cls in (("case_ids", str), ("material_seeds", int), ("requested_modes", str)):
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
        if raw["comparison"]["kind"] == "execution_strategy" and any(
            v.patch is not None for v in variants
        ):
            raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "execution_strategy")
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
    # One model request's wait. It decides whether a long answer arrives or is cut
    # off, so it is frozen like the retry policy instead of coming from whatever
    # environment or default the worker happens to inherit (ADR-0086).
    from math import isfinite

    request_timeout = model["timeout_seconds"]
    if (
        type(request_timeout) not in (int, float)
        or not isfinite(request_timeout)
        or request_timeout <= 0
    ):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "model.timeout_seconds")
    execution_fields = {"sandbox_config", "max_trials", "timeout_seconds"}
    if paired:
        execution_fields |= {"network_mode", "shutdown_seconds", "first_arm"}
    execution = object_fields(raw["execution"], execution_fields, "execution")
    if paired:
        from math import isfinite

        grace = execution["shutdown_seconds"]
        if (
            execution["network_mode"] != "direct"
            or execution["first_arm"] not in {"baseline", "candidate"}
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
    if type(raw["trials"]) is dict and "requested_modes" in raw["trials"]:
        fields.add("requested_modes")
    if type(raw["trials"]) is dict and "selection" in raw["trials"]:
        fields.add("selection")
    trials = object_fields(raw["trials"], fields, "trials")
    modes = trials.get("requested_modes")
    if "requested_modes" in fields and (type(modes) is not list or paired):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "trials.requested_modes")
    selection = trials.get("selection")
    if "selection" in fields:
        object_fields(selection, {"case_ids", "material_seeds"}, "selection")
        if type(selection["case_ids"]) is not list or (
            selection["material_seeds"] is not None
            and type(selection["material_seeds"]) is not list
        ):
            raise BenchmarkManifestError("evaluation-manifest-invalid", "selection")
    return RunOptions(
        trials["repetitions"],
        execution["max_trials"],
        execution["timeout_seconds"],
        variants[0].variant_id,
        doc,
        None if selection is None else tuple(selection["case_ids"]),
        None
        if selection is None or selection["material_seeds"] is None
        else tuple(selection["material_seeds"]),
        tuple(variants) if paired else (),
        None if modes is None else tuple(modes),
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
        "model_timeout_seconds",
    )
    fields += tuple(_retry_attribute(field) for field in RETRY_FIELDS)
    if any(getattr(args, name, None) is not None for name in fields):
        raise BenchmarkManifestError("evaluation-run-plan-conflict", "cli")
    for name, value in raw["model"].items():
        if name == "timeout_seconds":
            args.model_timeout_seconds = value
        elif name != "retry_policy":
            setattr(args, name, value)
    for name, value in raw["model"]["retry_policy"].items():
        setattr(args, _retry_attribute(name), value)
    args.sandbox_config = raw["execution"]["sandbox_config"]
    for name in ("script", "sandbox_config"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, (options.document.root / value).resolve())
    args._evaluation_options = options
