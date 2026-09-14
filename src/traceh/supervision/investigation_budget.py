"""Budget navigation and progress requests, backed by original Session evidence.

The request is a successful tool result, not a second message queue. A parent
collects it through the existing investigation report and explicitly decides
against the Budget ledger. Continuation ends this turn without another LLM call.
"""

from __future__ import annotations

from dataclasses import asdict

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.json_types import canonical_json
from traceh.api.tools import EffectKind, ToolOutput
from traceh.runtime.continuation import DefaultContinuationRuntime, Finish
from traceh.session.service import SessionService
from traceh.session.tool_output import resolve_tool_output
from traceh.supervision.authority import AgentToolBindingError
from traceh.supervision.investigation_work import validate_investigation_work
from traceh.supervision.tools import _operation_id

REQUEST_BUDGET = "request_investigation_budget"
INSPECT_BUDGET = "inspect_investigation_budget"
DECIDE_BUDGET = "decide_investigation_budget"
INVESTIGATOR_BUDGET_TOOLS = (INSPECT_BUDGET, REQUEST_BUDGET)


async def budget_view(budgets, agent_id):
    ledger = await budgets.ledger()
    account = ledger.require_open_account(agent_id)
    return {
        "allocated_tokens": account.limits.max_tokens,
        "remaining_tokens": ledger.available(agent_id).max_tokens,
        "cumulative_token_ceiling": ledger.token_ceiling(agent_id),
        "charged": asdict(account.charged),
        "other_remaining_limits": asdict(ledger.available(agent_id)),
        "notice": (
            "Ledger allowance, not an exact bill. Additional tokens do not renew other limits."
        ),
    }


async def read_budget_requests(store, child, *, message_id=None, step_id=None):
    """Read exact successful request results; original retained data is supported."""
    sessions = SessionService(store)
    events = await sessions.read_session(child.session_id)
    turns = {e.data["turn_id"]: e.data["message_id"] for e in events if e.type == "turn/start"}
    steps = {e.data["step_id"]: e.data["turn_id"] for e in events if e.type == "step/start"}
    found = []
    effects = None
    for event in events:
        data = event.data
        if (
            event.type != "tool/result"
            or data.get("tool_name") != REQUEST_BUDGET
            or data.get("status") != "succeeded"
        ):
            continue
        actual_turn = steps.get(data.get("step_id"))
        if message_id is not None and turns.get(actual_turn) != message_id:
            continue
        if step_id is not None and data.get("step_id") != step_id:
            continue
        body = data.get("data")
        if "output_ref" in data:
            if effects is None:
                effects = await sessions.read_effects(child.session_id)
            ref = data["output_ref"]
            body = resolve_tool_output(
                events,
                effects,
                session_id=child.session_id,
                effect_id=ref["effect_id"],
                digest=ref["digest"],
            )["data"]
        if (
            not isinstance(body, dict)
            or body.get("format") != 1
            or body.get("child_agent_id") != child.agent_id
            or body.get("parent_agent_id") != child.owner_agent_id
            or body.get("turn_id") != actual_turn
            or body.get("message_id") != turns.get(actual_turn)
            or body.get("tool_call_id") != data.get("tool_call_id")
        ):
            raise AgentToolBindingError("budget request evidence mismatch")
        found.append({**body, "evidence_ref": f"session:{child.session_id}#{event.seq}"})
    return found


