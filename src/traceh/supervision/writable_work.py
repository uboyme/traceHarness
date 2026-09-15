"""Explicit writable work identities; independent of readonly investigation."""

import json
from dataclasses import dataclass

from traceh.api.agents import AgentSpec
from traceh.api.json_types import canonical_json, fingerprint
from traceh.artifacts.manifest import freeze_changed_paths, require_artifact_identifier
from traceh.supervision.authority import AgentToolBindingError
from traceh.supervision.investigation_work import (
    CHILD_LIMITS,
    MAIN_LIMITS,
    PATCH_AUTHOR_ROLE,
    require_assignment_id,
    validate_text_fields,
)


@dataclass(frozen=True, slots=True)
class WritableBinding:
    spec: AgentSpec
    task_id: str
    source_id: str
    revision: str
    assembly_digest: str
    budget_digest: str


def validate_assignment(arguments):
    if type(arguments) is not dict or set(arguments) != {*CHILD_LIMITS, "paths", "main_work"}:
        raise ValueError("writable-work-fields-invalid")
    validate_text_fields({k: arguments[k] for k in CHILD_LIMITS}, CHILD_LIMITS)
    validate_text_fields(arguments["main_work"], MAIN_LIMITS)
    paths = freeze_changed_paths(arguments["paths"], max_paths=128)
    if not paths:
        raise ValueError("writable-work-paths-empty")


def work_content(
    arguments, binding, *, owner_id, child_id, session_id, message_id, assignment_id
):
    validate_assignment(arguments)
    body = {
        "format": 2,
        "kind": "writable-assignment",
        "assignment_id": require_assignment_id(assignment_id),
        "role": PATCH_AUTHOR_ROLE,
        "task_id": binding.task_id,
        "owner_agent_id": owner_id,
        "child_agent_id": child_id,
        "session_id": session_id,
        "message_id": message_id,
        "source_id": binding.source_id,
        "revision": binding.revision,
        "assembly_digest": binding.assembly_digest,
        "budget_digest": binding.budget_digest,
        **arguments,
    }
    return canonical_json({**body, "input_digest": fingerprint(body)})


def read_writable_work(content):
    try:
        work = json.loads(content)
        identity = {
            "assignment_id",
            "task_id",
            "owner_agent_id",
            "child_agent_id",
            "session_id",
            "message_id",
            "source_id",
            "revision",
            "assembly_digest",
            "budget_digest",
        }
        if type(work) is not dict or set(work) != {
            "format",
            "kind",
            "role",
            "input_digest",
            *identity,
            *CHILD_LIMITS,
            "paths",
            "main_work",
        }:
            raise ValueError("unexpected fields")
        if (
            type(work["format"]) is not int
            or work["format"] != 2
            or work["kind"] != "writable-assignment"
            or work["role"] != PATCH_AUTHOR_ROLE
        ):
            raise ValueError("unsupported writable work")
        validate_assignment({k: work[k] for k in (*CHILD_LIMITS, "paths", "main_work")})
        for key in identity:
            require_artifact_identifier(work[key], field=key)
        if work["input_digest"] != fingerprint(
            {k: v for k, v in work.items() if k != "input_digest"}
        ):
            raise ValueError("work digest mismatch")
    except Exception as error:
        raise AgentToolBindingError("writable-work-invalid") from error
    return work


def validate_writable_work(
    content, binding, *, owner_id, child_id, session_id, message_id, assignment_id=None
):
    work = read_writable_work(content)
    expected = {
        "task_id": binding.task_id,
        "source_id": binding.source_id,
        "revision": binding.revision,
        "assembly_digest": binding.assembly_digest,
        "budget_digest": binding.budget_digest,
        "owner_agent_id": owner_id,
        "child_agent_id": child_id,
        "session_id": session_id,
        "message_id": message_id,
        **({} if assignment_id is None else {"assignment_id": assignment_id}),
    }
    if any(work[key] != value for key, value in expected.items()):
        raise AgentToolBindingError("writable-work-binding-mismatch")
    return work
