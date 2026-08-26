"""The append-only vocabulary of one ProductTask.

Every payload here is built from the frozen values F0 defined, never from loose
keyword arguments a caller assembled itself. ``product/task-opened`` is built
from one Proposal and one Confirmation, and ``product/task-started`` from one
Assembly Receipt through :func:`product_started_values`, so a fact cannot
half-describe one binding and half-describe another - the mixing is not
expressible rather than merely rejected.

Reading is the mirror image. The header check validates what every fact shares
before any payload is touched, and normalises anything the store hands back that
this domain does not own: an ``Exception`` becomes a stable protocol error,
while `KeyboardInterrupt` and `SystemExit` are not answers about a payload and
reach the caller unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType

from traceh.agents.identity import is_agent_identifier
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.product import (
    PRODUCT_TASK_ABANDONED,
    PRODUCT_TASK_AWAITING,
    PRODUCT_TASK_CANCELLED,
    PRODUCT_TASK_COMPLETED,
    PRODUCT_TASK_EVENT_TYPES,
    PRODUCT_TASK_FAILED,
    PRODUCT_TASK_OPENED,
    PRODUCT_TASK_PROTOCOL_VERSION,
    PRODUCT_TASK_REJECTED,
    PRODUCT_TASK_ROUTED,
    PRODUCT_TASK_SCHEMA_VERSION,
    PRODUCT_TASK_STARTED,
    PRODUCT_TASK_STREAM_PREFIX,
    ProductAssemblyReceipt,
    ProductPreflightBinding,
    ProductTaskProposal,
    ProposalConfirmation,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
    TaskRouting,
    product_event_contract,
    product_started_values,
)
from traceh.product.errors import ProductInputError, ProductProtocolError
from traceh.promotion.models import require_target_ref

MAX_REASON_DISPLAY_CHARS = 256
"""Bound on the one display-only string the protocol admits.

It exists so a router's prose cannot become unbounded durable history. It is
also single-line safe, because a value that is eventually rendered must not be
able to forge a line or hide inside a bidirectional override.
"""


@dataclass(frozen=True, slots=True)
class NormalizedTaskOpening:
    """One opening input detached once for payload, policy and evidence."""

    data: dict[str, JsonValue]
    proposal: ProductTaskProposal
    confirmation: ProposalConfirmation


@dataclass(frozen=True, slots=True)
class ParsedProductEvent:
    """An untrusted envelope converted to domain-owned built-in values."""

    event_type: str
    data: Mapping[str, JsonValue]
    recorded_at: datetime
    seq: int


def product_task_stream(task_id: str) -> str:
    """The one stream this task's facts live on."""

    return f"{PRODUCT_TASK_STREAM_PREFIX}{require_product_identifier(task_id, field='task_id')}"


def task_id_from_stream(stream_id: object) -> str:
    """Recover the task id a stream name encodes, or refuse the name."""

    if type(stream_id) is not str or not stream_id.startswith(
        PRODUCT_TASK_STREAM_PREFIX
    ):
        raise ProductInputError("product-stream-invalid", "stream_id")
    return require_product_identifier(
        stream_id[len(PRODUCT_TASK_STREAM_PREFIX) :], field="task_id"
    )


def require_product_identifier(value: object, *, field: str) -> str:
    """Reuse the one repo-wide identifier rule, reported in this domain's terms.

    Calling the built-in ``str.__str__`` descriptor and re-checking is what
    stops a ``str`` subclass with hostile comparison methods or a stateful
    ``__str__`` from being stored and compared later as something other than
    what was validated. Ordinary ``str(value)`` is not a normalization boundary:
    it executes caller-controlled code.
    """

    try:
        normalized = str.__str__(value) if isinstance(value, str) else ""
    except Exception:
        normalized = ""
    if type(normalized) is not str or not is_agent_identifier(normalized):
        raise ProductInputError("product-identity-invalid", field)
    return normalized


def require_hex_digest(value: object, *, lengths: tuple[int, ...], field: str) -> str:
    if type(value) is not str or len(value) not in lengths:
        raise ProductInputError(f"product-{field}-invalid", field)
    if value != value.lower() or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ProductInputError(f"product-{field}-invalid", field)
    return value


def require_display_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or value != value.strip():
        raise ProductInputError(f"product-{field}-invalid", field)
    if len(value) > MAX_REASON_DISPLAY_CHARS or not is_agent_identifier(value):
        raise ProductInputError(f"product-{field}-invalid", field)
    return str(value)


# ------------------------------------------------------------------- writing


