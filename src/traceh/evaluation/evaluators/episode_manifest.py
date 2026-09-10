"""Host-owned, explicit episode materials; no model-controlled preparation callbacks."""

from dataclasses import dataclass
from pathlib import PurePosixPath

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.memory import MemoryPolicy, ProjectScopeLimits
from traceh.api.skills import SkillContribution, SkillDescriptor, SkillLimits, SkillSectionContent
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import object_fields, referenced_input, text_field
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.session.context_input import ContextInputPolicy

FAMILIES = frozenset({"history", "skill", "memory", "output"})


def invalid(field):
    raise BenchmarkManifestError("evaluation-manifest-invalid", field)


def positive(value, field):
    if type(value) is not int or value < 1:
        invalid(field)
    return value


def relative_file(value):
    text_field(value, "relative_file")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or any(c in value for c in "\\:\x00")
    ):
        invalid("relative_file")
    return value


def skill_contribution(raw):
    descriptor = SkillDescriptor.from_dict(raw["descriptor"])
    sections = tuple(
        SkillSectionContent(**object_fields(s, {"section_id", "body"}, "section"))
        for s in raw["sections"]
    )
    return SkillContribution(descriptor, sections)


@dataclass(frozen=True)
class EpisodeCase:
    encoded: str

    @property
    def data(self):
        import json

        return json.loads(self.encoded)

    @property
    def digest(self):
        return fingerprint(self.data)


@dataclass(frozen=True)
class EpisodeSuite:
    cases: tuple[EpisodeCase, ...]
    files: tuple


def load_episode_suite(manifest):
    try:
        return _load(manifest)
    except (TypeError, ValueError, KeyError) as error:
        if isinstance(error, BenchmarkManifestError):
            raise
        raise BenchmarkManifestError("evaluation-manifest-invalid", "episode") from None


