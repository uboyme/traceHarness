"""Explicit child Patch reading and integration through existing fact owners."""

import json
from dataclasses import dataclass, replace

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelRequest
from traceh.api.tools import EffectKind, ToolExecutionCancelled, ToolExecutionFailure, ToolOutput
from traceh.api.workspace_edits import WorkspaceEditCancelled, WorkspaceEditPlan
from traceh.artifacts.materialize import materialize_patch
from traceh.session.service import SessionService
from traceh.session.tool_output import output_reference, resolve_tool_output
from traceh.supervision.authority import AgentToolBindingError
from traceh.supervision.tools import _operation_id

PATCH_INTEGRATION_TOOLS = ("read_child_patch", "integrate_child_patch", "inspect_patch_integration")
PATCH_PAGE_DEFAULT_CHARS = 2000
PATCH_PAGE_MAX_CHARS = 4000


def _payload(outcome, events, effects, session_id):
    ref = output_reference(outcome)
    if ref is not None:
        return resolve_tool_output(
            events, effects, session_id=session_id, effect_id=ref["effect_id"], digest=ref["digest"]
        )
    return outcome.data


def _received_page(outcome, payload, events):
    """Actual frozen tool input, including ordinary retained-output read pages."""
    source = payload["content"]
    intervals = []
    ref = output_reference(outcome)
    for event in events:
        if event.type != "request/snapshot":
            continue
        raw = event.data["dispatch_request"]
        request = ModelRequest.from_dict(raw)
        if fingerprint(request.to_dict()) != event.data["dispatch_fingerprint"]:
            raise AgentToolBindingError("patch-read-request-invalid")
        for message in request.messages:
            if message.role != "tool":
                continue
            if (
                message.tool_call_id == outcome.data["tool_call_id"]
                and message.name == "read_child_patch"
                and message.content == source
            ):
                return True
            if message.name != "read_tool_output" or ref is None:
                continue
            try:
                page = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            if (
                page.get("effect_id") == ref["effect_id"]
                and page.get("digest") == ref["digest"]
                and page.get("part") == "content"
            ):
                start, end = page.get("offset"), page.get("end_offset")
                if (
                    type(start) is int
                    and type(end) is int
                    and 0 <= start <= end <= len(source)
                    and page.get("text") == source[start:end]
                ):
                    intervals.append((start, end))
    through = 0
    for start, end in sorted(intervals):
        if start > through:
            break
        through = max(through, end)
    return through == len(source)


