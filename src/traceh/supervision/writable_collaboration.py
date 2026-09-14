"""Writable handoff adapter over the original Supervisor and Artifact owners."""

import asyncio
from dataclasses import dataclass

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.inbox import AgentInboxReader
from traceh.api.agents import AgentMessage, MessageTarget
from traceh.api.json_types import canonical_json
from traceh.api.tools import EffectKind, ToolOutput
from traceh.budgets.enforcement import _await_owned
from traceh.concurrency import await_worker_convergence
from traceh.supervision.authority import AgentToolBindingError
from traceh.supervision.errors import AgentMessageNotSettledError
from traceh.supervision.execution import durable_log_identity
from traceh.supervision.investigation_work import require_assignment_id
from traceh.supervision.tools import _operation_id, _report_data
from traceh.supervision.writable_work import validate_writable_work, work_content
from traceh.workspaces.catalog import WorkspaceCatalog
from traceh.workspaces.events import WORKSPACE_CATALOG_STREAM

COLLECT_CHILD_PATCH = "collect_child_patch"
CHILD_PATCH_WAIT_MAX_SECONDS = 30
COLLECT_CHILD_PATCH_GUIDANCE = (
    "Collect the exact report of the patch author you dispatched with this agent_id and "
    "message_id. wait_seconds waits at most "
    f"{CHILD_PATCH_WAIT_MAX_SECONDS} seconds for that one message to settle; zero only "
    "polls. A pending report is not an answer: continue your retained work and collect "
    "again. When the child has completed, the host captures its actual immutable Patch and "
    "returns the real Artifact identity, digest, base revision and changed paths. Repeated "
    "collection returns the same identity and captures nothing twice. A completed report is "
    "a child claim with evidence, not verified truth, integration or approval: read the "
    "original Patch and integrate it explicitly before final verification."
)
WRITABLE_GUIDANCE = (
    "A patch_author assignment is an independent writable assistant. Allocate a substantive "
    "code change and exact relative file paths to it; retain integration, implementation "
    "and functional verification responsibilities for yourself. State goal, scope, exclusions, "
    "and checkable acceptance conditions in the existing deliverable and briefing fields: "
    "required input/output behavior, relevant invalid inputs and error behavior, mutation "
    "constraints, and permitted file changes. Preserve requirements that apply to the whole "
    "input, not just its individual items. Name the positive and negative checks the main "
    "agent will execute; do not ask the child to claim tests it cannot run. "
    "Explain how you will use the child result. Do not duplicate the whole "
    "task or ask it to restate known answers. Identified unread sources may be assigned for "
    "inspection; do not invent evidence or require the child to see your uncommitted edits. "
    "The child works on the host-bound original revision with read/search/apply_patch only. "
    "No shell, network, installation, recursive delegation, approval or promotion is granted. "
    "The host waits for its completed message and captures the actual immutable Patch. "
    "Receiving a Patch does not apply it or prove correctness. You must inspect its original "
    "content and explicitly integrate it using granted tools before final verification. "
    "Writable assignments must not declare overlapping paths: each file belongs to one "
    "assistant, and the host refuses an overlapping plan before any dispatch."
)


