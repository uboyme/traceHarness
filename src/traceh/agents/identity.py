"""What an Agent identity is, written once for both writers and replay.

The creation transaction and the directory projector must not develop two
readings of the same bytes: if the writer accepted a value the projector later
rejects, an Agent could exist for one call and vanish on replay. Every rule
about what an identifier is, what an ``agent/created`` payload contains and how
that payload becomes an `AgentRecord` lives here, and both sides import it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from traceh.agents.errors import AgentDirectoryProtocolError, AgentIdentityError
from traceh.api.agents import AgentRecord, AgentSpec, Budget
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json, to_json_value
from traceh.cli.text_safety import is_single_line_safe

AGENT_DIRECTORY_STREAM = "agents:directory"
"""The one durable stream of Agent identity facts in an `EventStore`.

It is deliberately separate from ``session:<id>``. A Session Stream is the
execution history of one Agent; this stream is the control plane that says
which Agents exist and which Session each one owns. Mixing them would make
"who exists" depend on reading every Session, and would let one Agent's
execution history assert facts about another Agent.
"""

AGENT_EVENT_SCHEMA_VERSION = 1
"""The only payload shape this projector can read.

A later writer that changes what a field means must raise this, so an old
projector refuses the event instead of reading unfamiliar bytes as a v1
identity. Accepting any version would make "fail closed on unknown protocol"
untrue for exactly the case it exists to cover.
"""

AGENT_CREATED = "agent/created"
"""The only event type this stream carries in Stage A.

Communication (Inbox, delivery, wakeup) is a different relation and will not be
appended here; see :mod:`traceh.agents.directory`.
"""

MAX_IDENTIFIER_LENGTH = 256

MAX_METADATA_DEPTH = 64
"""How deeply a metadata graph may nest, on either side of the boundary.

The bound exists so the failure is *deterministic*. Metadata is caller-supplied
and may be self-referential or simply very deep, and ``to_json_value()`` walks
it recursively - so without a bound the outcome is a bare ``RecursionError``
raised at whatever depth this interpreter, on this platform, in this thread
happens to run out of stack. That escapes the fixed ``agent-metadata-invalid``
outcome and makes the public API's error contract depend on
``sys.getrecursionlimit()``.
"""

MAX_BUDGET_VALUE = 2**53 - 1
"""Largest budget number this protocol accepts, on either side.

