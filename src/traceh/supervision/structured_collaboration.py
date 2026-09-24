"""A bounded decision and report handoff using the existing investigation owner."""

import asyncio
import json
from dataclasses import dataclass

from traceh.api.json_types import canonical_json
from traceh.api.tools import EffectKind, ToolOutput
from traceh.artifacts.manifest import freeze_changed_paths
from traceh.budgets.events import initial_token_limits
from traceh.concurrency import await_worker_convergence
from traceh.session.request_view import read_view
from traceh.session.service import SessionService
from traceh.supervision.delegation import COLLECT_INVESTIGATION, InvestigationControl
from traceh.supervision.investigation_work import (
    CHILD_LIMITS,
    INVESTIGATOR_ROLE,
    MAIN_LIMITS,
    MAX_ASSIGNMENTS,
    PATCH_AUTHOR_ROLE,
    object_schema,
    require_assignment_id,
    text_properties,
    validate_text_fields,
    validate_work_fields,
)
from traceh.supervision.writable_collaboration import COLLECT_CHILD_PATCH, WRITABLE_GUIDANCE
from traceh.supervision.writable_work import validate_assignment

SUBMIT_COLLABORATION = "submit_collaboration_plan"
AWAIT_REPORT = "await_report"
DISPATCH_AND_CONTINUE = "dispatch_and_continue"
HANDOFF_MODES = (AWAIT_REPORT, DISPATCH_AND_CONTINUE)
#: Fallback in-call wait for a host that declares no child grant, so this call
#: still has a bound when there is no authorized child wall to derive one from.
CHILD_REPORT_WAIT_SECONDS = 300
#: Reserved out of the call's own ceiling for the work that happens after the
#: children settle: collecting every assistant's report inside this same call.
COLLECT_RESERVE_SECONDS = 5
# Every consumable dimension a child reservation carves out of its parent.
_BATCH_DIMENSIONS = (
    "max_tokens",
    "max_steps",
    "max_tool_calls",
    "max_wall_milliseconds",
)


class CollaborationPlanInputInvalid(ValueError):
    """A correctable plan rejection before delegation has begun."""


#: The main Agent's retained work, as three top-level plan arguments.
#:
#: A nested ``main_work`` object was the one parameter shape a real Provider
#: could not emit: replaying the same frozen request eight times, qwen3.6-plus
#: via DashScope left its value empty, ``null`` or leaked template text seven
#: times, while the identical plan with these three top-level strings was valid
#: eight times out of eight. The work package still carries the original
#: ``main_work`` object; only the tool's input shape changed.
MAIN_WORK_ARGUMENTS = {
    "main_goal": "goal",
    "main_deliverable": "deliverable",
    "main_uses_child_report": "uses_child_report",
}


def plan_main_work(arguments):
    """The internal ``main_work`` object assembled from the flat plan arguments."""
    return {field: arguments[name] for name, field in MAIN_WORK_ARGUMENTS.items()}


def correctable_plan_result(data):
    return (data.get("status"), data.get("error_type")) in {
        ("invalid", "ToolArgumentError"),
        ("denied", "ToolDenied"),
        ("failed", "CollaborationPlanInputInvalid"),
    }