def task_opened_data(
    *,
    task_id: str,
    operation_id: str,
    proposal: ProductTaskProposal,
    confirmation: ProposalConfirmation,
) -> dict[str, JsonValue]:
    """The opening fact, built from the offer and the acceptance themselves.

    Taking the two values rather than thirteen keywords is what makes it
    impossible to record one Proposal's requirement beside another's preflight,
    or a confirmation that belongs to neither.
    """

    return normalize_task_opening(
        task_id=task_id,
        operation_id=operation_id,
        proposal=proposal,
        confirmation=confirmation,
    ).data


def normalize_task_opening(
    *,
    task_id: str,
    operation_id: str,
    proposal: ProductTaskProposal,
    confirmation: ProposalConfirmation,
) -> NormalizedTaskOpening:
    """Normalize once before authorization, evidence lookup or persistence."""

    if type(proposal) is not ProductTaskProposal:
        raise ProductInputError("product-proposal-invalid", "proposal")
    if type(confirmation) is not ProposalConfirmation:
        raise ProductInputError("product-confirmation-invalid", "confirmation")
    normalized_proposal = replace(
        proposal,
        proposal_id=require_product_identifier(
            proposal.proposal_id, field="proposal_id"
        ),
        origin_session_id=require_product_identifier(
            proposal.origin_session_id, field="origin_session_id"
        ),
        origin_turn_id=require_product_identifier(
            proposal.origin_turn_id, field="origin_turn_id"
        ),
        origin_message_id=require_product_identifier(
            proposal.origin_message_id, field="origin_message_id"
        ),
        proposed_turn_id=require_product_identifier(
            proposal.proposed_turn_id, field="proposed_turn_id"
        ),
    )
    normalized_confirmation = replace(
        confirmation,
        proposal_id=require_product_identifier(
            confirmation.proposal_id, field="proposal_id"
        ),
        confirming_session_id=require_product_identifier(
            confirmation.confirming_session_id,
            field="confirmation_session_id",
        ),
        confirming_turn_id=require_product_identifier(
            confirmation.confirming_turn_id, field="confirmation_turn_id"
        ),
        confirming_message_id=require_product_identifier(
            confirmation.confirming_message_id,
            field="confirmation_message_id",
        ),
    )
    preflight_digest, _ = _validated_preflight(normalized_proposal.preflight)
    data: dict[str, JsonValue] = {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(
            operation_id, field="operation_id"
        ),
        "origin_session_id": normalized_proposal.origin_session_id,
        "origin_turn_id": normalized_proposal.origin_turn_id,
        "origin_message_id": normalized_proposal.origin_message_id,
        "confirmation_session_id": normalized_confirmation.confirming_session_id,
        "confirmation_turn_id": normalized_confirmation.confirming_turn_id,
        "confirmation_message_id": normalized_confirmation.confirming_message_id,
        "requirement_digest": require_hex_digest(
            normalized_proposal.requirement_digest,
            lengths=(64,),
            field="requirement-digest",
        ),
        "profile_digest": require_hex_digest(
            normalized_proposal.preflight.profile_digest,
            lengths=(64,),
            field="profile-digest",
        ),
        "preflight_digest": preflight_digest,
        "requested_mode": _mode_value(normalized_proposal.requested_mode),
        "mode_source": _enum_value(normalized_proposal.mode_source, "mode_source"),
        "product_protocol_version": PRODUCT_TASK_PROTOCOL_VERSION,
    }
    return NormalizedTaskOpening(
        data=data,
        proposal=normalized_proposal,
        confirmation=normalized_confirmation,
    )


def task_routed_data(
    *,
    task_id: str,
    operation_id: str,
    routing: TaskRouting,
    router_agent_id: str,
    routing_session_id: str,
) -> dict[str, JsonValue]:
    if type(routing) is not TaskRouting:
        raise ProductInputError("product-routing-invalid", "routing")
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "router_agent_id": require_product_identifier(
            router_agent_id, field="router_agent_id"
        ),
        "routing_session_id": require_product_identifier(
            routing_session_id, field="routing_session_id"
        ),
        "resolved_mode": _resolved_value(routing.resolved_mode),
        "reason_display": require_display_text(
            routing.reason_display, field="reason-display"
        ),
    }


def task_started_data(
    *, task_id: str, operation_id: str, receipt: ProductAssemblyReceipt
) -> dict[str, JsonValue]:
    """Every started value comes from one Receipt, through the F0 derivation."""

    if type(receipt) is not ProductAssemblyReceipt:
        raise ProductInputError("product-receipt-invalid", "receipt")
    _validated_receipt(receipt)
    task_id = require_product_identifier(task_id, field="task_id")
    payload: dict[str, JsonValue] = {
        "task_id": task_id,
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
    }
    payload.update(product_started_values(task_id=task_id, receipt=receipt))
    return payload