A budget is persisted as a JSON number and most JSON readers parse numbers as
IEEE-754 doubles, so anything above the largest exactly representable integer
cannot be guaranteed to survive a round trip. The bound is also what keeps the
error protocol stable: ``10**10000`` is an ``int``, passes ``>= 0``, and then
raises a bare ``OverflowError`` out of ``math.isfinite()`` or ``float()`` -
escaping the fixed ``agent-budget-invalid`` outcome on both the write and the
replay path. Comparing against this bound is pure integer arithmetic and cannot
raise.
"""

_BUDGET_INT_FIELDS = (
    "max_tokens",
    "max_steps",
    "max_tool_calls",
    "max_children",
    "max_depth",
    "max_processes",
)
_BUDGET_FLOAT_FIELDS = ("max_wall_seconds",)

AGENT_CREATED_KEYS = frozenset(
    {
        "agent_id",
        "session_id",
        "request_id",
        "preset",
        "workspace_id",
        "owner_agent_id",
        "forked_from_session_id",
        "capability_grants",
        "budget",
        "metadata",
    }
)
"""Exactly the keys an ``agent/created`` payload carries at schema version 1."""


def is_agent_identifier(value: object) -> bool:
    """Whether ``value`` can be used as a durable Agent-plane identifier.

    Narrow on purpose. An identifier is persisted, compared for uniqueness and
    eventually printed, so:

    * it must be a ``str`` - ``True``, ``1`` and ``None`` are missing
      identities, not values to coerce. ``str(True)`` would mint an Agent
      called ``"True"`` and make two unrelated broken payloads collide;
    * it must not be empty or blank;
    * it must equal its own ``strip()`` - otherwise ``"a"`` and ``"a "`` are two
      identities that read as one;
    * it must be safe on a single line, so an id cannot forge output or hide
      inside a bidirectional override;
    * it must be bounded.
    """

    if not isinstance(value, str):
        return False
    if not value or value != value.strip():
        return False
    if len(value) > MAX_IDENTIFIER_LENGTH:
        return False
    return is_single_line_safe(value)


def require_identifier(value: object, *, field: str) -> str:
    """Validate a caller-supplied identifier before anything is read or written."""

    if not is_agent_identifier(value):
        raise AgentIdentityError("agent-identity-invalid", field)
    assert isinstance(value, str)
    return value


def _read_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if not is_agent_identifier(value):
        raise AgentDirectoryProtocolError("agent-identity-invalid", seq)
    assert isinstance(value, str)
    return value


def _read_optional_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str | None:
    # A missing key and an explicit ``null`` are two different facts, and this
    # never collapses them through ``dict.get()``. A missing key cannot reach
    # here at all - the exact key-set gate in ``parse_agent_created()`` has
    # already rejected it - so ``null`` unambiguously means "this relation does
    # not exist" rather than "the writer omitted it".
    value = data[key]
    if value is None:
        return None
    if not is_agent_identifier(value):
        raise AgentDirectoryProtocolError("agent-identity-invalid", seq)
    assert isinstance(value, str)
    return value


def _read_grants(data: dict[str, JsonValue], seq: int) -> tuple[str, ...]:
    value = data.get("capability_grants")
    if not isinstance(value, list):
        raise AgentDirectoryProtocolError("agent-grants-invalid", seq)
    grants: list[str] = []
    for item in value:
        if not is_agent_identifier(item):
            raise AgentDirectoryProtocolError("agent-grants-invalid", seq)
        assert isinstance(item, str)
        if item in grants:
            raise AgentDirectoryProtocolError("agent-grants-invalid", seq)
        grants.append(item)
    return tuple(grants)


def _budget_numbers(value: object) -> dict[str, int | float] | None:
    """Read a budget mapping, or ``None`` if it does not satisfy the rules.

    This is the **single** definition of what a valid budget is. The creation
    transaction and replay both call it, because a rule the writer applies more
    loosely than the reader is not a weaker check - it is a way to append a
    fact that can never be read back. One `Budget(max_steps=True)` used to
    commit successfully and then fail every subsequent rebuild, permanently
    bricking the directory for that store.
    """

    if not isinstance(value, dict):
        return None
    if set(value) != set(_BUDGET_INT_FIELDS) | set(_BUDGET_FLOAT_FIELDS):
        return None
    numbers: dict[str, int | float] = {}
    for field in _BUDGET_INT_FIELDS:
        raw = value[field]
        # ``bool`` is an ``int`` in Python; a budget of ``True`` is malformed
        # data, not a limit of one. The range check is deliberately made with
        # integer comparisons only, before anything converts to float.
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        if raw < 0 or raw > MAX_BUDGET_VALUE:
            return None
        numbers[field] = raw
    for field in _BUDGET_FLOAT_FIELDS:
        raw = value[field]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return None
        if isinstance(raw, int):
            # Bound first: ``float(10**10000)`` raises, and so does
            # ``math.isfinite()`` on it.
            if raw < 0 or raw > MAX_BUDGET_VALUE:
                return None
            numbers[field] = float(raw)
            continue
        # NaN and the infinities survive ``raw < 0`` and round-trip through
        # Python's non-strict JSON, but they are not JSON and are not budgets.
        if not math.isfinite(raw) or raw < 0 or raw > MAX_BUDGET_VALUE:
            return None
        numbers[field] = raw
    return numbers


def _read_budget(data: dict[str, JsonValue], seq: int) -> Budget:
    numbers = _budget_numbers(data.get("budget"))
    if numbers is None:
        raise AgentDirectoryProtocolError("agent-budget-invalid", seq)
    return Budget(**numbers)  # type: ignore[arg-type]


def _metadata_graph_is_bounded(value: object, depth: int, ancestors: tuple[int, ...]) -> bool:
    """Whether a metadata graph is acyclic and within `MAX_METADATA_DEPTH`.

    Mirrors the container types ``to_json_value()`` descends into. A container
    that reappears among its own ancestors is a cycle, and a cycle is not deep
    data - it has no encoding at all.
    """

    if depth > MAX_METADATA_DEPTH:
        return False
    if isinstance(value, Mapping):
        items: Iterable[object] = value.values()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = value
    else:
        return True
    if id(value) in ancestors:
        return False
    ancestors = (*ancestors, id(value))
    return all(_metadata_graph_is_bounded(item, depth + 1, ancestors) for item in items)


def _normalized_metadata(value: object) -> dict[str, JsonValue] | None:
    """Deep-copy and normalize a metadata graph, or ``None`` if unusable.

    The copy is the point, not a side effect. Metadata is the only mutable part
    of a creation request, so both entrances to this module must take their own
    graph: replay must not keep a reference into the caller's `EventEnvelope`,
    and the creation transaction must not read the caller's live dict again
    after its first ``await``.

    Normalization goes through ``to_json_value()``, so what a payload may hold
    is decided in exactly one place - the same rule `EventStore` applies. A
    value it cannot encode (a ``set``, an arbitrary object) is rejected here
    rather than raising out of the store much later.
    """

    if not isinstance(value, dict):
        return None
    try:
        for key in value:
            if not isinstance(key, str):
                return None
        if not _metadata_graph_is_bounded(value, 0, ()):
            return None
        return {key: to_json_value(item) for key, item in value.items()}
    except Exception:
        # Every step that touches the graph is inside this boundary, including
        # the key scan and the bounded walk. Metadata is caller-supplied, so
        # merely *looking* at it can fail: a ``dict`` subclass whose
        # ``values()`` or ``__iter__`` raises breaks traversal while remaining
        # perfectly encodable. A pre-check outside the boundary leaked that as
        # a bare exception and bypassed the one stable outcome.
        #
        # ``Exception``, deliberately not ``BaseException``: `KeyboardInterrupt`,
        # `SystemExit` and `CancelledError` are not verdicts about the metadata
        # and must reach the caller unchanged - the same rule the creation
        # transaction follows around its append.
        return None


def _read_metadata(data: dict[str, JsonValue], seq: int) -> dict[str, JsonValue]:
    metadata = _normalized_metadata(data.get("metadata"))
    if metadata is None:
        raise AgentDirectoryProtocolError("agent-metadata-invalid", seq)
    return metadata


def validate_spec(spec: AgentSpec) -> None:
    """Reject a specification before it can reach the event log."""

    require_identifier(spec.preset, field="preset")
    require_identifier(spec.workspace_id, field="workspace_id")
    if spec.owner_agent_id is not None:
        require_identifier(spec.owner_agent_id, field="owner_agent_id")
    if spec.forked_from_session_id is not None:
        require_identifier(spec.forked_from_session_id, field="forked_from_session_id")
    if not isinstance(spec.capability_grants, tuple):
        raise AgentIdentityError("agent-grants-invalid", "capability_grants")
    seen: set[str] = set()
    for grant in spec.capability_grants:
        require_identifier(grant, field="capability_grants")
        if grant in seen:
            raise AgentIdentityError("agent-grants-invalid", "capability_grants")
        seen.add(grant)
    # Validated through the same rule replay uses, against the very mapping
    # that would be persisted - not merely "is it a Budget instance". The
    # dataclass accepts any value for its fields, so the type alone proves
    # nothing about what would land in the log.
    if not isinstance(spec.budget, Budget) or _budget_numbers(budget_to_dict(spec.budget)) is None:
        raise AgentIdentityError("agent-budget-invalid", "budget")
    # Validates the entire graph, not just the top-level keys: a ``set`` deep
    # inside metadata used to pass here and only fail once the store tried to
    # encode it, turning a caller mistake into an `AgentCreationError` after
    # the transaction had already started.
    if _normalized_metadata(spec.metadata) is None:
        raise AgentIdentityError("agent-metadata-invalid", "metadata")


def budget_to_dict(budget: Budget) -> dict[str, JsonValue]:
    """Project a `Budget` to its payload shape without coercing anything.

    Deliberately no ``float()`` or other conversion: coercion here would either
    raise the wrong error type for a bad field or quietly repair it, and in
    both cases `_budget_numbers` would be judging a value other than the one
    the caller supplied. Validation belongs to that one rule.
    """

    return {
        "max_tokens": budget.max_tokens,
        "max_steps": budget.max_steps,
        "max_tool_calls": budget.max_tool_calls,
        "max_wall_seconds": budget.max_wall_seconds,
        "max_children": budget.max_children,
        "max_depth": budget.max_depth,
        "max_processes": budget.max_processes,
    }


def agent_created_data(
    *,
    agent_id: str,
    session_id: str,
    request_id: str,
    spec: AgentSpec,
) -> dict[str, JsonValue]:
    """Build the bounded, append-only Agent creation payload.

    Three relations are kept apart on purpose and must not be read as one
    another:

    * ``session_id`` - which history this Agent owns;
    * ``forked_from_session_id`` - *history lineage*: where this Agent's
      starting context was copied from. It says nothing about who may stop it;
    * ``owner_agent_id`` - *lifecycle ownership*: who is responsible for
      disposing this Agent. It is not a message route and not a lineage claim.

    Communication has no field here at all. A message source is a per-message
    fact, and folding it into a creation fact would make "who created me" and
    "who is talking to me" the same relation forever.

    Raises `AgentIdentityError` for metadata this protocol cannot carry. This
    is public, so it cannot fall back to an empty dict: ``or {}`` conflated
    "rejected" with "legitimately empty" and silently dropped the caller's
    data instead of refusing it.
    """

    metadata = _normalized_metadata(spec.metadata)
    if metadata is None:
        raise AgentIdentityError("agent-metadata-invalid", "metadata")
    # Deep, not ``dict(...)``: a shallow copy still shares every nested
    # container with the caller, so a mutation during the transaction's first
    # ``await`` would be what actually got persisted.
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "request_id": request_id,
        "preset": spec.preset,
        "workspace_id": spec.workspace_id,
        "owner_agent_id": spec.owner_agent_id,
        "forked_from_session_id": spec.forked_from_session_id,
        "capability_grants": list(spec.capability_grants),
        "budget": budget_to_dict(spec.budget),
        "metadata": metadata,
    }


def detach_record(record: AgentRecord) -> AgentRecord:
    """Return an equal record whose ``metadata`` shares no mutable container.

    `AgentRecord` is frozen, but ``metadata`` is an ordinary nested JSON graph,
    so freezing only stops the field being rebound. Any component that keeps a
    record and hands it out repeatedly owes each caller its own copy - the same
    ownership rule `EventStore` and `SessionEventFeed` already follow. Copying
    goes through ``to_json_value()`` so one rule decides what a payload may
    hold and what it normalizes into.
    """

    return replace(
        record,
        metadata={str(key): to_json_value(value) for key, value in record.metadata.items()},
    )


def creation_matches(record: AgentRecord, data: dict[str, JsonValue]) -> bool:
    """Whether ``record`` describes the same request as this creation payload.

    Compared against the frozen payload rather than the caller's live
    `AgentSpec`, so a repeated ``request_id`` is judged against what this call
    actually committed to writing and cannot drift across an ``await``.

    Two groups of fields are deliberately excluded:

    * ``agent_id``, ``session_id`` and ``request_id`` - identity. A retry that
      pinned them is checked directly by the caller of this function; a retry
      that did not pin them has fresh generated ids that are *expected* to
      differ, and comparing them would reject every unpinned idempotent retry;
    * ``metadata`` - free-form, and it does not define which request this is,
      so a cosmetic difference must not turn a retry into a hard failure.
    """

    return (
        record.preset == data["preset"]
        and record.workspace_id == data["workspace_id"]
        and record.owner_agent_id == data["owner_agent_id"]
        and record.forked_from_session_id == data["forked_from_session_id"]
        and list(record.capability_grants) == data["capability_grants"]
        and _budget_numbers(budget_to_dict(record.budget)) == _budget_numbers(data["budget"])
    )


def is_creation_fact(event: EventEnvelope, data: dict[str, JsonValue]) -> bool:
    """Whether ``event`` is exactly the creation fact ``data`` would have written.

    This answers a different question from `creation_matches`, and the two must
    not be swapped. `creation_matches` asks *"is this the same request"* and so
    deliberately ignores generated ids. This asks *"is this **our** event"*, for
    commit reconciliation, where everything participates.

    Matching on ``request_id`` alone is not enough: another writer racing on the
    same id may have committed a **different** Agent, and reporting that as
    ``committed=True`` would tell a caller its own creation landed when what
    landed was somebody else's.

    Two steps, and both are needed. Parsing proves the candidate is a
    well-formed creation fact on the right stream at the right schema version.
    Comparing ``canonical_json`` then proves it is *ours*, exactly - and it is
    deliberately not ``==`` on the payloads, because Python equality is not JSON
    identity: ``True == 1``, ``1 == 1.0`` and ``[True] == [1]`` are all true in
    Python while being different facts in a log. The canonical encoder is the
    same one request fingerprints use, so "are these the same JSON" has one
    definition.
    """

    try:
        parse_agent_created(event)
    except AgentDirectoryProtocolError:
        # Definitively not a well-formed creation fact, so definitively not
        # ours. This is the *only* thing that justifies ``False``.
        return False
    # Anything else propagates on purpose. A canonical-encoding failure means
    # the comparison could not be made, not that the answer is no - and
    # `committed_after_failure` turns that into ``None`` (unknown). Returning
    # ``False`` here would state "provably not committed" at the exact moment
    # the event may well be sitting in the stream, which is the strongest claim
    # from the weakest evidence.
    return canonical_json(event.data) == canonical_json(data)


def parse_agent_created(event: EventEnvelope) -> AgentRecord:
    """Rebuild one durable Agent identity from its creation event.

    The creation transaction parses the envelope it just appended through this
    same function, so the record a caller receives is exactly the record a
    replay produces. There is no second, more forgiving in-memory reading.
    """

    try:
        return _read_agent_created(event)
    except AgentDirectoryProtocolError:
        raise
    except Exception:
        # The boundary covers the **whole** event, not only ``event.data``.
        # `EventEnvelope` is a public DTO that anything may construct, so its
        # protocol fields are as untrusted as its payload: a ``str`` subclass
        # with a raising ``__ne__`` as ``event.type`` breaks the very first
        # comparison. Leaking that would bypass the one stable outcome and
        # break `validate_agent_directory_events`, whose contract is not to
        # raise. Only ``event.seq`` is read out here, and plain attribute
        # access cannot execute anything.
        #
        # ``Exception``, deliberately not ``BaseException``: `SystemExit` and
        # `KeyboardInterrupt` are not verdicts about the event.
        raise AgentDirectoryProtocolError("agent-payload-invalid", event.seq) from None


def _read_agent_created(event: EventEnvelope) -> AgentRecord:
    if event.type != AGENT_CREATED:
        raise AgentDirectoryProtocolError("agent-event-type-unknown", event.seq)
    if event.stream_id != AGENT_DIRECTORY_STREAM:
        # An identity fact is only a fact on the identity stream. Reading one
        # out of a Session Stream would let an Agent's execution history assert
        # who exists.
        raise AgentDirectoryProtocolError("agent-stream-unexpected", event.seq)
    if event.schema_version != AGENT_EVENT_SCHEMA_VERSION:
        raise AgentDirectoryProtocolError("agent-schema-version-unsupported", event.seq)
    data = event.data
    if not isinstance(data, dict):
        raise AgentDirectoryProtocolError("agent-payload-invalid", event.seq)
    if set(data) != AGENT_CREATED_KEYS:
        # Exactly this key set, no more and no less. An extra key means the
        # writer knows something this reader does not, and reading the rest as
        # a complete v1 identity would silently drop whatever it added.
        raise AgentDirectoryProtocolError("agent-payload-keys-unexpected", event.seq)
    return AgentRecord(
        agent_id=_read_identifier(data, "agent_id", event.seq),
        session_id=_read_identifier(data, "session_id", event.seq),
        request_id=_read_identifier(data, "request_id", event.seq),
        preset=_read_identifier(data, "preset", event.seq),
        workspace_id=_read_identifier(data, "workspace_id", event.seq),
        owner_agent_id=_read_optional_identifier(data, "owner_agent_id", event.seq),
        forked_from_session_id=_read_optional_identifier(
            data, "forked_from_session_id", event.seq
        ),
        capability_grants=_read_grants(data, event.seq),
        budget=_read_budget(data, event.seq),
        metadata=_read_metadata(data, event.seq),
        created_seq=event.seq,
    )


__all__ = [
    "AGENT_CREATED",
    "AGENT_CREATED_KEYS",
    "AGENT_DIRECTORY_STREAM",
    "AGENT_EVENT_SCHEMA_VERSION",
    "MAX_BUDGET_VALUE",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_METADATA_DEPTH",
    "agent_created_data",
    "budget_to_dict",
    "creation_matches",
    "detach_record",
    "is_agent_identifier",
    "is_creation_fact",
    "parse_agent_created",
    "require_identifier",
    "validate_spec",
]