PLAN_REQUIREMENT = (
    "When ready, submit one submit_collaboration_plan call containing main"
    "_goal, main_deliverable, main_uses_child_report and children. "
    "The confirmed multi mode requires at least one a"
    "ssistant assignment; there is no local option. The host validates a"
    "nd executes the whole plan. Existing permissions, budgets and human"
    " approval remain binding."
)
ALLOCATION_GUIDANCE = (
    "Submit exactly one submit_collaboration_plan call containing main"
    "_goal, main_deliverable, main_uses_child_report and children."
    "\nThere is no local execution option in the confir"
    "med multi mode. Allocate a\nsubstantive, bounded deliverable to each "
    "assignment and retain substantive\nintegration, implementati"
    "on, or analysis for the main agent.\n\nDefine each assignment's goal, sco"
    "pe, exclusions, deliverable, and necessary briefing.\nState what y"
    "ou retain and exactly how you will use each result. Assign"
    "\neach substantive deliverable to one owner. Do not give an assistant your"
    "\nwhole task, ask it to restate a known answer, "
    "or call a vague duplicate\nreview a separate contribution. Shared "
    "source files are allowed for reading. Targeted\nverification of an assistant's cl"
    "aims is required and is not duplicate assignment.\n\nEvery assistant work"
    "s on the host-bound original revision. It cannot inspect your\nfut"
    "ure or uncommitted edits, another assistant's workspace, or unfinished sibling "
    "work. Do not create a dependency on those edi"
    "ts, assign\nwriting or execution beyond an assignment's granted tools, or req"
    "uest another agent. A\nuseful report may precede your work; simult"
    "aneous execution is not required.\n\n"
    "Decide how many assignments the work actually contains. Look for independently "
    "deliverable units: a unit has its own checkable acceptance condition, needs no other "
    "assignment's output to be written, and (for writable work) touches its own files. "
    "Give every such unit its own assignment with its own assignment_id, role and exact "
    "paths, and keep integration, cross-cutting decisions and final verification for "
    "yourself. Two units that are specified independently can be written at the same time "
    "even though one calls the other at runtime: a stated interface is enough.\n\n"
    "Do not split what shares a file, needs a sibling's result first, or is too small to "
    "carry its own briefing and budget. When the work genuinely holds one unit, one "
    "assignment is a complete plan. The host ceiling bounds how many you may ask for; "
    "within it the count is your judgment about the work, not a quota to fill or avoid. "
    "If a batch does not fit the host's allowance, the rejection says so and you may "
    "resubmit with fewer assignments.\n\n"
    "Distinguish an identified source whose contents are not yet read from a source "
    "that is unavailable. When available evidence identifies relevant sources within "
    "an assistant's host-granted readonly capabilities, their unknown contents may be "
    "the subject of the assignment. Define which questions it should resolve, "
    "which sources it should inspect, and what evidence and limitations it should "
    "return. You do not need to know those contents or perform that investigation "
    "before allocating it. Retain the work that uses the resulting findings. "
    "Do not treat unread content alone as a blocking reason, or equate unread with "
    "missing. This does not establish access permission or guarantee that a source "
    "exists: do not invent sources, and let the assistant report denied, missing or "
    "insufficient evidence when observed.\n\nUse only available evidence an"
    "d clearly mark unknowns. Do not invent sources,\ncompleted checks,"
    " permissions, or findings. If a valid allocation cannot be\nmade, "
    "state the blocking reason; this stops the run and does not select"
    " single.\nBefore submitting, check coverage of the task, ownership"
    " of each deliverable,\nfeasible inputs, evidence requirements, and"
    " exclusions. You may correct rejected input before delegation starts. "
    "After an accepted plan, do not submit another plan."
)
INVESTIGATOR_GUIDANCE = (
    "An investigator assignment is a readonly assistant on the host-bound original "
    "revision: it reads and searches and returns findings with evidence references. "
    "It cannot write, run commands, publish or request another agent."
)


def planning_guidance():
    return PLAN_REQUIREMENT + "\n" + ALLOCATION_GUIDANCE


def handoff_guidance(collect_tools):
    """One dispatch, two explicit return shapes; the collect owners never change."""
    tools = " or ".join(collect_tools)
    return (
        f'handoff chooses how this single dispatch returns. Omit it or use "{AWAIT_REPORT}" '
        "to have the host dispatch every assignment and then wait for all of their exact "
        f'reports inside this call, under one shared deadline. Use "{DISPATCH_AND_CONTINUE}" '
        "when you hold retained work that does not depend on those results: the host returns "
        "the accepted identities immediately, the assistants run concurrently on the same "
        f"original revision, and you must collect each exact report with {tools} using that "
        "assignment's agent_id and message_id before your final report. Continuing is not an "
        "extra assignment and grants no extra budget, permission or agent. A pending "
        "collection is not an answer, and an uncollected or incomplete report is not a "
        "delivery: every accepted assignment must be collected as completed."
    )


def handoff_property():
    return {
        "type": "string",
        "enum": list(HANDOFF_MODES),
        "default": AWAIT_REPORT,
        "description": (
            "Whether the host waits for every assignment's report inside this call "
            "(default) or returns the accepted identities immediately so you can continue "
            "your retained work and collect the reports yourself."
        ),
    }


