"""UI-neutral, read-only execution activity for one ProductTask.

The ProductTask and Workflow streams identify the managed Agent Sessions.  This
reader follows only those identities, validates each Session with the core
invariant checker, and projects tool names and outcomes without arguments,
outputs or model prose.  It never scans for sessions and never writes facts.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.agents.directory import AgentDirectory, AgentDirectoryReader
from traceh.api.product import ProductRole, ProductTaskSummary
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.execution import product_task_owner_id
from traceh.product.observation import ProductObservation
from traceh.product.topology import product_role_node_id
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.supervision.lifecycle import AgentOwnershipGraph
from traceh.workflow.models import agent_identity

_ROLE_ORDER = ("router", "parent", "reviewer", "coder")
_ROLE_NODE_LABELS = {product_role_node_id(role): role.value for role in ProductRole}
_TOOL_RESULT_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "invalid",
        "denied",
        "aborted_before_dispatch",
        "unknown_after_crash",
    }
)


@dataclass(frozen=True, slots=True)
class ProductToolActivity:
    """One durable tool call paired with its result when one exists."""

    name: str
    call_seq: int
    result_seq: int | None
    status: str
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class ProductRoleActivity:
    """Bounded-value activity metadata for one host-managed role Session."""

    role: str
    agent_id: str
    session_id: str
    turns_started: int
    turns_completed: int
    event_head_seq: int
    effect_head_seq: int
    tools: tuple[ProductToolActivity, ...]

    @property
    def tool_call_count(self) -> int:
        return len(self.tools)


@dataclass(frozen=True, slots=True)
class ProductTaskActivity:
    """Fresh activity from the exact Sessions owned by one ProductTask."""

    task_id: str
    roles: tuple[ProductRoleActivity, ...]

    @property
    def tool_call_count(self) -> int:
        return sum(role.tool_call_count for role in self.roles)


class ProductTaskActivityReader:
    """Project role and tool activity from task-bound Session streams."""

    __slots__ = ("_directory", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._directory = AgentDirectoryReader(store)

    @property
    def store(self) -> EventStore:
        return self._store

    async def load(self, observation: ProductObservation) -> ProductTaskActivity:
        if type(observation) is not ProductObservation:
            raise ProductInputError("product-activity-observation-invalid", "observation")
        summary = observation.summary
        if summary is None or summary.task_id != observation.task_id:
            raise ProductStateError(
                "product-activity-summary-unavailable", observation.task_id
            )
        try:
            directory = await self._directory.load()
        except Exception:
            raise ProductStateError(
                "product-activity-directory-unavailable", observation.task_id
            ) from None
        identities = _role_identities(observation, summary, directory)
        roles = [
            await self._load_role(
                task_id=observation.task_id,
                role=role,
                agent_id=agent_id,
                session_id=session_id,
            )
            for role, agent_id, session_id in identities
        ]
        roles.sort(key=lambda item: _ROLE_ORDER.index(item.role))
        return ProductTaskActivity(observation.task_id, tuple(roles))

    async def _load_role(
        self,
        *,
        task_id: str,
        role: str,
        agent_id: str,
        session_id: str,
    ) -> ProductRoleActivity:
        try:
            events = await self._store.read(SessionService.session_stream(session_id))
            effects = await self._store.read(SessionService.effect_stream(session_id))
        except Exception:
            raise ProductStateError(
                "product-activity-session-unreadable", task_id
            ) from None
        try:
            if CoreInvariantChecker().check(events, effects):
                raise ValueError("invalid Session lifecycle")
            tools = _tool_activity(events)
        except Exception:
            raise ProductStateError(
                "product-activity-session-invalid", task_id
            ) from None
        return ProductRoleActivity(
            role=role,
            agent_id=agent_id,
            session_id=session_id,
            turns_started=sum(event.type == "turn/start" for event in events),
            turns_completed=sum(event.type == "turn/end" for event in events),
            event_head_seq=events[-1].seq if events else 0,
            effect_head_seq=effects[-1].seq if effects else 0,
            tools=tools,
        )


def _role_identities(
    observation: ProductObservation,
    summary: ProductTaskSummary,
    directory: AgentDirectory,
) -> tuple[tuple[str, str, str], ...]:
    identities: list[tuple[str, str, str]] = []
    known_streams = set(observation.related_streams)
    owned_agents = frozenset(
        AgentOwnershipGraph(directory).subtree_postorder(
            product_task_owner_id(observation.task_id)
        )
    )
    router_agent = summary.router_agent_id
    router_session = summary.routing_session_id
    if (router_agent is None) != (router_session is None):
        raise ProductStateError(
            "product-activity-router-identity-incomplete", observation.task_id
        )
    if router_agent is not None and router_session is not None:
        record = directory.get(router_agent)
        if record is None:
            raise ProductStateError(
                "product-activity-agent-record-missing", observation.task_id
            )
        if record.session_id != router_session:
            raise ProductStateError(
                "product-activity-router-session-mismatch", observation.task_id
            )
        _require_owned_agent(owned_agents, router_agent, observation.task_id)
        _require_observed_stream(known_streams, router_session, observation.task_id)
        identities.append(("router", router_agent, router_session))

    evidence = observation.evidence
    if evidence is not None:
        for node in evidence.nodes:
            role = _ROLE_NODE_LABELS.get(node.node_id)
            if role is None:
                continue
            if (node.agent_id is None) != (node.session_id is None):
                raise ProductStateError(
                    "product-activity-role-identity-incomplete", observation.task_id
                )
            if node.agent_id is None or node.session_id is None:
                continue
            expected_agent, expected_session, expected_request, _ = agent_identity(
                summary.workflow_run_id, node.node_id
            )
            if node.agent_id != expected_agent or node.session_id != expected_session:
                raise ProductStateError(
                    "product-activity-role-identity-mismatch", observation.task_id
                )
            record = directory.get(expected_agent)
            if record is None:
                raise ProductStateError(
                    "product-activity-agent-record-missing", observation.task_id
                )
            if (
                record.session_id != expected_session
                or record.request_id != expected_request
            ):
                raise ProductStateError(
                    "product-activity-role-session-mismatch", observation.task_id
                )
            _require_owned_agent(owned_agents, expected_agent, observation.task_id)
            _require_observed_stream(known_streams, expected_session, observation.task_id)
            identities.append((role, expected_agent, expected_session))

    session_ids = [session_id for _, _, session_id in identities]
    if len(session_ids) != len(set(session_ids)):
        raise ProductStateError(
            "product-activity-session-duplicate", observation.task_id
        )
    return tuple(identities)


def _require_owned_agent(
    owned_agents: frozenset[str], agent_id: str, task_id: str
) -> None:
    if agent_id not in owned_agents:
        raise ProductStateError("product-activity-agent-owner-mismatch", task_id)


def _require_observed_stream(
    known_streams: set[str], session_id: str, task_id: str
) -> None:
    if SessionService.session_stream(session_id) not in known_streams:
        raise ProductStateError("product-activity-session-unbound", task_id)


def _tool_activity(events) -> tuple[ProductToolActivity, ...]:
    pending: dict[str, tuple[int, str]] = {}
    completed: list[ProductToolActivity] = []
    for event in events:
        if event.type == "tool/call":
            call_id = _plain_identity(event.data.get("tool_call_id"))
            name = _plain_identity(event.data.get("tool_name"))
            if call_id in pending:
                raise ValueError("duplicate tool call")
            pending[call_id] = (event.seq, name)
        elif event.type == "tool/result":
            call_id = _plain_identity(event.data.get("tool_call_id"))
            call = pending.pop(call_id, None)
            status = event.data.get("status")
            if call is None or status not in _TOOL_RESULT_STATUSES:
                raise ValueError("orphan or invalid tool result")
            call_seq, name = call
            if _plain_identity(event.data.get("tool_name")) != name:
                raise ValueError("tool result identity mismatch")
            data = event.data.get("data")
            exit_code = data.get("exit_code") if isinstance(data, dict) else None
            if type(exit_code) is not int:
                exit_code = None
            completed.append(
                ProductToolActivity(name, call_seq, event.seq, str(status), exit_code)
            )
    completed.extend(
        ProductToolActivity(name, call_seq, None, "pending", None)
        for call_seq, name in pending.values()
    )
    completed.sort(key=lambda item: item.call_seq)
    return tuple(completed)


def _plain_identity(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid activity identity")
    return value


__all__ = [
    "ProductRoleActivity",
    "ProductTaskActivity",
    "ProductTaskActivityReader",
    "ProductToolActivity",
]