class PatchIntegrationControl:
    def __init__(self, writable, limits):
        self.writable = writable
        self.limits = limits
        self.sessions = SessionService(writable.supervisor.store)

    async def plan(self, artifact_id, context):
        artifact = await self.writable.capture.reader.load(artifact_id)
        manifest = artifact.manifest
        collected = await self.writable.collect(
            {"agent_id": manifest.agent_id, "message_id": manifest.message_id}, context
        )
        if collected.data.get("artifact", {}).get("artifact_id") != artifact_id:
            raise AgentToolBindingError("patch-not-owned-handoff")
        return await self._target_plan(artifact, context.session_id, context.workspace)

    async def _target_plan(self, artifact, session_id, workspace):
        owner = await self.writable.authority.require_caller(session_id)
        manifest = artifact.manifest
        handle = await self.writable.workspaces.resolve_for_session(session_id)
        record = (await self.writable.workspaces.catalog()).get(handle.workspace_id)
        if (
            handle.agent_id != owner.agent_id
            or handle.root != workspace
            or record.repository_fingerprint != manifest.repository_fingerprint
            or handle.base_revision != manifest.base_revision
        ):
            raise AgentToolBindingError("patch-target-binding-invalid")
        files = await materialize_patch(artifact, handle.root, self.limits)
        return artifact, WorkspaceEditPlan(
            manifest.artifact_id,
            manifest.manifest_digest,
            manifest.blob.sha256,
            handle.workspace_id,
            record.updated_seq,
            owner.agent_id,
            owner.session_id,
            handle.source_id,
            handle.base_revision,
            record.repository_fingerprint,
            files,
        )

    async def completion_receipt(self, *, session_id, turn_id, workspace):
        """Require every writable assignment's applied Effect, never model prose.

        Returns one receipt per accepted ``patch_author`` assignment, or ``None``
        while any of them still lacks a captured completed Patch or its applied
        receipt. Binding violations raise instead of degrading to "missing".
        """
        from traceh.supervision.investigation_work import PATCH_AUTHOR_ROLE
        from traceh.supervision.structured_collaboration import SUBMIT_COLLABORATION
        from traceh.supervision.writable_collaboration import COLLECT_CHILD_PATCH

        owner = await self.writable.authority.require_caller(session_id)
        events = await self.sessions.read_session(session_id)
        effects = await self.sessions.read_effects(session_id)
        intents = {
            e.data["effect_id"]: e
            for e in effects
            if e.type == "effect/intent" and e.data.get("turn_id") == turn_id
        }
        # The waiting dispatch returns each report itself; a concurrent dispatch
        # is collected per assignment, and repeated collection of one settled
        # message is allowed. Both carry the same original captured identity.
        handoffs = [
            e
            for e in effects
            if e.type == "effect/outcome"
            and e.data.get("effect_id") in intents
            and e.data.get("tool_name") in {SUBMIT_COLLABORATION, COLLECT_CHILD_PATCH}
            and e.data.get("status") == "succeeded"
        ]
        plans = [e for e in handoffs if e.data["tool_name"] == SUBMIT_COLLABORATION]
        if not handoffs or len(plans) != 1:
            raise AgentToolBindingError("patch-completion-handoff-missing")
        accepted: dict[tuple[str, str], str] = {}
        captured: dict[tuple[str, str], dict] = {}
        plan_payload = _payload(plans[0], events, effects, session_id)["data"]
        children = plan_payload.get("children") if type(plan_payload) is dict else None
        if type(children) is not list or not children:
            raise AgentToolBindingError("patch-completion-handoff-incomplete")
        for row in children:
            if type(row) is not dict or not {"agent_id", "message_id", "role"} <= set(row):
                raise AgentToolBindingError("patch-completion-handoff-incomplete")
            if row["role"] != PATCH_AUTHOR_ROLE:
                continue
            key = (row["agent_id"], row["message_id"])
            accepted[key] = row.get("assignment_id")
            if row.get("status") == "completed" and row.get("reason") == "completed":
                captured[key] = row.get("artifact")
        if not accepted:
            raise AgentToolBindingError("patch-completion-handoff-incomplete")
        for outcome in handoffs:
            if outcome.data["tool_name"] == SUBMIT_COLLABORATION:
                continue
            report = _payload(outcome, events, effects, session_id)["data"]
            if type(report) is not dict:
                raise AgentToolBindingError("patch-completion-handoff-incomplete")
            key = (report.get("agent_id"), report.get("message_id"))
            if report.get("status") != "completed" or report.get("reason") != "completed":
                continue
            if key not in accepted:
                # A completed capture that this plan never accepted cannot become
                # delivery evidence for one that it did.
                raise AgentToolBindingError("patch-completion-handoff-unbound")
            existing = captured.get(key)
            if existing is not None and existing != report.get("artifact"):
                raise AgentToolBindingError("patch-completion-handoff-conflict")
            captured[key] = report.get("artifact")
        if any(type(captured.get(key)) is not dict for key in accepted):
            return None
        applied = [
            e
            for e in effects
            if e.type == "effect/outcome"
            and e.data.get("effect_id") in intents
            and e.data.get("tool_name") == "integrate_child_patch"
            and e.data.get("status") == "succeeded"
        ]
        receipts = []
        touched: dict[str, str] = {}
        for key, assignment_id in sorted(accepted.items()):
            reference = captured[key]
            artifact = await self.writable.capture.reader.load(reference["artifact_id"])
            manifest = artifact.manifest
            child = await self.writable.authority.require_owned(manifest.agent_id, session_id)
            if (
                child.owner_agent_id != owner.agent_id
                or child.agent_id != key[0]
                or child.session_id != manifest.session_id
                or manifest.message_id != key[1]
                or manifest.blob.sha256 != reference["patch_digest"]
            ):
                raise AgentToolBindingError("patch-completion-handoff-binding-invalid")
            for path in manifest.changed_paths:
                if path in touched:
                    # Two assistants delivering the same file is a conflict, not
                    # a merge: the later one does not silently win.
                    raise AgentToolBindingError("patch-completion-paths-overlap")
                touched[path] = assignment_id
            _, plan = await self._target_plan(artifact, session_id, workspace)
            receipt = next(
                (
                    dict(
                        assignment_id=assignment_id,
                        effect_id=outcome.data["effect_id"],
                        tool_call_id=outcome.data["tool_call_id"],
                        artifact_id=plan.artifact_id,
                        request_digest=plan.digest,
                    )
                    for outcome in applied
                    for payload in (_payload(outcome, events, effects, session_id)["data"],)
                    if payload.get("outcome") == "applied"
                    and payload.get("request") == plan.data()
                    and payload.get("request_digest") == plan.digest
                ),
                None,
            )
            if receipt is None:
                return None
            receipts.append(receipt)
        return receipts

    async def read(self, arguments, context):
        artifact, plan = await self.plan(arguments["artifact_id"], context)
        source = artifact.content.decode("utf-8", errors="strict")
        offset = arguments.get("offset", 0)
        count = arguments.get("count", PATCH_PAGE_DEFAULT_CHARS)
        if type(offset) is not int or not 0 <= offset < len(source):
            raise ValueError("patch-read-offset-invalid")
        if type(count) is not int or not 1 <= count <= PATCH_PAGE_MAX_CHARS:
            raise ValueError("patch-read-count-invalid")
        end = min(len(source), offset + count)
        page = {
            "artifact_id": artifact.manifest.artifact_id,
            "patch_digest": plan.patch_digest,
            "request_digest": plan.digest,
            "read_tool_call_id": context.tool_call_id,
            "offset": offset,
            "end_offset": end,
            "total_chars": len(source),
            "next_offset": end if end < len(source) else None,
            "text": source[offset:end],
        }
        return ToolOutput(
            canonical_json(page),
            {"page": page, "request": plan.data()},
            evidence=(artifact.manifest.reference,),
        )

    async def integrate(self, arguments, context):
        artifact, plan = await self.plan(arguments["artifact_id"], context)
        if arguments["request_digest"] != plan.digest:
            raise AgentToolBindingError("patch-integration-request-drift")
        events = await self.sessions.read_session(context.session_id)
        effects = await self.sessions.read_effects(context.session_id)
        reads = [
            e
            for e in effects
            if e.type == "effect/outcome"
            and e.data.get("tool_name") == "read_child_patch"
            and e.data.get("status") == "succeeded"
        ]
        source = artifact.content.decode("utf-8", errors="strict")
        coverage = []
        selected = False
        for outcome in reads:
            payload = _payload(outcome, events, effects, context.session_id)
            data = payload.get("data", {})
            page = data.get("page", {})
            if data.get("request") != plan.data() or page.get("request_digest") != plan.digest:
                continue
            if not _received_page(outcome, payload, events):
                continue
            start, end = page["offset"], page["end_offset"]
            if page["text"] != source[start:end]:
                raise AgentToolBindingError("patch-read-content-mismatch")
            coverage.append((start, end))
            selected |= outcome.data["tool_call_id"] == arguments["read_tool_call_id"]
        through = 0
        for start, end in sorted(coverage):
            if start > through:
                break
            through = max(through, end)
        if not selected or through != len(source):
            raise AgentToolBindingError("patch-original-not-fully-read")
        intents = [
            e
            for e in effects
            if e.type == "effect/intent" and e.data.get("tool_call_id") == context.tool_call_id
        ]
        if len(intents) != 1:
            raise AgentToolBindingError("patch-integration-dispatch-not-unique")
        intent = intents[0]
        if (
            intent.data.get("tool_name") != "integrate_child_patch"
            or intent.data.get("arguments") != arguments
            or intent.data.get("turn_id") != context.turn_id
            or intent.data.get("step_id") != context.step_id
            or len(
                [
                    e
                    for e in effects
                    if e.type == "effect/dispatched"
                    and e.data.get("effect_id") == intent.data["effect_id"]
                ]
            )
            != 1
        ):
            raise AgentToolBindingError("patch-integration-dispatch-invalid")
        if any(
            e.type in {"effect/outcome", "effect/reconciled"}
            and e.data.get("effect_id") == intent.data["effect_id"]
            for e in effects
        ):
            raise AgentToolBindingError("patch-integration-already-settled-no-retry")
        operation_id = _operation_id("patch-integrate", plan.agent_id, context)
        try:
            receipt = await self.writable.workspaces.edit(
                plan,
                operation_id=operation_id,
                session_id=context.session_id,
                workspace=context.workspace,
            )
        except WorkspaceEditCancelled as cancellation:
            output = ToolOutput(canonical_json(cancellation.receipt), cancellation.receipt)
            raise ToolExecutionCancelled(output) from cancellation
        output = ToolOutput(canonical_json(receipt), receipt, (artifact.manifest.reference,))
        if receipt["outcome"] != "applied":
            raise ToolExecutionFailure(output)
        return output

    async def inspect(self, arguments, context):
        """Reconcile original intent/outcome with files; never re-publish."""
        from traceh.workspaces.editing import matches

        await self.writable.authority.require_caller(context.session_id)
        events = await self.sessions.read_session(context.session_id)
        effects = await self.sessions.read_effects(context.session_id)
        intents = [
            e
            for e in effects
            if e.type == "effect/intent"
            and e.data.get("tool_name") == "integrate_child_patch"
            and e.data.get("tool_call_id") == arguments["tool_call_id"]
        ]
        if len(intents) != 1:
            raise AgentToolBindingError("patch-inspection-operation-not-unique")
        intent = intents[0]
        _, plan = await self.plan(intent.data["arguments"]["artifact_id"], context)
        outcomes = [
            e
            for e in effects
            if e.type in {"effect/outcome", "effect/reconciled"}
            and e.data.get("effect_id") == intent.data["effect_id"]
        ]
        if len(outcomes) > 1:
            raise AgentToolBindingError("patch-inspection-outcome-not-unique")
        original = (
            _payload(outcomes[0], events, effects, context.session_id).get("data", {})
            if outcomes and outcomes[0].type == "effect/outcome"
            else {}
        )
        rows = []
        for edit in plan.files:
            try:
                state = (
                    "before"
                    if matches(context.workspace, edit, edit.before)
                    else "after"
                    if matches(context.workspace, edit, edit.after)
                    else "conflict"
                )
            except Exception:
                state = "unknown"
            rows.append({"path": edit.path, "current_image": state})
        identity = replace(
            context,
            turn_id=intent.data["turn_id"],
            step_id=intent.data["step_id"],
            tool_call_id=intent.data["tool_call_id"],
        )
        data = {
            "operation_id": _operation_id("patch-integrate", plan.agent_id, identity),
            "original_receipt": original,
            "outcome_status": outcomes[0].data["status"] if outcomes else "unknown_after_dispatch",
            "request_matches": plan.digest == intent.data["arguments"]["request_digest"],
            "files": rows,
            "action": "read_only_no_retry",
        }
        return ToolOutput(canonical_json(data), data)