class WritableControl:
    def __init__(self, *, supervisor, authority, policy, capture, workspaces, budgets):
        identity = durable_log_identity(supervisor.store)
        if any(
            durable_log_identity(owner.store) is not identity
            for owner in (authority, capture, workspaces, budgets)
        ):
            raise AgentToolBindingError("writable-store-mismatch")
        self.supervisor = supervisor
        self.authority = authority
        self.policy = policy
        self.capture = capture
        self.workspaces = workspaces
        self.budgets = budgets

    async def child(self, agent_id, context):
        owner = await self.authority.require_caller(context.session_id)
        child = await self.authority.require_owned(agent_id, context.session_id)
        if child.owner_agent_id != owner.agent_id:
            raise AgentToolBindingError("writable-child-not-direct")
        binding = await self.policy.validate_child(owner, child)
        return owner, child, binding

    async def delegate(self, arguments, context, *, assignment_id=None):
        owner = await self.authority.require_caller(context.session_id)
        binding = self.policy.prepare(owner)
        if (
            binding.spec.owner_agent_id != owner.agent_id
            or binding.spec.forked_from_session_id is not None
        ):
            raise AgentToolBindingError("writable-owner-invalid")
        # Each plan entry derives its own identities from the caller's exact call
        # plus that entry's key, so one plan can hold several patch authors and a
        # repeated entry can never resolve to a second live Agent.
        discriminator = "" if assignment_id is None else require_assignment_id(assignment_id)
        child_id = _operation_id("patch-author", owner.agent_id, context, discriminator)
        session_id = _operation_id("patch-session", owner.agent_id, context, discriminator)
        message_id = _operation_id("patch-message", owner.agent_id, context, discriminator)
        content = work_content(
            arguments,
            binding,
            owner_id=owner.agent_id,
            child_id=child_id,
            session_id=session_id,
            message_id=message_id,
            assignment_id=_operation_id("patch-assignment", owner.agent_id, context)
            if assignment_id is None
            else assignment_id,
        )
        directory = await AgentDirectoryReader(self.supervisor.store).load()
        if any(child.agent_id == child_id for child in directory.children_of(owner.agent_id)):
            raise AgentToolBindingError("writable-assignment-already-dispatched")
        handle = await self.supervisor.create(
            binding.spec,
            # The creation request identity must also separate assignments, or a
            # second entry reuses one request id for a different Agent.
            request_id=_operation_id("patch-create", owner.agent_id, context, discriminator),
            agent_id=child_id,
            session_id=session_id,
        )
        try:
            await self.child(handle.agent_id, context)
            await self.supervisor.send(
                handle.agent_id,
                AgentMessage(
                    message_id=message_id,
                    content=content,
                    source=owner.agent_id,
                    correlation_id=context.turn_id,
                    causation_id=context.tool_call_id,
                ),
                target=MessageTarget.NEW_TURN,
                wakeup=True,
            )
        except BaseException as primary:
            cleanup = asyncio.create_task(self.supervisor.dispose(handle.agent_id))
            await await_worker_convergence(cleanup)
            if not cleanup.cancelled() and cleanup.exception() is not None:
                if isinstance(primary, asyncio.CancelledError):
                    raise primary from cleanup.exception()
                raise BaseExceptionGroup(
                    "writable send and cleanup failed", [primary, cleanup.exception()]
                ) from None
            raise
        data = {
            "agent_id": child_id,
            "message_id": message_id,
            "status": "accepted",
            "next_action": f"{COLLECT_CHILD_PATCH} with this exact agent_id and message_id",
        }
        return ToolOutput(canonical_json(data), data)

    async def evidence(self, arguments, context):
        owner, child, binding = await self.child(arguments["agent_id"], context)
        inbox = await AgentInboxReader(self.supervisor.store).load(child.agent_id)
        accepted = inbox.get(arguments["message_id"])
        if (
            accepted is None
            or accepted.message.source != owner.agent_id
            or len(inbox.messages) != 1
        ):
            raise AgentToolBindingError("writable-message-not-single-owned-work")
        work = validate_writable_work(
            accepted.message.content,
            binding,
            owner_id=owner.agent_id,
            child_id=child.agent_id,
            session_id=child.session_id,
            message_id=arguments["message_id"],
        )
        report = await self.supervisor.report(child.agent_id, arguments["message_id"])
        if (
            report.agent_id != child.agent_id
            or report.session_id != child.session_id
            or report.message_id != arguments["message_id"]
        ):
            raise AgentToolBindingError("writable-report-mismatch")
        return child, binding, work, report

    async def capture_and_collect(self, arguments, context):
        child, _, _, report = await self.evidence(arguments, context)
        if report.status == "completed" and report.reason == "completed":
            if not await self.capture.reader.refs_for(child.agent_id, report.message_id):
                await self.capture.capture(child.agent_id, report.message_id)
        return await self.collect(arguments, context)

    async def collect(self, arguments, context):
        child, binding, work, report = await self.evidence(arguments, context)
        data = {
            **_report_data(report),
            "statement": report.final_text,
            "source_id": binding.source_id,
            "revision": binding.revision,
            "input_digest": work["input_digest"],
            "artifact": None,
        }
        if report.status == "completed" and report.reason == "completed":
            refs = await self.capture.reader.refs_for(child.agent_id, report.message_id)
            if len(refs) != 1:
                raise AgentToolBindingError("writable-artifact-not-captured")
            artifact = await self.capture.reader.resolve_reference(refs[0])
            manifest = artifact.manifest
            workspace = (await self.workspaces.catalog()).for_agent(child.agent_id)
            historical = WorkspaceCatalog.rebuild(
                tuple(
                    e
                    for e in await self.workspaces.store.read(WORKSPACE_CATALOG_STREAM)
                    if e.seq <= manifest.workspace_generation
                )
            ).for_agent(child.agent_id)
            if (
                workspace is None
                or manifest.session_id != child.session_id
                or manifest.workspace_id != workspace.workspace_id
                or historical is None
                or historical.status.value != "attached"
                or historical.updated_seq != manifest.workspace_generation
                or historical.workspace_id != workspace.workspace_id
                or manifest.base_revision != binding.revision
                or manifest.repository_fingerprint != workspace.repository_fingerprint
                or not manifest.changed_paths
                or not set(manifest.changed_paths) <= set(work["paths"])
            ):
                raise AgentToolBindingError("writable-artifact-binding-or-scope-invalid")
            data["artifact"] = {
                "artifact_id": manifest.artifact_id,
                "patch_digest": manifest.blob.sha256,
                "base_revision": manifest.base_revision,
                "agent_id": child.agent_id,
                "session_id": child.session_id,
                "message_id": report.message_id,
                "changed_paths": list(manifest.changed_paths),
            }
        return ToolOutput(canonical_json(data), data, evidence=report.evidence_refs)

    async def stop(self, arguments, context):
        _, child, _ = await self.child(arguments["agent_id"], context)

        async def finish():
            await self.supervisor.dispose(child.agent_id)
            account = (await self.budgets.ledger()).account(child.agent_id)
            if account is not None and account.closed_seq is None:
                await self.budgets.close_account(
                    operation_id=f"patch-stop-budget-{child.agent_id}", agent_id=child.agent_id
                )

        await _await_owned(finish(), name="writable-child-stop")
        return ToolOutput("Patch author stopped.", {"agent_id": child.agent_id})


