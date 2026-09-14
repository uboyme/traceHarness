"""The one current readonly work envelope shared by its writer and readers."""

import json

from traceh.api.json_types import fingerprint
from traceh.supervision.authority import AgentToolBindingError

INVESTIGATOR_ROLE = "investigator"
PATCH_AUTHOR_ROLE = "patch_author"
ASSIGNMENT_ROLES = (INVESTIGATOR_ROLE, PATCH_AUTHOR_ROLE)
ASSIGNMENT_ID_MAX_CHARS = 128
# One plan carries one batch of direct assistants. The real ceiling is the main
# account's max_children; this only bounds what a single call may even propose.
MAX_ASSIGNMENTS = 8
MAIN_LIMITS = {"goal": 4000, "deliverable": 2000, "uses_child_report": 2000}
CHILD_LIMITS = {
    "goal": 4000,
    "scope": 2000,
    "exclusions": 2000,
    "deliverable": 2000,
    "briefing": 8000,
}


def text_properties(limits):
    return {
        key: {"type": "string", "minLength": 1, "maxLength": limit} for key, limit in limits.items()
    }


def object_schema(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def work_properties():
    return {
        **text_properties(CHILD_LIMITS),
        "main_work": object_schema(text_properties(MAIN_LIMITS)),
    }


def validate_text_fields(value, limits):
    if type(value) is not dict or set(value) != set(limits):
        raise ValueError("collaboration-work-fields-invalid")
    for key, limit in limits.items():
        text = value[key]
        if type(text) is not str or not text.strip() or len(text) > limit:
            raise ValueError("collaboration-work-text-invalid")


def validate_work_fields(value):
    if type(value) is not dict or set(value) != {*CHILD_LIMITS, "main_work"}:
        raise ValueError("collaboration-work-fields-invalid")
    validate_text_fields({k: value[k] for k in CHILD_LIMITS}, CHILD_LIMITS)
    validate_text_fields(value["main_work"], MAIN_LIMITS)


def require_assignment_id(value):
    """A plan-local correlation key recorded by the host; never a permission."""
    if type(value) is not str or not value.strip() or len(value) > ASSIGNMENT_ID_MAX_CHARS:
        raise ValueError("collaboration-assignment-id-invalid")
    if any(character.isspace() or not character.isprintable() for character in value):
        raise ValueError("collaboration-assignment-id-invalid")
    return value


def read_investigation_work(content):
    try:
        work = json.loads(content)
        if type(work) is not dict or set(work) != {
            "format",
            "kind",
            "assignment_id",
            "role",
            "owner_agent_id",
            "source_id",
            "revision",
            "input_digest",
            *CHILD_LIMITS,
            "main_work",
        }:
            raise ValueError("unexpected fields")
        if type(work["format"]) is not int or work["format"] != 3:
            raise ValueError("unsupported work format")
        require_assignment_id(work["assignment_id"])
        if work["role"] != INVESTIGATOR_ROLE:
            raise ValueError("unexpected readonly role")
        if work["kind"] != "readonly-investigation":
            raise ValueError("unexpected work kind")
        validate_work_fields({k: work[k] for k in (*CHILD_LIMITS, "main_work")})
        for key in ("owner_agent_id", "source_id", "revision"):
            if type(work[key]) is not str or not work[key].strip():
                raise ValueError("missing identity")
        body = {k: v for k, v in work.items() if k != "input_digest"}
        if work["input_digest"] != fingerprint(body):
            raise ValueError("work digest mismatch")
    except (ValueError, TypeError, KeyError) as error:
        raise AgentToolBindingError("investigation work format or digest invalid") from error
    return work


def validate_investigation_work(content, binding, owner_id):
    work = read_investigation_work(content)
    if (
        work["owner_agent_id"] != owner_id
        or work["source_id"] != binding.source_id
        or work["revision"] != binding.revision
    ):
        raise AgentToolBindingError("investigation input does not match its frozen source")
    return work