@dataclass(frozen=True)
class PatchIntegrationTool:
    control: PatchIntegrationControl
    name: str

    @property
    def effect_kind(self):
        return (
            EffectKind.WORKSPACE_WRITE
            if self.name == "integrate_child_patch"
            else EffectKind.WORKSPACE_READ
        )

    @property
    def description(self):
        if self.name == "inspect_patch_integration":
            return (
                "Read one original integration Effect and compare its file images with the "
                "current workspace. Reports unknown/conflict without reapplying or overwriting."
            )
        if self.name == "read_child_patch":
            return (
                "Read an owned child's original immutable Patch page and freeze the target "
                "integration request. offset is a zero-based Unicode character offset; count "
                f"is characters, not lines or bytes (default {PATCH_PAGE_DEFAULT_CHARS}, "
                f"maximum {PATCH_PAGE_MAX_CHARS}). Start with artifact_id alone for a default "
                "page. Continue at next_offset until it is null; do not restart at zero. "
                "If a page is retained, use a currently listed output reader or request a "
                "smaller page at the same offset. If neither yields the original text, report "
                "the limitation. Receiving an id or preview is not reading the complete Patch."
            )
        return (
            "Explicitly integrate the fully read child Patch using the returned request_digest "
            "and read_tool_call_id. Exact preimages must match; conflicts preserve main edits. "
            "Inspect the per-file receipt; no automatic merge, retry, approval or promotion."
        )

    @property
    def input_schema(self):
        if self.name == "inspect_patch_integration":
            return {
                "type": "object",
                "properties": {"tool_call_id": {"type": "string"}},
                "required": ["tool_call_id"],
                "additionalProperties": False,
            }
        fields = {"artifact_id": {"type": "string"}}
        required = ["artifact_id"]
        if self.name == "read_child_patch":
            fields.update(
                {
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Zero-based Unicode character offset; continue at next_offset."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": PATCH_PAGE_MAX_CHARS,
                        "default": PATCH_PAGE_DEFAULT_CHARS,
                        "description": (
                            "Number of Unicode characters, not lines or bytes; "
                            "omit for the default page."
                        ),
                    },
                }
            )
        else:
            fields.update(
                {"request_digest": {"type": "string"}, "read_tool_call_id": {"type": "string"}}
            )
            required.extend(("request_digest", "read_tool_call_id"))
        return {
            "type": "object",
            "properties": fields,
            "required": required,
            "additionalProperties": False,
        }

    async def execute(self, arguments, context):
        if self.name == "inspect_patch_integration":
            return await self.control.inspect(arguments, context)
        method = self.control.read if self.name == "read_child_patch" else self.control.integrate
        return await method(arguments, context)
