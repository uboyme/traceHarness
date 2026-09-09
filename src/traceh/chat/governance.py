"""Shared host governance for Line and Textual, with fresh domain reads.

No widget state is authority. A review captures exact CAS inputs; cancellation
before confirmation writes nothing, and accepted actions use existing owners.
"""

from __future__ import annotations

import json
import shlex
from uuid import uuid4

from traceh.api.history import HistoryCursor
from traceh.api.json_types import canonical_json
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.manager import PluginManager
from traceh.projects.events import reference as event_ref
from traceh.runtime.prompt import PromptAssembler
from traceh.session.context_input import parse_context_input
from traceh.session.history import read_history
from traceh.session.history_observation import observe_history
from traceh.tools.registry import ToolRegistry

HELP = (
    "/context [STEP_ID] · frozen query, hits, exclusions, budgets and blocks",
    "/skills · enabled catalog, selection, eligibility and last retrieval",
    "/skills select ACTOR SKILL_ID... | /skills clear ACTOR",
    "/skills rebuild · rebuild this Session's derived index",
    "/plugins · installed and enabled; /plugins use ID... | --none",
    "/plugins reload · review and rebuild the enabled composition",
    "/memory · proposals and active facts with source/scope/digest",
    "/memory approve PROPOSAL_ID SLOT NEW_MEMORY_ID ACTOR",
    "/memory supersede PROPOSAL_ID SLOT NEW_MEMORY_ID ACTOR",
    "/memory revoke MEMORY_ID ACTOR | /memory rebuild",
    '/memory declare ACTOR "BODY" · propose a host statement; does not approve',
    '/project create PROJECT_ID ACTOR "LABEL"',
    "/project source PROJECT_ID SOURCE_ID ACTOR | /project bind PROJECT_ID ACTOR",
    "/history · current Session's compressed history directory",
    "/history page BLOCK_ID PAGE_INDEX · bounded original evidence for the human",
)
ROOTS = frozenset({"/context", "/skills", "/memory", "/history", "/plugins", "/project"})


def handles(text: str) -> bool:
    return text.split(maxsplit=1)[0] in ROOTS if text.strip() else False


def display(value) -> str:
    """JSON escaping preserves complete evidence while making controls inert."""
    from traceh.cli.command_line import escape_for_display

    return "\n".join(
        escape_for_display(line, limit=max(1, len(line) * 8))
        for line in json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    )


