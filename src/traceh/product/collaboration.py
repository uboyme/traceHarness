"""Multi execution policy derived from the main Session, not mutable flags."""

from traceh.product.verification_review import REVIEW_GUIDANCE, execution_receipts
from traceh.runtime.continuation import Continue, DefaultContinuationRuntime, Finish
from traceh.runtime.step_view import StepViewSelection
from traceh.session.request_view import VIEW_EVENT, RequestView
from traceh.session.service import SessionService
from traceh.supervision.delegation import COLLECT_INVESTIGATION, INVESTIGATION_TOOL_IDS
from traceh.supervision.investigation_work import INVESTIGATOR_ROLE, PATCH_AUTHOR_ROLE
from traceh.supervision.structured_collaboration import (
    DISPATCH_AND_CONTINUE,
    SUBMIT_COLLABORATION,
    correctable_plan_result,
)
from traceh.supervision.writable_collaboration import COLLECT_CHILD_PATCH

READ_TOOLS = frozenset({"list_files", "read_file", "search_text"})
COLLECT_BY_ROLE = {INVESTIGATOR_ROLE: COLLECT_INVESTIGATION, PATCH_AUTHOR_ROLE: COLLECT_CHILD_PATCH}
COLLECT_TOOLS = tuple(COLLECT_BY_ROLE.values())
DELEGATION_TOOLS = (*INVESTIGATION_TOOL_IDS, SUBMIT_COLLABORATION, COLLECT_CHILD_PATCH)
PLAN = "collaboration-plan"
EXECUTE = "collaboration-execute"
REVIEW = "collaboration-review"
WRAP_UP = "collaboration-wrap-up"
MAIN_WRAP_UP_GUIDANCE = (
    "Your budget for this task is nearly spent, so the working tools have been "
    "withdrawn. Deliver now from the state your workspace is already in: say what "
    "you changed, what you actually verified and how, and what remains unverified "
    "or unfinished. Do not describe intended work as completed, and do not claim a "
    "check you did not run. The host's own fixed verification runs after you "
    "deliver; an honest, incomplete handover is worth more than being cut off "
    "mid-sentence."
)

CONCURRENT_CHILD_GUIDANCE = (
    "The assignments you dispatched run concurrently; the host is not holding any report "
    "for you. Do the retained work that does not depend on them, then collect each exact "
    "report with {collect} using the agent_id and message_id from your accepted dispatch. "
    "A pending collection is not an answer and not a failure: continue useful work and "
    "collect again, and collecting one assignment never stands in for another. You cannot "
    "deliver until every accepted assignment has been collected as completed, and "
    "collecting grants no extra budget, agent or permission."
)


def views(events, turn_id):
    return [e for e in events if e.type == VIEW_EVENT and e.data.get("turn_id") == turn_id]


def accepted_plan(events, turn_id):
    """The one accepted plan call/result pair of this Turn, from original events."""
    steps = {e.data["step_id"] for e in views(events, turn_id) if e.data["label"] == PLAN}
    results = {
        (e.data["step_id"], e.data["tool_call_id"]): e
        for e in events
        if e.type == "tool/result"
        and e.data.get("step_id") in steps
        and e.data.get("tool_name") == SUBMIT_COLLABORATION
        and e.data.get("status") == "succeeded"
    }
    calls = [
        e
        for e in events
        if e.type == "tool/call"
        and (e.data.get("step_id"), e.data.get("tool_call_id")) in results
    ]
    if len(calls) != 1:
        return None
    return calls[0], results[(calls[0].data["step_id"], calls[0].data["tool_call_id"])]


def dispatched_and_continuing(events, turn_id):
    """True when the accepted plan explicitly asked for concurrent assistants."""
    accepted = accepted_plan(events, turn_id)
    if accepted is None:
        return False
    arguments = accepted[0].data.get("arguments")
    return type(arguments) is dict and arguments.get("handoff") == DISPATCH_AND_CONTINUE


def dispatched_roles(events, turn_id):
    """The roles a concurrent plan actually asked for, from its recorded call."""
    if not dispatched_and_continuing(events, turn_id):
        return frozenset()
    children = accepted_plan(events, turn_id)[0].data["arguments"].get("children")
    if type(children) is not list:
        return frozenset()
    return frozenset(
        entry["role"]
        for entry in children
        if type(entry) is dict and type(entry.get("role")) is str
    )