def _load(manifest):
    settings = object_fields(
        manifest.task_settings,
        {
            "setup_version",
            "scope_profile",
            "max_cases",
            "runtime",
            "contexts",
            "skill_limits",
            "memory_policy",
            "project_limits",
        },
        "episode-settings",
    )
    if type(settings["setup_version"]) is not int or settings["setup_version"] != 1:
        invalid("setup_version")
    if settings["scope_profile"] != "source-isolated":
        invalid("scope_profile")
    runtime = object_fields(
        settings["runtime"],
        {
            "max_steps",
            "max_output_tokens",
            "temperature",
            "max_tool_output_chars",
            "tool_timeout_seconds",
            "token_budget",
        },
        "runtime",
    )
    for name in ("max_steps", "max_output_tokens", "max_tool_output_chars"):
        positive(runtime[name], name)
    if (
        type(runtime["tool_timeout_seconds"]) not in (int, float)
        or runtime["tool_timeout_seconds"] <= 0
    ):
        invalid("tool_timeout_seconds")
    if runtime["temperature"] is not None and type(runtime["temperature"]) not in (int, float):
        invalid("temperature")
    TokenBudgetPolicy(**runtime["token_budget"])
    contexts = object_fields(settings["contexts"], FAMILIES, "contexts")
    for family, value in contexts.items():
        policy = ContextInputPolicy.from_dict(value)
        if (
            (policy.skills is not None) != (family == "skill")
            or (policy.memory is not None) != (family == "memory")
            or (policy.history is not None) != (family in {"history", "output"})
        ):
            invalid("source-context")
    SkillLimits(**settings["skill_limits"])
    MemoryPolicy(
        **{
            **settings["memory_policy"],
            "denied_patterns": tuple(settings["memory_policy"]["denied_patterns"]),
        }
    )
    ProjectScopeLimits(**settings["project_limits"])
    assessment = object_fields(
        manifest.assessment, {"scorer_id", "version", "rubric", "requires_review"}, "assessment"
    )
    if (
        assessment["scorer_id"] != "retrieval-evidence-v1"
        or type(assessment["version"]) is not int
        or assessment["version"] != 1
        or assessment["requires_review"] is not True
    ):
        invalid("assessment")
    rubric = referenced_input(manifest.directory, assessment["rubric"])
    rubric_data = object_fields(rubric.data, {"format", "instructions"}, "rubric")
    if type(rubric_data["format"]) is not int or rubric_data["format"] != 1:
        invalid("rubric")
    text_field(rubric_data["instructions"], "rubric.instructions")
    data = object_fields(manifest.dataset.data, {"format", "purpose", "cases"}, "dataset")
    if (
        data["purpose"] != "development-regression"
        or type(data["format"]) is not int
        or data["format"] != 1
    ):
        invalid("dataset")
    if type(data["cases"]) is not list or not 1 <= len(data["cases"]) <= positive(
        settings["max_cases"], "max_cases"
    ):
        invalid("cases")
    files, cases, identities = [rubric], [], set()
    for raw in data["cases"]:
        case = object_fields(
            raw,
            {"case_id", "group_id", "family", "material_seed", "question", "setup", "expectation"},
            "case",
        )
        for name in ("case_id", "group_id", "question"):
            text_field(case[name], name)
        if type(case["material_seed"]) is not int or case["family"] not in FAMILIES:
            invalid("case-identity")
        identity = (case["case_id"], case["material_seed"])
        if identity in identities:
            invalid("duplicate-case-material")
        identities.add(identity)
        expect = object_fields(
            case["expectation"],
            {"kind", "value", "value_type", "source_text", "reference_id"},
            "expectation",
        )
        if expect["kind"] not in {"value", "no-evidence"} or expect["value_type"] not in {
            "code",
            "number",
            "location",
            "none",
        }:
            invalid("expectation")
        for name in ("value", "source_text", "reference_id"):
            if type(expect[name]) is not str:
                invalid(name)
        if expect["kind"] == "value" and (
            not expect["value"] or expect["value"] not in expect["source_text"]
        ):
            invalid("expected-source")
        setup, family = case["setup"], case["family"]
        if family == "history":
            object_fields(setup, {"turns", "prompt_prefix", "summary"}, "history")
            if type(setup["turns"]) is not list or not setup["turns"]:
                invalid("history-turns")
            for value in (*setup["turns"], setup["prompt_prefix"], setup["summary"]):
                text_field(value, "history-text")
        elif family == "skill":
            object_fields(setup, {"descriptor", "sections", "resources"}, "skill")
            contribution = skill_contribution(setup)
            resources = setup["resources"]
            if type(resources) is not list or len(resources) != len(
                contribution.descriptor.resources
            ):
                invalid("skill-resources")
            for descriptor, resource in zip(
                contribution.descriptor.resources, resources, strict=True
            ):
                value = referenced_input(manifest.directory, resource)
                if (
                    value.sha256 != descriptor.content_digest
                    or len(value.content) != descriptor.content_bytes
                ):
                    invalid("skill-resource-digest")
                files.append(value)
        elif family == "memory":
            object_fields(
                setup,
                {"project_id", "source_id", "label", "items", "revoke_id", "second_session"},
                "memory",
            )
            for name in ("project_id", "source_id", "label"):
                text_field(setup[name], name)
            if type(setup["second_session"]) is not bool or type(setup["items"]) is not list:
                invalid("memory")
            ids = set()
            for item in setup["items"]:
                object_fields(
                    item, {"memory_id", "fact_slot", "body", "predecessor_id"}, "memory-item"
                )
                for name in ("memory_id", "fact_slot", "body"):
                    text_field(item[name], name)
                if item["memory_id"] in ids or (
                    item["predecessor_id"] is not None and item["predecessor_id"] not in ids
                ):
                    invalid("memory-identity")
                ids.add(item["memory_id"])
            if setup["revoke_id"] is not None and setup["revoke_id"] not in ids:
                invalid("memory-revoke")
        else:
            object_fields(
                setup,
                {"script", "script_path", "command", "counter_path", "prompt", "exit_code"},
                "output",
            )
            for name in ("script_path", "counter_path"):
                relative_file(setup[name])
            for name in ("command", "prompt"):
                text_field(setup[name], name)
            if type(setup["exit_code"]) is not int:
                invalid("exit_code")
            files.append(referenced_input(manifest.directory, setup["script"]))
        cases.append(EpisodeCase(canonical_json(case)))
    return EpisodeSuite(tuple(cases), tuple(files))