def _validated_preflight(binding: object) -> tuple[str, str]:
    """Validate every field before its digest can authorize a durable fact."""

    if type(binding) is not ProductPreflightBinding:
        raise ProductInputError("product-preflight-invalid", "preflight")
    fields = (
        ("profile_digest", binding.profile_digest, (64,)),
        ("role_assembly_digest", binding.role_assembly_digest, (64,)),
        ("router_assembly_digest", binding.router_assembly_digest, (64,)),
        ("repository_fingerprint", binding.repository_fingerprint, (64,)),
        ("base_revision", binding.base_revision, (40, 64)),
        ("verification_plan_digest", binding.verification_plan_digest, (64,)),
        (
            "promotion_target_fingerprint",
            binding.promotion_target_fingerprint,
            (64,),
        ),
        (
            "promotion_expected_revision",
            binding.promotion_expected_revision,
            (40, 64),
        ),
    )
    for field, value, lengths in fields:
        require_hex_digest(value, lengths=lengths, field=field.replace("_", "-"))
    try:
        require_target_ref(binding.promotion_target_ref)
    except Exception:
        raise ProductInputError("product-preflight-invalid", "preflight") from None
    try:
        digest = binding.digest
    except Exception:
        raise ProductInputError("product-preflight-invalid", "preflight") from None
    return require_hex_digest(
        digest, lengths=(64,), field="preflight-digest"
    ), binding.base_revision


def _validated_receipt(receipt: ProductAssemblyReceipt) -> None:
    if type(receipt.resolved_mode) is not ResolvedTaskMode:
        raise ProductInputError("product-resolved-mode-invalid", "resolved_mode")
    _validated_preflight(receipt.preflight)
    require_hex_digest(
        receipt.workflow_definition_hash,
        lengths=(64,),
        field="definition-hash",
    )
    try:
        digest = receipt.digest
    except Exception:
        raise ProductInputError("product-receipt-invalid", "receipt") from None
    require_hex_digest(digest, lengths=(64,), field="assembly-digest")


def task_awaiting_data(
    *, task_id: str, operation_id: str, review_id: str
) -> dict[str, JsonValue]:
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "review_id": require_product_identifier(review_id, field="review_id"),
    }


def task_completed_data(
    *, task_id: str, operation_id: str, promotion_id: str
) -> dict[str, JsonValue]:
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "promotion_id": require_product_identifier(
            promotion_id, field="promotion_id"
        ),
    }


def task_rejected_data(
    *, task_id: str, operation_id: str, review_id: str
) -> dict[str, JsonValue]:
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "review_id": require_product_identifier(review_id, field="review_id"),
    }


def task_cancelled_data(
    *, task_id: str, operation_id: str, reason_code: str
) -> dict[str, JsonValue]:
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "reason_code": require_product_identifier(reason_code, field="reason_code"),
    }


def task_failed_data(
    *, task_id: str, operation_id: str, failure_code: str
) -> dict[str, JsonValue]:
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "failure_code": require_product_identifier(failure_code, field="failure_code"),
    }


def task_abandoned_data(
    *, task_id: str, operation_id: str, reason_code: str
) -> dict[str, JsonValue]:
    return {
        "task_id": require_product_identifier(task_id, field="task_id"),
        "operation_id": require_product_identifier(operation_id, field="operation_id"),
        "reason_code": require_product_identifier(reason_code, field="reason_code"),
    }


# ------------------------------------------------------------------- reading


def product_event_header(
    event: EventEnvelope, stream_id: str
) -> tuple[str, dict[str, JsonValue], datetime, int]:
    """Validate what every ProductTask fact shares before any payload is read."""

    try:
        return _product_event_header(event, stream_id)
    except ProductProtocolError:
        raise
    except Exception:
        raise ProductProtocolError("product-payload-invalid", _safe_seq(event)) from None