def _every_report_collected(events, turn_id) -> bool:
    """Whether the delivery contract still needs a collect tool to be reachable."""

    if not dispatched_and_continuing(events, turn_id):
        return True
    accepted = accepted_plan(events, turn_id)
    if accepted is None:
        return True
    arguments = accepted[0].data.get("arguments") or {}
    children = arguments.get("children")
    if type(children) is not list or not children:
        return True
    steps = {
        e.data["step_id"]
        for e in events
        if e.type == "step/start" and e.data.get("turn_id") == turn_id
    }
    collected = sum(
        1
        for e in events
        if e.type == "tool/result"
        and e.data.get("step_id") in steps
        and e.data.get("tool_name") in COLLECT_TOOLS
        and e.data.get("status") == "succeeded"
        and (e.data.get("data") or {}).get("status") == "completed"
    )
    return collected >= len(children)


class CollaborationPolicy:
    def __init__(self, delivery=None, *, verifier=None, wrap_up=None, collaboration_required=True):
        self.delivery = delivery
        self.verifier = verifier
        #: Budget held back so the main Agent can finish rather than be cut off
        #: mid-sentence. A real trial ended with it cancelled on its own wall
        #: clock while saying "let me do final verification": all the work done,
        #: none of it delivered. ``None`` keeps the historical behaviour.
        self.wrap_up = wrap_up
        self.collaboration_required = collaboration_required

    async def select(self, events, composition, *, turn_id, step_id):
        wrapped = self._wrap_up_view(events, composition, turn_id)
        if wrapped is not None:
            return StepViewSelection(wrapped, self.verifier)
        view = await self._select(events, composition, turn_id=turn_id, step_id=step_id)
        return StepViewSelection(view, self.verifier if view.label == REVIEW else None)

    def _wrap_up_view(self, events, composition, turn_id):
        """Withdraw the working tools once the reserve is reached.

        The collect controls survive until every dispatched assignment has been
        collected: the delivery contract requires those reports, so taking the
        tool away before they exist would make the run unsatisfiable rather than
        merely short. Everything else goes, because a prompt asking the model to
        wrap up is advice and the trial showed it working right up to the cut.
        """

        if self.wrap_up is None or not self.wrap_up.reached(events, turn_id):
            return None
        available = {tool.name for tool in composition.tools}
        keep = (
            frozenset()
            if _every_report_collected(events, turn_id)
            else frozenset(name for name in COLLECT_TOOLS if name in available)
        )
        return RequestView(
            WRAP_UP,
            tuple(name for name in (t.name for t in composition.tools) if name in keep),
            composition.system_prompt + "\n" + MAIN_WRAP_UP_GUIDANCE,
        )

    async def _select(self, events, composition, *, turn_id, step_id):
        del step_id
        if not self.collaboration_required:
            return RequestView(
                "coding", tuple(tool.name for tool in composition.tools), composition.system_prompt
            )
        prior = views(events, turn_id)
        execution_steps = {e.data["step_id"] for e in prior if e.data["label"] == EXECUTE}
        preparation_steps = {e.data["step_id"] for e in prior if e.data["label"] == PLAN}
        if any(e.data["label"] == REVIEW for e in prior) or any(
            e.type == "assistant/message"
            and e.data.get("step_id") in execution_steps
            and not e.data.get("tool_calls")
            for e in events
        ):
            label = REVIEW
        elif any(e.data["label"] == EXECUTE for e in prior) or any(
            e.type == "tool/result"
            and e.data.get("step_id") in preparation_steps
            and e.data.get("tool_name") == SUBMIT_COLLABORATION
            and e.data.get("status") == "succeeded"
            for e in events
        ):
            label = EXECUTE
        else:
            label = PLAN
        if label == PLAN:
            plan = next(t for t in composition.tools if t.name == SUBMIT_COLLABORATION)
            return RequestView(
                label,
                tuple(t.name for t in composition.tools if t.name in READ_TOOLS | {plan.name}),
                composition.system_prompt + "\n"
                "Inspect the available sources to understand the user's requirement. "
                "Choose how much investigation is needed within the existing total budget; "
                "you may batch readonly calls and continue investigating after a denied or "
                "invalid call. When you can define useful responsibilities, submit the plan. "
                "Do not do the child's whole assignment just to ask it to repeat your work. "
                "Implementation starts after the bound child handoff completes.\n"
                + plan.description,
            )
        available = {t.name for t in composition.tools}
        # Concurrency exposes exactly the collect controls the accepted plan's
        # roles need; every other delegation control stays hidden.
        collect = frozenset(
            COLLECT_BY_ROLE.get(role, "")
            for role in dispatched_roles(events, turn_id)
            if COLLECT_BY_ROLE.get(role, "") in available
        )
        return RequestView(
            label,
            tuple(
                t.name
                for t in composition.tools
                if t.name not in DELEGATION_TOOLS or t.name in collect
            ),
            composition.system_prompt
            + "\n"
            + (
                CONCURRENT_CHILD_GUIDANCE.format(collect=" / ".join(sorted(collect))) + "\n"
                if collect
                else ""
            )
            + (
                "The host has returned the report for the bound child assignment. "
                "Inspect its\nstatus, findings, evidence, and limitations before us"
                "ing it. Read retained\ncontent when the preview is insufficient. C"
                "heck the evidence needed for your\nnext action, then complete your"
                " retained work and the final verification.\nDo not redo the entire"
                " child assignment without a concrete inconsistency or\nmissing ite"
                "m. Report what you used and any remaining uncertainty. Never clai"
                "m\nchild completion, tests, or approval solely because a tool retu"
                "rned successfully."
                if label == EXECUTE else ""
            )
            + (
                "\n"
                + REVIEW_GUIDANCE
                + execution_receipts(
                    events,
                    turn_id,
                    delivery=None if self.delivery is None else await self.delivery.read(),
                )
                if label == REVIEW
                else ""
            ),
        )