@dataclass(frozen=True, slots=True)
class AssignmentRole:
    """One authorized assistant template this plan may instantiate.

    The host decides which roles exist and what each may do; the plan only
    chooses how many assignments of an authorized role to create and what work
    each carries.
    """

    role: str
    control: object
    collect_tool_name: str
    guidance: str
    writable: bool = False

    def fields(self):
        return {"assignment_id", "role", *CHILD_LIMITS} | ({"paths"} if self.writable else set())

    def planned_grant(self, owner):
        """This role's declared child grant, when the host policy states one."""
        planned = getattr(self.control.policy, "planned_grant", None)
        return None if planned is None else planned(owner)

    async def dispatch(self, assignment, main_work, context):
        work = {key: assignment[key] for key in CHILD_LIMITS}
        if self.writable:
            work["paths"] = assignment["paths"]
        return await self.control.delegate(
            {**work, "main_work": main_work},
            context,
            assignment_id=assignment["assignment_id"],
        )

    async def collect(self, handle, context):
        arguments = {"agent_id": handle["agent_id"], "message_id": handle["message_id"]}
        if self.writable:
            return await self.control.capture_and_collect(arguments, context)
        return await self.control.collect({**arguments, "wait_seconds": 0}, context)

    async def stop(self, agent_id, context):
        await self.control.stop({"agent_id": agent_id}, context)


def investigator_role(control: InvestigationControl) -> AssignmentRole:
    return AssignmentRole(
        INVESTIGATOR_ROLE, control, COLLECT_INVESTIGATION, INVESTIGATOR_GUIDANCE, writable=False
    )


def patch_author_role(control) -> AssignmentRole:
    return AssignmentRole(
        PATCH_AUTHOR_ROLE, control, COLLECT_CHILD_PATCH, WRITABLE_GUIDANCE, writable=True
    )