def _product_event_header(
    event: EventEnvelope, stream_id: str
) -> tuple[str, dict[str, JsonValue], datetime, int]:
    if type(event.stream_id) is not str or event.stream_id != stream_id:
        raise ProductProtocolError("product-stream-unexpected", _safe_seq(event))
    seq = event.seq
    if type(seq) is not int or seq < 1:
        raise ProductProtocolError("product-sequence-invalid", 0)
    if (
        type(event.schema_version) is not int
        or event.schema_version != PRODUCT_TASK_SCHEMA_VERSION
    ):
        raise ProductProtocolError("product-schema-version-unsupported", seq)
    if type(event.type) is not str or event.type not in PRODUCT_TASK_EVENT_TYPES:
        raise ProductProtocolError("product-event-type-unknown", seq)
    if type(event.occurred_at) is not datetime or event.occurred_at.tzinfo is None:
        raise ProductProtocolError("product-recorded-at-invalid", seq)
    data = event.data
    if type(data) is not dict:
        raise ProductProtocolError("product-payload-invalid", seq)
    return str(event.type), data, event.occurred_at.astimezone(UTC), seq


def require_exact_keys(
    event_type: str, data: dict[str, JsonValue], seq: int
) -> None:
    contract = product_event_contract(event_type)
    if contract is None:
        raise ProductProtocolError("product-event-type-unknown", seq)
    try:
        keys = tuple(data)
    except Exception:
        raise ProductProtocolError("product-payload-keys-unexpected", seq) from None
    if any(type(key) is not str for key in keys) or frozenset(keys) != contract.keys:
        raise ProductProtocolError("product-payload-keys-unexpected", seq)


def parse_product_event(event: EventEnvelope, stream_id: str) -> ParsedProductEvent:
    """Validate and detach one envelope before any domain comparison."""

    event_type, raw, recorded_at, seq = product_event_header(event, stream_id)
    try:
        require_exact_keys(event_type, raw, seq)
        data = _normalized_product_payload(event_type, raw, seq)
    except ProductProtocolError:
        raise
    except Exception:
        raise ProductProtocolError("product-payload-invalid", seq) from None
    return ParsedProductEvent(
        event_type, MappingProxyType(data), recorded_at, seq
    )


def _normalized_product_payload(
    event_type: str, data: dict[str, JsonValue], seq: int
) -> dict[str, JsonValue]:
    normalized: dict[str, JsonValue] = {
        "task_id": protocol_identifier(data.get("task_id"), seq),
        "operation_id": protocol_identifier(data.get("operation_id"), seq),
    }
    if event_type == PRODUCT_TASK_OPENED:
        version = data.get("product_protocol_version")
        if type(version) is not int:
            raise ProductProtocolError("product-protocol-version-unsupported", seq)
        normalized.update(
            {
                "origin_session_id": protocol_identifier(
                    data.get("origin_session_id"), seq
                ),
                "origin_turn_id": protocol_identifier(data.get("origin_turn_id"), seq),
                "origin_message_id": protocol_identifier(
                    data.get("origin_message_id"), seq
                ),
                "confirmation_session_id": protocol_identifier(
                    data.get("confirmation_session_id"), seq
                ),
                "confirmation_turn_id": protocol_identifier(
                    data.get("confirmation_turn_id"), seq
                ),
                "confirmation_message_id": protocol_identifier(
                    data.get("confirmation_message_id"), seq
                ),
                "requirement_digest": protocol_digest(
                    data.get("requirement_digest"), lengths=(64,), seq=seq
                ),
                "profile_digest": protocol_digest(
                    data.get("profile_digest"), lengths=(64,), seq=seq
                ),
                "preflight_digest": protocol_digest(
                    data.get("preflight_digest"), lengths=(64,), seq=seq
                ),
                "requested_mode": _protocol_enum_value(
                    RequestedTaskMode, data.get("requested_mode"), seq
                ),
                "mode_source": _protocol_enum_value(
                    TaskModeSource, data.get("mode_source"), seq
                ),
                "product_protocol_version": version,
            }
        )
    elif event_type == PRODUCT_TASK_ROUTED:
        normalized.update(
            {
                "router_agent_id": protocol_identifier(
                    data.get("router_agent_id"), seq
                ),
                "routing_session_id": protocol_identifier(
                    data.get("routing_session_id"), seq
                ),
                "resolved_mode": _protocol_enum_value(
                    ResolvedTaskMode, data.get("resolved_mode"), seq
                ),
                "reason_display": protocol_display_text(
                    data.get("reason_display"), seq
                ),
            }
        )
    elif event_type == PRODUCT_TASK_STARTED:
        normalized.update(
            {
                "mode": _protocol_enum_value(
                    ResolvedTaskMode, data.get("mode"), seq
                ),
                "workflow_run_id": protocol_identifier(
                    data.get("workflow_run_id"), seq
                ),
                "definition_hash": protocol_digest(
                    data.get("definition_hash"), lengths=(64,), seq=seq
                ),
                "assembly_digest": protocol_digest(
                    data.get("assembly_digest"), lengths=(64,), seq=seq
                ),
                "preflight_digest": protocol_digest(
                    data.get("preflight_digest"), lengths=(64,), seq=seq
                ),
                "source_base_revision": protocol_digest(
                    data.get("source_base_revision"), lengths=(40, 64), seq=seq
                ),
            }
        )
    elif event_type in (PRODUCT_TASK_AWAITING, PRODUCT_TASK_REJECTED):
        normalized["review_id"] = protocol_identifier(data.get("review_id"), seq)
    elif event_type == PRODUCT_TASK_COMPLETED:
        normalized["promotion_id"] = protocol_identifier(
            data.get("promotion_id"), seq
        )
    elif event_type in (PRODUCT_TASK_CANCELLED, PRODUCT_TASK_ABANDONED):
        normalized["reason_code"] = protocol_identifier(
            data.get("reason_code"), seq
        )
    elif event_type == PRODUCT_TASK_FAILED:
        normalized["failure_code"] = protocol_identifier(
            data.get("failure_code"), seq
        )
    else:  # pragma: no cover - the header rejects unknown types
        raise ProductProtocolError("product-event-type-unknown", seq)
    return normalized


