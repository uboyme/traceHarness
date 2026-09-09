"""Project-qualified Memory references; canonical state remains in the original streams."""

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass

from traceh.api.json_types import canonical_json
from traceh.api.memory import MemoryPolicy, ProjectScopeLimits
from traceh.memory.projection import memory_stream, replay_memory
from traceh.projects.events import PROJECT_STREAM, exact, parse_reference, reference, referenced
from traceh.projects.projection import replay_projects
from traceh.session.context_index import ContextCorpus
from traceh.session.retrieval import block_identity, tokenize
from traceh.session.stream_heads import stream_head, validate_stream_head


def body_digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryContextSource:
    scope_json: str
    heads_json: str
    policy_json: str
    view: object

    @property
    def scope(self):
        return json.loads(self.scope_json)

    @property
    def heads(self):
        return json.loads(self.heads_json)

    @property
    def policy(self):
        return json.loads(self.policy_json)


class MemoryContextReader:
    """Borrow the existing authority for live reads; retain no selected state."""

    def __init__(self, authority):
        self._authority = authority

    async def observe_workspace(self, session_id):
        from traceh.workspaces.observation import WorkspaceObserver

        return await WorkspaceObserver(self._authority.scope).observe(session_id)

    async def read(self, session_id):
        authority = self._authority
        catalog = await authority.scope.catalog()
        if session_id not in catalog.sessions:
            return None
        binding = await authority.scope.resolve(session_id)
        if catalog.sessions[session_id] != binding:
            raise ValueError("memory-context-source-changed")
        view = await authority.read(session_id)
        source = MemoryContextSource(
            canonical_json(
                {"kind": "project", "session_id": session_id, "project_binding": reference(binding)}
            ),
            canonical_json(
                [
                    stream_head(PROJECT_STREAM, catalog.events),
                    stream_head(memory_stream(view.project_id), view.events),
                ]
            ),
            canonical_json(
                {
                    "project_limits": asdict(authority.scope.limits),
                    "memory_policy": asdict(authority.policy),
                }
            ),
            view,
        )
        if not await self.recheck(source):
            raise ValueError("memory-context-source-changed")
        return source

    async def recheck(self, source):
        binding = await self._authority.scope.resolve(source.scope["session_id"])
        if reference(binding) != source.scope["project_binding"]:
            return False
        for head in source.heads:
            events = await self._authority.store.read(head["stream_id"])
            if stream_head(head["stream_id"], events) != head:
                return False
        return True


def parse_source_policy(value):
    exact(value, {"project_limits", "memory_policy"})
    limits, memory = value["project_limits"], value["memory_policy"]
    exact(limits, set(ProjectScopeLimits.__dataclass_fields__))
    exact(memory, set(MemoryPolicy.__dataclass_fields__))
    if type(memory["denied_patterns"]) is not list:
        raise ValueError("memory-context-policy-invalid")
    return ProjectScopeLimits(**limits), MemoryPolicy(
        **{**memory, "denied_patterns": tuple(memory["denied_patterns"])}
    )


def validate_scope(scope, session_id):
    exact(scope, {"kind", "session_id", "project_binding"})
    ref = parse_reference(scope["project_binding"])
    if (
        scope["kind"] != "project"
        or scope["session_id"] != session_id
        or ref["stream_id"] != PROJECT_STREAM
        or ref["type"] != "project/session-bound"
    ):
        raise ValueError("memory-context-scope-invalid")


async def read_frozen_source(data, read_stream, session_events):
    """Replay exact historical prefixes; never consult today's Git, active set or index."""
    if data["memory_source"] is None:
        return None
    validate_scope(data["scope"], data["session_id"])
    limits, policy = parse_source_policy(data["memory_source"])
    prefixes = []
    for head in data["source_heads"]:
        validate_stream_head(head)
        events = (await read_stream(head["stream_id"]))[: head["head_seq"]]
        if stream_head(head["stream_id"], events) != head:
            raise ValueError("memory-context-head-mismatch")
        prefixes.append(events)
    if len(prefixes) != 2 or data["source_heads"][0]["stream_id"] != PROJECT_STREAM:
        raise ValueError("memory-context-head-mismatch")
    catalog = replay_projects(prefixes[0], limits)
    binding = referenced(catalog.events, data["scope"]["project_binding"])
    if catalog.sessions.get(data["session_id"]) != binding:
        raise ValueError("memory-context-binding-mismatch")
    if referenced(session_events, binding.data["session_ref"]) != session_events[0]:
        raise ValueError("memory-context-session-mismatch")
    project_id = binding.data["project_id"]
    observation = data["workspace_observation"]
    if (
        observation is not None
        and observation["source_identity"]
        != catalog.sources[project_id].data["repository_fingerprint"]
    ):
        raise ValueError("memory-context-observation-source-mismatch")
    if data["source_heads"][1]["stream_id"] != memory_stream(project_id):
        raise ValueError("memory-context-project-mismatch")
    view = replay_memory(project_id, prefixes[1], policy)
    return MemoryContextSource(
        canonical_json(data["scope"]),
        canonical_json(data["source_heads"]),
        canonical_json(data["memory_source"]),
        view,
    )