class InvestigatorBudgetTool:
    effect_kind = EffectKind.PURE_READ

    def __init__(self, *, name, budgets, child_id, session_id, policy):
        self.name = name
        self.budgets, self.child_id, self.session_id, self.policy = (
            budgets,
            child_id,
            session_id,
            policy,
        )
        properties = (
            {}
            if name == INSPECT_BUDGET
            else {
                "tokens": {"type": "integer", "minimum": 1},
                "progress": {"type": "string", "minLength": 1, "maxLength": 2000},
                "remaining_work": {"type": "string", "minLength": 1, "maxLength": 2000},
            }
        )
        self.input_schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        self.description = (
            "Read your fresh lifetime allowance and host ceiling. Check before expensive reading "
            "when the remaining work may exceed your allowance. Do not poll repeatedly."
            if name == INSPECT_BUDGET
            else "Report partial findings with file/line evidence and unknowns, "
            "and request additional "
            "tokens from your direct parent. Use before exhaustion only if more work is useful. "
            "A successful call ends this turn without another model call. The parent will collect "
            "the request, decide, and explicitly send further work if needed. This is not task "
            "completion or permission to exceed the host ceiling. "
            "Do not batch further work after it."
        )

    async def execute(self, arguments, context):
        if context.session_id != self.session_id:
            raise AgentToolBindingError("investigator budget session mismatch")
        directory = await AgentDirectoryReader(self.budgets.store).load()
        child = directory.get(self.child_id)
        owner = directory.get(child.owner_agent_id) if child else None
        if child is None or owner is None or child.session_id != self.session_id:
            raise AgentToolBindingError("investigator budget owner mismatch")
        binding = await self.policy.validate_child(owner, child)
        view = await budget_view(self.budgets, child.agent_id)
        if self.name == INSPECT_BUDGET:
            if arguments:
                raise ValueError("budget inspection accepts no arguments")
            return ToolOutput(canonical_json(view), view)
        if set(arguments) != {"tokens", "progress", "remaining_work"}:
            raise ValueError("budget request fields invalid")
        amount = arguments["tokens"]
        ceiling, allocated = view["cumulative_token_ceiling"], view["allocated_tokens"]
        if (
            type(amount) is not int
            or type(ceiling) is not int
            or type(allocated) is not int
            or not 0 < amount <= ceiling - allocated
        ):
            raise ValueError("requested tokens exceed remaining cumulative headroom")
        for key in ("progress", "remaining_work"):
            value = arguments[key]
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError("budget request text must be bounded and nonempty")
        starts = [
            e
            for e in await SessionService(self.budgets.store).read_session(self.session_id)
            if e.type == "turn/start" and e.data["turn_id"] == context.turn_id
        ]
        if len(starts) != 1:
            raise AgentToolBindingError("budget request has no actual turn")
        message_id = starts[0].data["message_id"]
        accepted = (await AgentInboxReader(self.budgets.store).load(child.agent_id)).get(message_id)
        if accepted is None or accepted.message.source != owner.agent_id:
            raise AgentToolBindingError("budget request has no owned work")
        validate_investigation_work(accepted.message.content, binding, owner.agent_id)
        body = {
            "format": 1,
            "request_id": _operation_id("investigation-budget-request", child.agent_id, context),
            "child_agent_id": child.agent_id,
            "parent_agent_id": owner.agent_id,
            "message_id": message_id,
            "turn_id": context.turn_id,
            "tool_call_id": context.tool_call_id,
            **arguments,
            "interpretation": "Partial child claims; budget requested, task not completed.",
        }
        return ToolOutput(canonical_json(body), body)


class InvestigationBudgetContinuation:
    """A production continuation adapter; no mutable progress flag in AgentLoop."""

    def __init__(self, store, child_id, session_id):
        self.store, self.child_id, self.session_id = store, child_id, session_id
        self.inner = DefaultContinuationRuntime()

    async def decide(self, *, response, **kwargs):
        if any(call.name == REQUEST_BUDGET for call in response.tool_calls):
            child = (await AgentDirectoryReader(self.store).load()).get(self.child_id)
            events = await SessionService(self.store).read_session(self.session_id)
            steps = [e for e in events if e.type == "step/start"]
            if child is not None and steps:
                requests = await read_budget_requests(
                    self.store, child, step_id=steps[-1].data["step_id"]
                )
                if any(r["tool_call_id"] in {c.id for c in response.tool_calls} for r in requests):
                    return Finish("investigation_budget_requested")
        return await self.inner.decide(response=response, **kwargs)