def _protocol_enum_value[T](enum: type[T], value: object, seq: int) -> str:
    if type(value) is not str:
        raise ProductProtocolError("product-enum-invalid", seq)
    try:
        member = enum(value)  # type: ignore[call-arg]
    except ValueError:
        raise ProductProtocolError("product-enum-invalid", seq) from None
    return str(member.value)  # type: ignore[attr-defined]


def protocol_identifier(value: object, seq: int) -> str:
    try:
        return require_product_identifier(value, field="identity")
    except ProductInputError:
        raise ProductProtocolError("product-identity-invalid", seq) from None


def protocol_digest(value: object, *, lengths: tuple[int, ...], seq: int) -> str:
    try:
        return require_hex_digest(value, lengths=lengths, field="digest")
    except ProductInputError:
        raise ProductProtocolError("product-digest-invalid", seq) from None


def protocol_display_text(value: object, seq: int) -> str | None:
    try:
        return require_display_text(value, field="reason-display")
    except ProductInputError:
        raise ProductProtocolError("product-reason-display-invalid", seq) from None


def is_product_fact(
    event: EventEnvelope,
    stream_id: str,
    event_type: str,
    data: dict[str, JsonValue],
) -> bool:
    """Whether ``event`` is exactly the fact a failed append tried to write."""

    try:
        parsed = parse_product_event(event, stream_id)
    except ProductProtocolError:
        return False
    if parsed.event_type != event_type:
        return False
    # An encoding failure is unknowable and intentionally propagates to the
    # shared reconciler, which maps it to ``None`` rather than false absence.
    require_exact_keys(event_type, data, parsed.seq)
    expected = _normalized_product_payload(event_type, data, parsed.seq)
    return canonical_json(parsed.data) == canonical_json(expected)


def _mode_value(value: object) -> str:
    from traceh.api.product import RequestedTaskMode

    if type(value) is not RequestedTaskMode:
        raise ProductInputError("product-requested-mode-invalid", "requested_mode")
    return value.value


def _resolved_value(value: object) -> str:
    from traceh.api.product import ResolvedTaskMode

    if type(value) is not ResolvedTaskMode:
        raise ProductInputError("product-resolved-mode-invalid", "resolved_mode")
    return value.value


def _enum_value(value: object, field: str) -> str:
    from traceh.api.product import TaskModeSource

    if type(value) is not TaskModeSource:
        raise ProductInputError(f"product-{field}-invalid", field)
    return value.value


def _safe_seq(event: object) -> int:
    try:
        seq = event.seq  # type: ignore[attr-defined]
    except Exception:
        return 0
    return seq if type(seq) is int and seq >= 0 else 0


__all__ = [
    "MAX_REASON_DISPLAY_CHARS",
    "NormalizedTaskOpening",
    "ParsedProductEvent",
    "is_product_fact",
    "normalize_task_opening",
    "parse_product_event",
    "product_event_header",
    "product_task_stream",
    "protocol_digest",
    "protocol_display_text",
    "protocol_identifier",
    "require_display_text",
    "require_exact_keys",
    "require_hex_digest",
    "require_product_identifier",
    "task_abandoned_data",
    "task_awaiting_data",
    "task_cancelled_data",
    "task_completed_data",
    "task_failed_data",
    "task_id_from_stream",
    "task_opened_data",
    "task_rejected_data",
    "task_routed_data",
    "task_started_data",
]