def make_block(fact, tier, source):
    if tier not in {"directory", "summary", "section"}:
        raise ValueError("memory-disclosure-not-authorized")
    approved_digest = body_digest(fact.body)
    project_id = source.view.project_id
    body = (
        fact.body
        if tier != "directory"
        else canonical_json(
            {
                "project_id": project_id,
                "memory_id": fact.memory_id,
                "fact_slot": fact.fact_slot,
                "approved_content_digest": approved_digest,
                "content_bytes": len(fact.body.encode("utf-8")),
            }
        )
    )
    return {
        "kind": "memory",
        "id": fact.memory_id,
        "version": fact.proposal.data["proposal_digest"],
        "tier": tier,
        "scope": source.scope,
        "source_refs": [reference(fact.proposal), reference(fact.activation)],
        "content_digest": body_digest(body),
        "content_bytes": len(body.encode("utf-8")),
        "body": body,
        "provenance": {
            "project_id": project_id,
            "memory_id": fact.memory_id,
            "fact_slot": fact.fact_slot,
            "activation_ref": reference(fact.activation),
            "approved_content_digest": approved_digest,
        },
    }


def prepare_corpus(source, policy):
    blocks, rows, values = [], [], []
    directory = [
        json.loads(make_block(fact, "directory", source)["body"])
        for fact in sorted(source.view.active, key=lambda fact: fact.memory_id)
    ]
    if len(canonical_json(directory).encode("utf-8")) > policy.max_catalog_bytes:
        raise ValueError("memory-catalog-resource-limit")
    for fact in sorted(source.view.active, key=lambda fact: fact.memory_id):
        block = make_block(fact, policy.default_tier, source)
        # Quotes/backticks delimit complete literals with spaces. Unquoted text contributes
        # contiguous literals only; do not infer a multiword path from surrounding prose.
        literals = [
            next(value for value in match if value)
            for match in re.findall(
                r"""`([^`\r\n]+)`|"([^"\r\n]+)"|'([^'\r\n]+)'|([\w.:/\\#-]+)""", fact.body
            )
        ]
        terms = tokenize(" ".join((fact.memory_id, fact.fact_slot, fact.body)))
        rows.append(
            {
                "identity": block_identity(block),
                "kind": "memory",
                "id": fact.memory_id,
                "version": block["version"],
                "tier": block["tier"],
                "scope": block["scope"],
                "source_refs": block["source_refs"],
                "content_digest": block["content_digest"],
                "tf": dict(sorted(Counter(terms).items())),
                "length": len(terms),
                "terms": " ".join("t" + term.encode("utf-8").hex() for term in terms),
            }
        )
        values.append(
            {
                "id": [fact.memory_id],
                "tag": [fact.fact_slot],
                "path": literals,
                "symbol": literals,
                "error": literals,
            }
        )
        blocks.append(block)
    if (
        len(rows) > policy.max_corpus_items
        or len(canonical_json(rows).encode("utf-8")) > policy.max_corpus_bytes
    ):
        raise ValueError("memory-corpus-resource-limit")
    return (
        ContextCorpus.build(
            scope=source.scope,
            catalog_digest=None,
            source_heads=source.heads,
            config_digest=policy.digest,
            items=rows,
        ),
        blocks,
        rows,
        values,
    )


def verify_blocks(data, source, policy):
    facts = {fact.memory_id: fact for fact in source.view.active}
    for block in data["blocks"]:
        if block["kind"] != "memory":
            continue
        fact = facts.get(block["id"])
        if fact is None or canonical_json(block) != canonical_json(
            make_block(fact, block["tier"], source)
        ):
            raise ValueError("memory-context-active-mismatch")
    receipt = data["retrieval"]["memory"] if data["retrieval"] else None
    if receipt is not None:
        from traceh.session.retrieval import validate_coverage

        corpus, blocks, rows, values = prepare_corpus(source, policy)
        manifest = json.loads(corpus.manifest_json)
        if (
            receipt["corpus_key"] != corpus.key
            or receipt["corpus_digest"] != manifest["corpus_digest"]
            or receipt["eligible_count"] != manifest["item_count"]
        ):
            raise ValueError("memory-context-corpus-mismatch")
        identities = {block_identity(block) for block in blocks}
        if any(
            item["identity"] not in identities
            for lane in receipt["lanes"]
            for item in lane["ranking"]
        ):
            raise ValueError("memory-context-ranking-mismatch")
        validate_coverage(receipt, rows, values, data["query"]["text"], policy)