class CollaborationPlanInvalid(RuntimeError):
    """The required plan was absent or structurally invalid."""


class CollaborationDispatchFailed(RuntimeError):
    """The required child could not be dispatched or its report retrieved."""


class CollaborationChildIncomplete(RuntimeError):
    """A terminal child did not complete its assigned work."""


class CollaborationChildDeliveryEmpty(RuntimeError):
    """A child's Turn ended well but it handed back nothing to act on.

    A lifecycle that reached ``completed`` says the child stopped cleanly; it
    does not say the child produced a finding. Accepting a blank statement here
    is what let a main Agent report a successful collaboration it had received
    no evidence from. This is a terminal stop for the whole multi: the main
    Agent cannot repair a child's delivery, so re-collecting or re-dispatching
    would only spend more budget on the same empty answer.
    """


class CollaborationExecutionStopped(RuntimeError):
    """A bounded stop cannot count as completed multi execution."""


class CollaborationContinuation:
    def __init__(self, store, session_id, *, inner=None):
        self.sessions = SessionService(store)
        self.session_id = session_id
        self.inner = inner if inner is not None else DefaultContinuationRuntime()

    async def decide(self, **kwargs):
        normal = await self.inner.decide(**kwargs)
        if isinstance(normal, Finish) and normal.reason != "completed":
            raise CollaborationExecutionStopped(normal.reason)
        events = await self.sessions.read_session(self.session_id)
        starts = [e for e in events if e.type == "step/start"]
        current = starts[-1].data
        prior = views(events, current["turn_id"])
        if not prior:
            raise ValueError("collaboration-view-missing")
        label = prior[-1].data["label"]
        if label == PLAN:
            if not kwargs["response"].tool_calls:
                raise CollaborationPlanInvalid("collaboration-plan-required-before-completion")
            results = [
                e
                for e in events
                if e.type == "tool/result"
                and e.data.get("step_id") == current["step_id"]
                and e.data.get("tool_name") == SUBMIT_COLLABORATION
            ]
            if not results or all(correctable_plan_result(e.data) for e in results):
                return normal
            if (
                len(results) != 1
                or results[0].data.get("tool_name") != SUBMIT_COLLABORATION
                or results[0].data.get("status") != "succeeded"
            ):
                if len(results) == 1 and results[0].data.get("status") == "failed":
                    raise CollaborationDispatchFailed("collaboration-dispatch-failed")
                raise CollaborationPlanInvalid("collaboration-plan-invalid-or-missing")
            result = await self._payload(results[0], events)
            # A concurrent dispatch is accepted work, not a delivered report: the
            # collected report is required below before this Turn may complete.
            if type(result) is not dict or result.get("outcome") not in {
                "completed",
                "dispatched",
            }:
                raise CollaborationChildIncomplete("collaboration-child-incomplete")
            return Continue()
        # Judge every report this Step actually collected, before deciding
        # anything else. Waiting for the model to announce completion made this
        # a final-delivery check: in one real trial the main Agent held a
        # "failed" report at 04:55 and went on to spend 17 more model calls and
        # 562,727 tokens over the next 18 minutes before the delivery gate
        # refused it. The answer was already known; the spending was not.
        # Judge every report this Step actually collected, before deciding
        # anything else. Waiting for the model to announce completion made this
        # a final-delivery check: in one real trial the main Agent held a
        # "failed" report at 04:55 and went on to spend 17 more model calls and
        # 562,727 tokens over the next 18 minutes before the delivery gate
        # refused it. The answer was already known; the spending was not.
        await self._stop_on_undeliverable_report(events, current)
        if label == EXECUTE and isinstance(normal, Finish) and normal.reason == "completed":
            return Continue()
        if isinstance(normal, Finish) and normal.reason == "completed":
            await self._require_collected_report(events, current["turn_id"])
        return normal

    async def _stop_on_undeliverable_report(self, events, current) -> None:
        """End the run on the first collected report that can never be delivered.

        ``status`` on the collect *tool result* only says the host managed to
        fetch a report; the child's own terminal state lives inside that report.
        Conflating the two is what let a failed assistant look like a successful
        collection. Only a report bound to an assignment this Turn actually
        dispatched can stop the run, so a stray payload cannot.
        """

        turn_id = current["turn_id"]
        if not dispatched_and_continuing(events, turn_id):
            return
        accepted = accepted_plan(events, turn_id)
        if accepted is None:
            return
        dispatch = await self._payload(accepted[1], events)
        children = dispatch.get("children") if type(dispatch) is dict else None
        if type(children) is not list:
            return
        dispatched = {
            (child.get("agent_id"), child.get("message_id"))
            for child in children
            if type(child) is dict
        }
        for event in events:
            if (
                event.type != "tool/result"
                or event.data.get("step_id") != current["step_id"]
                or event.data.get("tool_name") not in COLLECT_TOOLS
                or event.data.get("status") != "succeeded"
            ):
                continue
            report = await self._payload(event, events)
            if type(report) is not dict:
                continue
            if (report.get("agent_id"), report.get("message_id")) not in dispatched:
                continue
            status = report.get("status")
            if status == "pending":
                # Still running is not a terminal verdict; the existing wait and
                # wall-clock boundaries own that case.
                continue
            if status != "completed" or report.get("reason") != "completed":
                raise CollaborationChildIncomplete("collaboration-child-terminal-failure")
            statement = report.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                raise CollaborationChildDeliveryEmpty("collaboration-child-delivery-empty")

    async def _payload(self, result, events):
        """Read one Tool result's original data, including retained output."""
        data = result.data
        if "output_ref" in data:
            from traceh.session.tool_output import resolve_tool_output

            ref = data["output_ref"]
            data = resolve_tool_output(
                events,
                await self.sessions.read_effects(self.session_id),
                session_id=self.session_id,
                effect_id=ref["effect_id"],
                digest=ref["digest"],
            )
        return data.get("data")

    async def _require_collected_report(self, events, turn_id):
        """Every dispatched assignment must have its own collected completed report."""
        if not dispatched_and_continuing(events, turn_id):
            return
        dispatch = await self._payload(accepted_plan(events, turn_id)[1], events)
        children = dispatch.get("children") if type(dispatch) is dict else None
        if type(children) is not list or not children:
            raise CollaborationChildIncomplete("collaboration-dispatch-receipt-invalid")
        pending = {}
        for child in children:
            if type(child) is not dict or not {"agent_id", "message_id"} <= set(child):
                raise CollaborationChildIncomplete("collaboration-dispatch-receipt-invalid")
            pending[(child["agent_id"], child["message_id"])] = child.get("assignment_id")
        steps = {
            e.data["step_id"]
            for e in events
            if e.type == "step/start" and e.data.get("turn_id") == turn_id
        }
        for event in events:
            if (
                event.type != "tool/result"
                or event.data.get("step_id") not in steps
                or event.data.get("tool_name") not in COLLECT_TOOLS
                or event.data.get("status") != "succeeded"
            ):
                continue
            report = await self._payload(event, events)
            if (
                type(report) is dict
                and report.get("status") == "completed"
                and report.get("reason") == "completed"
            ):
                identity = (report.get("agent_id"), report.get("message_id"))
                if identity not in pending:
                    continue
                statement = report.get("statement")
                if not isinstance(statement, str) or not statement.strip():
                    # Stop on the first proven-empty delivery rather than
                    # letting the main Agent collect its way around it.
                    raise CollaborationChildDeliveryEmpty(
                        "collaboration-child-delivery-empty"
                    )
                # One collected report settles its own exact assignment only; it
                # can never stand in for a sibling.
                pending.pop(identity, None)
        if pending:
            raise CollaborationChildIncomplete("collaboration-child-report-not-collected")