@dataclass(frozen=True)
class ChildPatchCollectTool:
    """The main's on-demand collection of the one concurrently dispatched author.

    Capture, identity and Artifact facts keep their existing owners; this only
    lets the main decide when to wait, instead of the dispatch call waiting.
    """

    control: WritableControl
    name: str = COLLECT_CHILD_PATCH
    effect_kind: EffectKind = EffectKind.EXTERNAL_TRANSACTION

    @property
    def description(self):
        return COLLECT_CHILD_PATCH_GUIDANCE

    @property
    def input_schema(self):
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agent_id": {"type": "string"},
                "message_id": {"type": "string"},
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": CHILD_PATCH_WAIT_MAX_SECONDS,
                },
            },
            "required": ["agent_id", "message_id", "wait_seconds"],
        }

    async def execute(self, arguments, context):
        wait_seconds = arguments.get("wait_seconds")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= CHILD_PATCH_WAIT_MAX_SECONDS:
            raise ValueError(
                f"wait_seconds must be an integer from 0 to {CHILD_PATCH_WAIT_MAX_SECONDS}"
            )
        handoff = {"agent_id": arguments["agent_id"], "message_id": arguments["message_id"]}
        # The original control checks owner, direct ownership and source binding
        # before any wait, so a foreign message cannot park this tool call.
        _, child, _ = await self.control.child(handoff["agent_id"], context)
        if wait_seconds:
            timeout = asyncio.timeout(wait_seconds)
            try:
                async with timeout:
                    await self.control.supervisor.wait_message(
                        child.agent_id, handoff["message_id"]
                    )
            except TimeoutError:
                if not timeout.expired():
                    raise
        try:
            return await self.control.capture_and_collect(handoff, context)
        except AgentMessageNotSettledError:
            pending = {
                **handoff,
                "status": "pending",
                "artifact": None,
                "next_action": (
                    "Continue your retained work or collect later; "
                    "no final report or Patch exists yet."
                ),
            }
            return ToolOutput(canonical_json(pending), pending)