class ChatGovernance:
    def __init__(self, runtime, session_id, *, discovery=None):
        self.runtime = runtime
        self.session_id = session_id
        self.discovery = discovery or PluginDiscovery()

    async def context(self, step_id=None):
        events = await self.runtime.sessions.read_session(self.session_id)
        matches = [
            e
            for e in events
            if e.type == "context/input" and (step_id is None or e.data["step_id"] == step_id)
        ]
        if not matches:
            return {"status": "unavailable", "reason": "context-not-frozen"}
        event = matches[-1]
        snapshot = parse_context_input(event.data).to_dict()
        dispatched = any(
            e.type == "request/snapshot" and e.data["step_id"] == snapshot["step_id"]
            for e in events
        )
        return {"event": event_ref(event), "dispatched": dispatched, "snapshot": snapshot}

    async def skills(self):
        view = await self.runtime.skill_context.read(self.session_id)
        context = await self.context()
        view["last_retrieved"] = [
            {
                key: block[key]
                for key in (
                    "id",
                    "version",
                    "tier",
                    "content_bytes",
                    "content_digest",
                    "provenance",
                )
            }
            for block in context.get("snapshot", {}).get("blocks", [])
            if block["kind"] == "skill"
        ]
        view["retrieved_step"] = context.get("snapshot", {}).get("step_id")
        return view

    async def memory(self):
        view = await self.runtime.memory.read(self.session_id)
        return {
            "project_id": view.project_id,
            "head": view.head,
            "proposals": [{"ref": event_ref(e), **e.data} for e in view.proposals],
            "active": [
                {
                    "memory_id": f.memory_id,
                    "fact_slot": f.fact_slot,
                    "body": f.body,
                    "proposal_ref": event_ref(f.proposal),
                    "activation_ref": event_ref(f.activation),
                    "proposal_digest": f.proposal.data["proposal_digest"],
                }
                for f in view.active
            ],
        }

    async def history(self, block_id=None, index=None):
        policy = self.runtime.config.context_input
        if policy is None or policy.history is None:
            raise ValueError("history-reader-disabled")
        events = await self.runtime.sessions.read_session(self.session_id)
        history = read_history(
            events, session_id=self.session_id, through_seq=events[-1].seq, policy=policy.history
        )
        current = None
        if policy.workspace_observations:
            current = await self.runtime.memory.observe_workspace(self.session_id)
        blocks = history.directory() if block_id is None else (history.resolve(block_id),)
        result = []
        for block in blocks:
            page = (
                None
                if index is None
                else history.read_page(
                    block_id=block.block_id,
                    cursor=HistoryCursor(block.block_id, policy.history.digest, index),
                )
            )
            observation, freshness = observe_history(
                {"id": block.block_id, "provenance": {"page": None if page is None else page.page}},
                history,
                events,
                current,
            )
            result.append(
                {
                    "block_id": block.block_id,
                    "source_digest": block.version,
                    "observed_through_seq": block.observed_through_seq,
                    "observed_at": block.observed_at,
                    "freshness": freshness,
                    "observation": observation,
                    "original_bytes": history.original_bytes(block.block_id),
                    "summary": block.summary,
                    "cursor": block.first_cursor.to_dict(),
                    "page": None if page is None else {"body": page.body, **page.page},
                }
            )
        receipts = [event.data for event in events if event.type == "history/requested"]
        receipts.extend(
            event.data["data"]["history_receipt"]
            for event in events
            if event.type == "tool/result"
            and isinstance(event.data.get("data"), dict)
            and "history_receipt" in event.data["data"]
        )
        return {
            "session_id": self.session_id,
            "history": result,
            "disclosure_receipts": receipts,
            "notice": "Historical evidence; viewing does not inject it into a model request.",
        }

    async def run(self, text, *, confirm):
        """Return an inert display result; the adapter owns explicit confirmation UI."""
        parts = shlex.split(text)
        root, *args = parts
        if root == "/context" and len(args) <= 1:
            return await self.context(args[0] if args else None)
        if root == "/skills":
            if not args:
                return await self.skills()
            if args == ["rebuild"]:
                receipt = await self.runtime.skill_context.rebuild_index(self.session_id)
                return {"status": "rebuilt", "index": receipt}
            if (len(args) >= 3 and args[0] == "select") or (len(args) == 2 and args[0] == "clear"):
                view = await self.runtime.skill_context.read(self.session_id)
                ids = args[2:] if args[0] == "select" else []
                catalog = {item["skill_id"]: item for item in view["catalog"]}
                if len(set(ids)) != len(ids) or any(i not in catalog for i in ids):
                    raise ValueError("skill-selection-identity-invalid")
                selected = [{"skill_id": i, "version": catalog[i]["version"]} for i in sorted(ids)]
                if not await confirm(
                    {
                        "action": "select Skills",
                        "actor": args[1],
                        "skills": selected,
                        "descriptors": [catalog[i] for i in sorted(ids)],
                        "catalog_digest": view["catalog_digest"],
                    }
                ):
                    return {"status": "cancelled"}
                event = await self.runtime.skill_context.select(
                    self.session_id,
                    operation_id=str(uuid4()),
                    actor_id=args[1],
                    expected_head=view["selection_head"],
                    expected_catalog_digest=view["catalog_digest"],
                    skills=selected,
                )
                return {"status": "selected", "receipt": event_ref(event)}
        if root == "/plugins":
            if not args:
                return {
                    "installed": [r.to_dict() for r in self.discovery.discover()],
                    "enabled": [p.to_dict() for p in self.runtime.external_plugin_identities],
                }
            if args == ["reload"] or (len(args) >= 2 and args[0] == "use"):
                ids = (
                    tuple(p.plugin_id for p in self.runtime.external_plugin_identities)
                    if args == ["reload"]
                    else (() if args[1:] == ["--none"] else tuple(args[1:]))
                )
                manager = PluginManager(
                    tools=ToolRegistry(), prompt=PromptAssembler(), discovery=self.discovery
                )
                statuses, reviewed = manager.review_enable(ids)
                if not await confirm(
                    {
                        "action": "enable trusted plugins",
                        "plugins": statuses,
                        "notice": (
                            "Import has occurred; setup has not. Provides are declarations; "
                            "trusted code has in-process authority and may register Tools, "
                            "Providers, Policies, Middleware or Services during setup. "
                            "Actual active capabilities are shown after activation."
                        ),
                    }
                ):
                    return {"status": "cancelled"}
                await self.runtime.migrate_session_plugin_composition(
                    self.session_id, ids, plugin_discovery=reviewed
                )
                return {"status": "enabled", "skills": await self.skills()}
        if root == "/memory":
            if not args:
                return await self.memory()
            if args == ["rebuild"]:
                receipt = await self.runtime.memory.rebuild_index(self.session_id)
                return {"status": "rebuilt", "index": receipt}
            return await self._memory_action(args, confirm)
        if root == "/history":
            if not args:
                return await self.history()
            if len(args) == 3 and args[0] == "page":
                return await self.history(args[1], int(args[2]))
        if root == "/project":
            return await self._project_action(args, confirm)
        raise ValueError("governance-command-invalid")

    async def _memory_action(self, args, confirm):
        if not args:
            raise ValueError("governance-command-invalid")
        kind = args[0]
        view = await self.runtime.memory.read(self.session_id)
        fields = {}
        detail = {}
        if kind in {"approve", "supersede"} and len(args) == 5:
            _, proposal_id, slot, memory_id, actor = args
            proposal = next(
                (e for e in view.proposals if e.data["proposal_id"] == proposal_id), None
            )
            if proposal is None:
                raise ValueError("memory-proposal-unavailable")
            fields.update(
                proposal_ref=event_ref(proposal),
                proposal_digest=proposal.data["proposal_digest"],
                memory_id=memory_id,
                fact_slot=slot,
            )
            detail = {"proposal": proposal.data}
            previous = next((f for f in view.active if f.fact_slot == slot), None)
            if kind == "approve" and previous is not None:
                raise ValueError("memory-slot-occupied")
            if kind == "supersede":
                if previous is None:
                    raise ValueError("memory-predecessor-unavailable")
                fields.update(
                    predecessor_ref=event_ref(previous.activation),
                    predecessor_digest=previous.proposal.data["proposal_digest"],
                )
                detail["previous_body"] = previous.body
        elif kind == "revoke" and len(args) == 3:
            _, memory_id, actor = args
            previous = next((f for f in view.active if f.memory_id == memory_id), None)
            if previous is None:
                raise ValueError("memory-predecessor-unavailable")
            fields.update(
                memory_id=memory_id,
                fact_slot=previous.fact_slot,
                predecessor_ref=event_ref(previous.activation),
                predecessor_digest=previous.proposal.data["proposal_digest"],
            )
            detail = {"body": previous.body}
        elif kind == "declare" and len(args) == 3:
            _, actor, body = args
            fields.update(
                proposal_id=str(uuid4()), declaration_id=str(uuid4()), body=body, statement=body
            )
        else:
            raise ValueError("governance-command-invalid")
        arguments = {
            "operation_id": str(uuid4()),
            "actor_id": actor,
            "expected_head": view.head,
            **fields,
        }
        # Freeze the whole review before the UI await; subsequent writes use the same CAS head.
        arguments_json = canonical_json(arguments)
        if not await confirm(
            {"action": kind, "project_id": view.project_id, **arguments, **detail}
        ):
            return {"status": "cancelled"}
        event = await getattr(self.runtime.memory, kind)(
            self.session_id, **json.loads(arguments_json)
        )
        return {"status": kind, "receipt": event_ref(event)}

    async def _project_action(self, args, confirm):
        scope = self.runtime.project_scope
        if scope is None:
            raise ValueError("project-memory-disabled")
        if not args:
            catalog = await scope.catalog()
            binding = (
                await scope.resolve(self.session_id)
                if self.session_id in catalog.sessions
                else None
            )
            return {
                "head": catalog.head,
                "binding": None if binding is None else binding.data,
                "projects": [e.data for e in catalog.projects.values()],
                "sources": [e.data for e in catalog.sources.values()],
            }
        kind = args[0]
        if kind == "create" and len(args) == 4:
            fields = {"project_id": args[1], "actor_id": args[2], "label": args[3]}
            method = scope.create
        elif kind == "source" and len(args) == 4:
            fields = {"project_id": args[1], "source_id": args[2], "actor_id": args[3]}
            method = scope.bind_source
        elif kind == "bind" and len(args) == 3:
            fields = {"project_id": args[1], "session_id": self.session_id, "actor_id": args[2]}
            method = scope.bind_session
        else:
            raise ValueError("governance-command-invalid")
        fields.update(operation_id=str(uuid4()), expected_head=(await scope.catalog()).head)
        frozen = canonical_json(fields)
        if not await confirm({"action": f"project {kind}", **fields}):
            return {"status": "cancelled"}
        event = await method(**json.loads(frozen))
        return {"status": kind, "receipt": event_ref(event)}