@dataclass(frozen=True)
class CollaborationPlanTool:
    """One plan, one batch: N authorized assistants with independent identities."""

    roles: tuple[AssignmentRole, ...]
    name: str = SUBMIT_COLLABORATION
    effect_kind: EffectKind = EffectKind.EXTERNAL_TRANSACTION
    # Process slots are leased per live Agent against every ancestor and are not
    # part of the Budget ledger, so a plan can only warn about them when the host
    # hands over the same authority its activations use.
    slots: object | None = None
    #: How long one call of this tool may hold in total. The host passes the same
    #: number its ToolRuntime enforces, so an `await_report` plan can never
    #: promise a wait the runtime will cut short.
    in_call_wait_seconds: float = CHILD_REPORT_WAIT_SECONDS

    def __post_init__(self):
        if not self.roles or len({role.role for role in self.roles}) != len(self.roles):
            raise ValueError("collaboration-plan-roles-invalid")

    @property
    def authorized(self):
        return {role.role: role for role in self.roles}

    @property
    def primary(self):
        """The control used for caller identity and Session reads."""
        return self.roles[0].control

    @property
    def collect_tool_names(self):
        return tuple(role.collect_tool_name for role in self.roles)

    @property
    def description(self):
        roles = "\n".join(f"{role.role}: {role.guidance}" for role in self.roles)
        return (
            planning_guidance()
            + "\n"
            + roles
            + "\n"
            + handoff_guidance(self.collect_tool_names)
        )

    @property
    def input_schema(self):
        assignment = {
            "assignment_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Plan-local key for this assignment; not a permission and not an identity."
                ),
            },
            "role": {"type": "string", "enum": [role.role for role in self.roles]},
            **text_properties(CHILD_LIMITS),
        }
        if any(role.writable for role in self.roles):
            assignment["paths"] = {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 128,
                "description": (
                    f"Exact relative files this assignment may deliver; {PATCH_AUTHOR_ROLE} "
                    "only, and no two assignments may declare the same file."
                ),
            }
        return {
            **object_schema(
                {
                    **{
                        name: text_properties(MAIN_LIMITS)[field]
                        for name, field in MAIN_WORK_ARGUMENTS.items()
                    },
                    "children": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_ASSIGNMENTS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": assignment,
                            "required": ["assignment_id", "role", *CHILD_LIMITS],
                        },
                    },
                    "handoff": handoff_property(),
                }
            ),
            "required": [*MAIN_WORK_ARGUMENTS, "children"],
        }

    def validate(self, arguments):
        """Whole-plan validation before any dispatch; raises ValueError only."""
        required = {*MAIN_WORK_ARGUMENTS, "children"}
        if type(arguments) is not dict or not required <= set(arguments) <= {*required, "handoff"}:
            raise ValueError("collaboration-plan-fields-invalid")
        if arguments.get("handoff", AWAIT_REPORT) not in HANDOFF_MODES:
            raise ValueError("collaboration-plan-handoff-invalid")
        main_work = plan_main_work(arguments)
        validate_text_fields(main_work, MAIN_LIMITS)
        children = arguments["children"]
        if type(children) is not list or not 1 <= len(children) <= MAX_ASSIGNMENTS:
            raise ValueError("collaboration-plan-assignment-count-invalid")
        seen: set[str] = set()
        owners: dict[str, str] = {}
        for entry in children:
            if type(entry) is not dict:
                raise ValueError("collaboration-assignment-invalid")
            spec = self.authorized.get(entry.get("role"))
            if spec is None:
                raise ValueError("collaboration-assignment-role-unauthorized")
            if set(entry) != spec.fields():
                # Which fields a role needs is not expressible in the shared schema
                # (``paths`` only for writable roles), so the refusal must say it:
                # a real Multi arm resubmitted five times without ``paths`` against
                # the bare code and spent its step budget guessing.
                missing = sorted(spec.fields() - set(entry)) or ["none"]
                unexpected = sorted(set(entry) - spec.fields()) or ["none"]
                raise ValueError(
                    "collaboration-assignment-fields-invalid: role "
                    f"{entry['role']} is missing {', '.join(missing)}; "
                    f"not allowed {', '.join(unexpected)}"
                )
            assignment_id = require_assignment_id(entry["assignment_id"])
            if assignment_id in seen:
                raise ValueError("collaboration-assignment-id-duplicate")
            seen.add(assignment_id)
            work = {key: entry[key] for key in CHILD_LIMITS}
            if not spec.writable:
                validate_work_fields({**work, "main_work": main_work})
                continue
            validate_assignment({**work, "paths": entry["paths"], "main_work": main_work})
            for path in freeze_changed_paths(entry["paths"], max_paths=128):
                if path in owners:
                    raise ValueError("collaboration-assignment-paths-overlap")
                owners[path] = assignment_id

    async def _require_capacity(self, children, owner):
        """Correctable pre-check; the Budget reservation remains authoritative.

        A batch reserves every assistant's whole allowance out of the caller's
        remaining capacity, on top of what its own in-flight Turn already holds.
        Checking that here turns "this batch cannot fit" into a correctable
        answer the model can act on, instead of a failed dispatch mid-batch.

        Returns the longest wall clock this host authorizes a single assistant in
        this batch to run, or None when it declares no grant. The batch runs
        concurrently, so that longest grant - not their sum - is how long waiting
        for all of them can legitimately take.
        """

        budgets = next(
            (
                getattr(role.control, "budgets", None)
                for role in self.roles
                if getattr(role.control, "budgets", None) is not None
            ),
            None,
        )
        if budgets is None:
            return None
        ledger = await budgets.ledger()
        account = ledger.account(owner.agent_id)
        if account is None or account.closed_seq is not None:
            # No open account declares no ceiling here; the original reservation
            # still owns admission and will refuse if it cannot be satisfied.
            return None
        remaining = ledger.available(owner.agent_id)
        if remaining.max_children is not None and len(children) > remaining.max_children:
            raise CollaborationPlanInputInvalid(
                f"this plan asks for {len(children)} assistants; "
                f"the host authorization has {remaining.max_children} left"
            )
        needed: dict[str, int] = {}
        retained_tokens = 0
        longest_child_wall: int | None = None
        for entry in children:
            grant = self.authorized[entry["role"]].planned_grant(owner)
            if grant is None:
                # This host declares no grant here, so only the original
                # reservation can judge the allowance. The live-Agent ceiling
                # below is independent of that and still applies.
                needed.clear()
                retained_tokens = 0
                longest_child_wall = None
                break
            limits = initial_token_limits(grant.limits, grant.initial_tokens)
            retained_tokens = max(retained_tokens, grant.retained_tokens)
            for field in _BATCH_DIMENSIONS:
                value = getattr(limits, field)
                if value is not None:
                    needed[field] = needed.get(field, 0) + value
            wall = limits.max_wall_milliseconds
            if wall is not None:
                longest_child_wall = max(longest_child_wall or 0, wall)
        for field, value in needed.items():
            left = getattr(remaining, field)
            if left is not None and value > left:
                raise CollaborationPlanInputInvalid(
                    f"this plan's {len(children)} assistants need {field}={value}, "
                    f"but only {left} remains for this Turn; ask for fewer assistants "
                    "or a smaller batch"
                )
        left_tokens = remaining.max_tokens
        if (
            left_tokens is not None
            and needed.get("max_tokens", 0) + retained_tokens > left_tokens
        ):
            raise CollaborationPlanInputInvalid(
                f"this plan's {len(children)} assistants need "
                f"max_tokens={needed.get('max_tokens', 0)} plus "
                f"retained_tokens={retained_tokens}, but only {left_tokens} remains "
                "for this Turn; ask for fewer assistants or a smaller batch"
            )
        await self._require_process_slots(len(children), owner, ledger)
        return longest_child_wall

    def _require_report_wait(self, longest_child_wall):
        """The deadline this call may hold, or a correctable "use the other mode".

        The host authorizes how long an assistant may run; this call may only
        hold for what its own runtime allows. When the authorization does not fit
        inside that, waiting here cannot deliver what the host granted, so the
        answer is the handoff mode that does - not a silently shortened wait that
        destroys assistants still inside their budget.
        """

        usable = self.in_call_wait_seconds - COLLECT_RESERVE_SECONDS
        if longest_child_wall is None:
            return max(min(CHILD_REPORT_WAIT_SECONDS, usable), 0)
        authorized = longest_child_wall / 1000
        if authorized > usable:
            raise CollaborationPlanInputInvalid(
                f"this plan's assistants may run up to {authorized:.0f}s each, but one "
                f"{self.name} call may hold for {usable:.0f}s; resubmit the same plan with "
                f"handoff={DISPATCH_AND_CONTINUE!r} and collect every assignment with "
                f"{' or '.join(self.collect_tool_names)}"
            )
        return authorized

    async def _require_process_slots(self, count, owner, ledger):
        """Every live assistant also holds one process slot per ancestor.

        That lease is refused when the Activation is created, which would end the
        whole run. Reading the same authority here turns "this batch needs more
        live Agents than the tree allows" into a correctable answer.
        """

        if self.slots is None:
            return
        from traceh.agents.directory import AgentDirectoryReader
        from traceh.supervision.lifecycle import AgentOwnershipGraph

        directory = await AgentDirectoryReader(self.primary.supervisor.store).load()
        for ancestor in AgentOwnershipGraph(directory).lineage(owner.agent_id):
            account = ledger.account(ancestor)
            limit = None if account is None else account.limits.max_processes
            if limit is None:
                continue
            free = limit - await self.slots.held(ancestor)
            if count > free:
                raise CollaborationPlanInputInvalid(
                    f"this plan needs {count} more live assistants, but the host allows "
                    f"{max(free, 0)} more under max_processes; ask for fewer assistants"
                )

    async def _converge(self, accepted, context, primary):
        """Stop every assistant this call started, without masking the primary error."""
        failures = []
        for row in accepted:
            spec = self.authorized[row["role"]]
            cleanup = asyncio.create_task(spec.stop(row["agent_id"], context))
            await await_worker_convergence(cleanup)
            if not cleanup.cancelled() and cleanup.exception() is not None:
                failures.append(cleanup.exception())
        if not failures:
            return
        if isinstance(primary, asyncio.CancelledError):
            # AgentLoop/Delivery recognize cancellation by its outer type.
            raise primary from failures[0]
        raise BaseExceptionGroup(
            "collaboration and cleanup failed", [primary, *failures]
        ) from None

    async def execute(self, arguments, context):
        await self.primary.authority.require_caller(context.session_id)
        sessions = SessionService(self.primary.supervisor.store)
        events = await sessions.read_session(context.session_id)
        view = read_view(
            events,
            session_id=context.session_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            through_seq=events[-1].seq,
        )
        calls = [
            e
            for e in events
            if e.type == "tool/call"
            and e.data.get("turn_id") == context.turn_id
            and e.data.get("tool_name") == self.name
        ]
        current_calls = [e for e in calls if e.data.get("step_id") == context.step_id]
        if (
            view is None
            or view[1].label != "collaboration-plan"
            or not any(e.data["tool_call_id"] == context.tool_call_id for e in current_calls)
        ):
            raise ValueError("collaboration-plan-binding-invalid")
        results = {
            (e.data["step_id"], e.data["tool_call_id"]): e.data
            for e in events
            if e.type == "tool/result" and e.data.get("tool_name") == self.name
        }
        for call in calls:
            if call.data.get("step_id") == context.step_id:
                continue
            result = results.get((call.data["step_id"], call.data["tool_call_id"]), {})
            if not correctable_plan_result(result):
                raise ValueError("collaboration-plan-already-dispatched-or-unresolved")
        if len(current_calls) != 1:
            raise CollaborationPlanInputInvalid(
                "Submit one plan, not multiple plans in a batch."
            )
        try:
            self.validate(arguments)
        except ValueError as error:
            raise CollaborationPlanInputInvalid(str(error)) from error
        handoff = arguments.get("handoff", AWAIT_REPORT)
        owner = await self.primary.authority.require_caller(context.session_id)
        longest_child_wall = await self._require_capacity(arguments["children"], owner)
        # Decided before dispatch: a plan that cannot be waited for must be
        # corrected, not started and then cut short.
        report_wait = (
            self._require_report_wait(longest_child_wall) if handoff == AWAIT_REPORT else None
        )
        accepted: list[dict] = []
        try:
            for entry in arguments["children"]:
                spec = self.authorized[entry["role"]]
                receipt = await spec.dispatch(entry, plan_main_work(arguments), context)
                accepted.append(
                    {
                        "assignment_id": entry["assignment_id"],
                        "role": entry["role"],
                        "agent_id": receipt.data["agent_id"],
                        "message_id": receipt.data["message_id"],
                        "status": "accepted",
                    }
                )
        except BaseException as primary:
            # Partial dispatch is not a transaction: converge what started, keep
            # the facts it already produced, and report the failure as is.
            await self._converge(accepted, context, primary)
            raise
        if handoff == DISPATCH_AND_CONTINUE:
            # Every assistant now runs under its own Activation, Budget and
            # Session. This call owns no waiter, so there is nothing to converge
            # here; the original owned-tree disposal still stops them.
            dispatched = {
                "plan": arguments,
                "children": accepted,
                "outcome": "dispatched",
                "next_action": (
                    "Continue your retained work now, then collect every assignment with "
                    f"{' or '.join(self.collect_tool_names)} using its exact agent_id and "
                    "message_id. No report exists until you collect it."
                ),
            }
            return ToolOutput(canonical_json(dispatched), dispatched)
        result = {"plan": arguments, "children": accepted, "outcome": "child_incomplete"}
        try:
            # One shared deadline for the whole batch: waiting for several
            # assistants must not multiply the total time this call may hold.
            async with asyncio.timeout(report_wait):
                for row in accepted:
                    await self.authorized[row["role"]].control.supervisor.wait_message(
                        row["agent_id"], row["message_id"]
                    )
            evidence: list[str] = []
            reports = []
            for row in accepted:
                report = await self.authorized[row["role"]].collect(row, context)
                reports.append({**row, **json.loads(report.content)})
                evidence.extend(report.evidence or ())
            result["children"] = reports
            if all(
                row["status"] == "completed" and row["reason"] == "completed" for row in reports
            ):
                result["outcome"] = "completed"
            return ToolOutput(canonical_json(result), result, evidence=tuple(evidence))
        except BaseException as primary:
            await self._converge(accepted, context, primary)
            raise
