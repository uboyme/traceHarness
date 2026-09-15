"""Task-shaped, host-bound use of the original Supervisor and Inbox.

No task queue or report cache lives here. Creation, delivery, cancellation and
reports retain their existing durable owners; the model supplies only work.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Protocol

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import freeze_agent_spec
from traceh.agents.inbox import AgentInboxReader
from traceh.api.agents import AgentMessage, AgentRecord, AgentSpec, AgentSupervisor, MessageTarget
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.tools import EffectKind, Tool, ToolExecutionContext, ToolOutput
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import EventStore
from traceh.supervision.authority import AgentToolAuthority, AgentToolBindingError
from traceh.supervision.errors import AgentMessageNotSettledError
from traceh.supervision.execution import durable_log_identity
from traceh.supervision.investigation_budget import (
    DECIDE_BUDGET,
    budget_view,
    read_budget_requests,
)
from traceh.supervision.investigation_work import (
    INVESTIGATOR_ROLE,
    read_investigation_work,
    require_assignment_id,
    validate_investigation_work,
    validate_work_fields,
    work_properties,
)
from traceh.supervision.tools import _operation_id, _report_data

COLLECT_INVESTIGATION = "collect_investigation"
INVESTIGATION_TOOL_IDS = (
    "delegate_investigation",
    "followup_investigation",
    COLLECT_INVESTIGATION,
    "stop_investigation",
    DECIDE_BUDGET,
)

DELEGATE_GUIDANCE = (
    "Delegate an independent readonly investigation of the task's original source revision. "
    "Use only when its result can help the task; simple work can stay local. "
    "The child cannot inspect your uncommitted edits. "
    "Give a bounded goal and evidence deliverable. "
    "Continue useful local work, or wait if the report is needed next; "
    "collect its exact message before relying on it."
)
FOLLOWUP_GUIDANCE = (
    "Send a further bounded investigation to an existing owned child on the same original source. "
    "This creates a new message; collect the returned message id."
)
COLLECT_GUIDANCE = (
    "Read a specific investigation report and its evidence references. "
    "Wait up to 30 seconds or use zero to poll. A pending report is not an answer; "
    "a completed report is a child claim, not verified truth or approval."
)
STOP_GUIDANCE = (
    "Stop an unneeded investigation and wait for its activation to converge; "
    "original messages, reports and costs remain available."
)


@dataclass(frozen=True, slots=True)
class InvestigationBinding:
    spec: AgentSpec
    source_id: str
    revision: str


class InvestigationPolicy(Protocol):
    """Host assembly verifies the task, readonly source and actual child."""

    def prepare(self, owner: AgentRecord) -> InvestigationBinding: ...

    async def validate_child(
        self, owner: AgentRecord, child: AgentRecord
    ) -> InvestigationBinding: ...

    def planned_grant(self, owner: AgentRecord):
        """The exact child Budget grant a dispatch would reserve, if declared.

        A plan uses it to tell the model up front that a batch cannot fit, so
        the answer is a correctable rejection rather than a failed dispatch. The
        original reservation remains the authority; a host that declares no
        grant simply leaves the whole decision there.
        """
        ...


def _text(arguments: dict[str, JsonValue], key: str, limit: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must contain bounded nonempty text")
    return value


class InvestigationControl:
    def __init__(self, supervisor, authority, policy, budgets=None):
        if durable_log_identity(supervisor.store) is not durable_log_identity(authority.store):
            raise AgentToolBindingError("investigation bindings use different event stores")
        self.supervisor = supervisor
        self.authority = authority
        self.policy = policy
        if budgets is not None and durable_log_identity(budgets.store) is not durable_log_identity(
            supervisor.store
        ):
            raise AgentToolBindingError("investigation budgets use another event store")
        self.budgets = budgets

    async def child(self, agent_id: str, context: ToolExecutionContext):
        owner = await self.authority.require_caller(context.session_id)
        child = await self.authority.require_owned(agent_id, context.session_id)
        if child.owner_agent_id != owner.agent_id:
            raise AgentToolBindingError("investigation must be a direct owned child")
        binding = await self.policy.validate_child(owner, child)
        return owner, child, binding

    @staticmethod
    def work(arguments, binding, owner_id, *, assignment_id):
        validate_work_fields(arguments)
        body = {
            "format": 3,
            "kind": "readonly-investigation",
            "assignment_id": require_assignment_id(assignment_id),
            "role": INVESTIGATOR_ROLE,
            "owner_agent_id": owner_id,
            "source_id": binding.source_id,
            "revision": binding.revision,
            **arguments,
        }
        return canonical_json({**body, "input_digest": fingerprint(body)})

    async def send(self, agent_id, content, context, *, discriminator=""):
        message_id = _operation_id(
            "investigation-message", self.authority.owner_agent_id, context, discriminator
        )
        await self.supervisor.send(
            agent_id,
            AgentMessage(
                message_id=message_id,
                content=content,
                source=self.authority.owner_agent_id,
                correlation_id=context.turn_id,
                causation_id=context.tool_call_id,
            ),
            target=MessageTarget.NEW_TURN,
            wakeup=True,
        )
        receipt = {
            "agent_id": agent_id,
            "message_id": message_id,
            "status": "accepted",
            "next_action": "collect_investigation with this exact agent_id and message_id",
        }
        return ToolOutput(canonical_json(receipt), receipt)

    async def delegate(self, arguments, context, *, assignment_id=None):
        owner = await self.authority.require_caller(context.session_id)
        binding = self.policy.prepare(owner)
        spec = freeze_agent_spec(binding.spec)
        if spec.owner_agent_id != owner.agent_id or spec.forked_from_session_id is not None:
            raise AgentToolBindingError("investigation owner or history inheritance is invalid")
        # A direct delegation has no plan entry, so the host records this call's
        # own derived key; a plan supplies the assignment it validated.
        discriminator = "" if assignment_id is None else require_assignment_id(assignment_id)
        content = self.work(
            arguments,
            binding,
            owner.agent_id,
            assignment_id=_operation_id("investigation-assignment", owner.agent_id, context)
            if assignment_id is None
            else assignment_id,
        )
        handle = await self.supervisor.create(
            spec,
            request_id=_operation_id(
                "investigation-create", owner.agent_id, context, discriminator
            ),
            session_id=_operation_id(
                "investigation-session", owner.agent_id, context, discriminator
            ),
        )
        try:
            return await self.send(handle.agent_id, content, context, discriminator=discriminator)
        except BaseException as primary:
            # A created child belongs to this nonretryable Tool effect. Do not
            # let a failed/cancelled send leave an unreturned live activation.
            cleanup = asyncio.create_task(self.supervisor.dispose(handle.agent_id))
            await await_worker_convergence(cleanup)
            if not cleanup.cancelled() and cleanup.exception() is not None:
                if isinstance(primary, asyncio.CancelledError):
                    # AgentLoop/Delivery recognize cancellation by its outer
                    # type. Keep cleanup failure as cause, as Supervisor does.
                    raise primary from cleanup.exception()
                raise BaseExceptionGroup(
                    "investigation delivery and cleanup failed", (primary, cleanup.exception())
                ) from None
            raise

    async def followup(self, arguments, context):
        agent_id = _text(arguments, "agent_id", 256)
        owner, _, binding = await self.child(agent_id, context)
        if self.budgets is not None:
            (await self.budgets.ledger()).require_open_account(agent_id)
        # A followup is further work for the *same* assignment: the host reuses
        # the child's accepted assignment key instead of inventing a new one.
        inbox = tuple(await AgentInboxReader(self.supervisor.store).load(agent_id))
        if not inbox:
            raise AgentToolBindingError("investigation has no accepted work to follow up")
        assignment_id = read_investigation_work(inbox[-1].message.content)["assignment_id"]
        return await self.send(
            agent_id,
            self.work(
                {k: v for k, v in arguments.items() if k != "agent_id"},
                binding,
                owner.agent_id,
                assignment_id=assignment_id,
            ),
            context,
        )

    async def collect(self, arguments, context):
        agent_id = _text(arguments, "agent_id", 256)
        message_id = _text(arguments, "message_id", 256)
        owner, child, binding = await self.child(agent_id, context)
        accepted = (await AgentInboxReader(self.supervisor.store).load(agent_id)).get(message_id)
        if accepted is None or accepted.message.source != owner.agent_id:
            raise AgentToolBindingError("investigation message is not from the bound owner")
        work = validate_investigation_work(accepted.message.content, binding, owner.agent_id)
        wait_seconds = arguments.get("wait_seconds")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be an integer from 0 to 30")
        if wait_seconds:
            timeout = asyncio.timeout(wait_seconds)
            try:
                async with timeout:
                    await self.supervisor.wait_message(agent_id, message_id)
            except TimeoutError:
                if not timeout.expired():
                    raise
        try:
            report = await self.supervisor.report(agent_id, message_id)
        except AgentMessageNotSettledError:
            pending = {
                "agent_id": agent_id,
                "message_id": message_id,
                "status": "pending",
                "next_action": (
                    "Continue useful work or collect later; no final report is available."
                ),
            }
            return ToolOutput(canonical_json(pending), pending)
        if (
            report.agent_id != agent_id
            or report.session_id != child.session_id
            or report.message_id != message_id
        ):
            raise AgentToolBindingError("investigation report identity mismatch")
        data = {
            **_report_data(report),
            "source_id": binding.source_id,
            "revision": binding.revision,
            "input_digest": work["input_digest"],
            "interpretation": (
                "Child statement with evidence references; completion is not verification."
            ),
        }
        if self.budgets is not None:
            requests = await read_budget_requests(
                self.supervisor.store, child, message_id=message_id
            )
            ledger = await self.budgets.ledger()
            data["budget_requests"] = [
                {
                    **r,
                    "decision": asdict(decision)
                    if (decision := ledger.token_decision(r["request_id"])) is not None
                    else None,
                }
                for r in requests
            ]
            account = ledger.account(agent_id)
            if account is not None and account.closed_seq is None:
                data["budget"] = await budget_view(self.budgets, agent_id)
                data["parent_remaining_tokens"] = ledger.available(owner.agent_id).max_tokens
            if report.reason == "investigation_budget_requested":
                data["interpretation"] = (
                    "Turn yielded with partial evidence and a budget request; work is unfinished."
                )
                data["next_action"] = (
                    "Decide the exact request, then explicitly follow up if continuing."
                )
        return ToolOutput(
            content=canonical_json({**data, "statement": report.final_text}),
            data=data,
            evidence=report.evidence_refs,
        )

    async def stop(self, arguments, context):
        agent_id = _text(arguments, "agent_id", 256)
        await self.child(agent_id, context)
        from traceh.budgets.enforcement import _await_owned

        async def finish():
            await self.supervisor.dispose(agent_id)
            if self.budgets is not None:
                account = (await self.budgets.ledger()).account(agent_id)
                if account is not None and account.closed_seq is None:
                    await self.budgets.close_account(
                        operation_id=f"investigation-stop-budget-{agent_id}", agent_id=agent_id
                    )

        await _await_owned(finish(), name="investigation-stop-and-close-budget")
        return ToolOutput(
            "Investigation activation and descendants have stopped.", {"agent_id": agent_id}
        )

    async def decide_budget(self, arguments, context):
        if self.budgets is None:
            raise AgentToolBindingError("investigation budget control is not assembled")
        agent_id = _text(arguments, "agent_id", 256)
        request_id = _text(arguments, "request_id", 256)
        owner, child, binding = await self.child(agent_id, context)
        requests = await read_budget_requests(self.supervisor.store, child)
        matches = [r for r in requests if r["request_id"] == request_id]
        if len(matches) != 1:
            raise AgentToolBindingError("budget request is not an exact child request")
        request = matches[0]
        accepted = (await AgentInboxReader(self.supervisor.store).load(agent_id)).get(
            request["message_id"]
        )
        if accepted is None or accepted.message.source != owner.agent_id:
            raise AgentToolBindingError("budget request has no owned work")
        validate_investigation_work(accepted.message.content, binding, owner.agent_id)
        tokens = arguments.get("tokens")
        if type(tokens) is not int or not 0 <= tokens <= request["tokens"]:
            raise ValueError("grant must be between zero and the requested amount")
        reason = _text(arguments, "reason", 2000)
        ledger = await self.budgets.ledger()
        if ledger.token_decision(request_id) is None:
            report = await self.supervisor.report(agent_id, request["message_id"])
            inbox = tuple(await AgentInboxReader(self.supervisor.store).load(agent_id))
            if (
                report.status != "completed"
                or report.reason != "investigation_budget_requested"
                or report.turn_id != request["turn_id"]
                or not inbox
                or inbox[-1].message.message_id != request["message_id"]
            ):
                raise AgentToolBindingError("budget request is pending, cancelled or superseded")
        decision = await self.budgets.decide_child_tokens(
            operation_id=f"investigation-budget-decision-{request_id}",
            request_id=request_id,
            parent_agent_id=owner.agent_id,
            child_agent_id=child.agent_id,
            tokens=tokens,
            reason=reason,
        )
        data = {
            **asdict(decision),
            "status": "granted" if tokens else "declined",
            "next_action": (
                "Use followup_investigation for explicit further work; "
                "this decision starts no execution."
                if tokens
                else "Use the partial evidence or stop this investigation. "
                "No further allowance was granted."
            ),
        }
        return ToolOutput(canonical_json(data), data)


@dataclass(slots=True)
class InvestigationTool:
    control: InvestigationControl
    action: str
    name: str
    description: str
    effect_kind: EffectKind
    input_schema: dict[str, JsonValue] = field(repr=False)

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        return await getattr(self.control, self.action)(arguments, context)


class InvestigationToolset:
    """Work adapters and optional ledger decision; reuse the original lifecycle."""

    def __init__(
        self,
        *,
        supervisor: AgentSupervisor,
        owner_agent_id: str,
        event_store: EventStore,
        policy: InvestigationPolicy,
        budgets=None,
    ):
        control = InvestigationControl(
            supervisor,
            AgentToolAuthority(
                directory_reader=AgentDirectoryReader(event_store),
                owner_agent_id=owner_agent_id,
            ),
            policy,
            budgets,
        )
        self.control = control
        specs = (
            (
                "delegate",
                INVESTIGATION_TOOL_IDS[0],
                DELEGATE_GUIDANCE,
                EffectKind.EXTERNAL_TRANSACTION,
                work_properties(),
            ),
            (
                "followup",
                INVESTIGATION_TOOL_IDS[1],
                FOLLOWUP_GUIDANCE,
                EffectKind.EXTERNAL_TRANSACTION,
                {"agent_id": {"type": "string"}, **work_properties()},
            ),
            (
                "collect",
                INVESTIGATION_TOOL_IDS[2],
                COLLECT_GUIDANCE,
                EffectKind.PURE_READ,
                {
                    "agent_id": {"type": "string"},
                    "message_id": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 30},
                },
            ),
            (
                "stop",
                INVESTIGATION_TOOL_IDS[3],
                STOP_GUIDANCE,
                EffectKind.EXTERNAL_TRANSACTION,
                {"agent_id": {"type": "string"}},
            ),
        )
        if budgets is not None:
            specs += (
                (
                    "decide_budget",
                    DECIDE_BUDGET,
                    "Decide a collected child's exact budget request. tokens=0 declines; positive "
                    "tokens allocate from your existing allowance within its host ceiling and your "
                    "retained reserve. First wait for the requesting turn to settle. "
                    "Decide whether "
                    "more investigation is useful from its progress/evidence. No work is resumed: "
                    "send a bounded followup explicitly after granting. "
                    "Never promise unlimited retries.",
                    EffectKind.EXTERNAL_TRANSACTION,
                    {
                        "agent_id": {"type": "string"},
                        "request_id": {"type": "string"},
                        "tokens": {"type": "integer", "minimum": 0},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                    },
                ),
            )
        self.tools: tuple[Tool, ...] = tuple(
            InvestigationTool(
                control,
                action,
                name,
                description,
                effect,
                {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            )
            for action, name, description, effect, properties in specs
        )
