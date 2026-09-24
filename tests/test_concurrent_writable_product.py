"""One main and one concurrent patch author through the real Product host.

Requires the explicit real Docker verification image, like every other writable
Product end-to-end test in this repository.
"""

import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from promotion_fixtures import build_source_repository, git, make_bare_target
from test_patch_integration import IntegratingProvider
from test_writable_collaboration import PLAN, writable_profile

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision.structured_collaboration import DISPATCH_AND_CONTINUE

COLLECT = "collect_child_patch"


class ConcurrentIntegratingProvider(IntegratingProvider):
    """Dispatch without waiting, do the retained work, then collect and integrate."""

    def __init__(self):
        super().__init__()
        self.collected = []
        self.dispatch = None

    def artifact_id(self, request):
        return self.collected[-1]["artifact"]["artifact_id"]

    async def complete(self, request):
        names = {t.name for t in request.tools}
        if "traceh.product.patch-author" in request.system_prompt:
            return await super().complete(request)
        if "submit_collaboration_plan" in names:
            return product._response(
                "",
                ToolCall(
                    "plan",
                    "submit_collaboration_plan",
                    {**PLAN, "handoff": DISPATCH_AND_CONTINUE},
                ),
            )
        if "apply_patch" not in names:
            return product._response("Ready to allocate the source change.")
        assert COLLECT in names, sorted(names)
        self.dispatch = json.loads(
            next(m.content for m in request.messages if m.name == "submit_collaboration_plan")
        )
        assert self.dispatch["outcome"] == "dispatched"
        assert len(self.dispatch["children"]) == 1
        child = self.dispatch["children"][0]
        assert child["assignment_id"] == PLAN["children"][0]["assignment_id"]
        handle = {key: child[key] for key in ("agent_id", "message_id")}
        calls = {m.tool_call_id for m in request.messages}
        if "add-main" not in calls:
            # Retained work first: the child is still running in its own workspace.
            return product._response(
                "",
                ToolCall(
                    "add-main",
                    "apply_patch",
                    {"path": "added.txt", "old_text": "", "new_text": "added\n", "create": True},
                ),
            )
        self.collected = [json.loads(m.content) for m in request.messages if m.name == COLLECT]
        if not self.collected or self.collected[-1]["status"] != "completed":
            return product._response(
                "",
                ToolCall(
                    f"collect-{len(self.collected)}", COLLECT, {**handle, "wait_seconds": 30}
                ),
            )
        return await super().complete(request)


@pytest.mark.asyncio
async def test_concurrent_writable_product_collects_then_integrates(tmp_path, monkeypatch):
    original = product._host_profile
    monkeypatch.setattr(
        product,
        "_host_profile",
        lambda mode: replace(original(mode), profile=writable_profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    git("config", "core.autocrlf", "false", cwd=source)
    (source / "tracked.txt").write_bytes(b"base\n")
    (source / "kept.txt").write_bytes(b"kept\n")
    git("add", "--renormalize", ".", cwd=source)
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    provider = ConcurrentIntegratingProvider()
    await product._run_to_barrier(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        RequestedTaskMode.MULTI,
        provider,
    )
    assert provider.dispatch["children"][0]["status"] == "accepted"
    artifact = provider.collected[-1]["artifact"]
    assert artifact["changed_paths"] == ["tracked.txt"]
    manifests = tuple(await PatchArtifactCatalogReader(store).load())
    child_manifests = [
        manifest for manifest in manifests
        if manifest.agent_id == provider.dispatch["children"][0]["agent_id"]
    ]
    assert len(child_manifests) == 1
    assert child_manifests[0].artifact_id == artifact["artifact_id"]
    # Product completion also captures the main agent's assembled delivery.
    assert len(manifests) == 2
    sessions = SessionService(store)
    events = {}
    for stream in await store.list_streams(prefix="session:"):
        session_id = stream.removeprefix("session:")
        events[session_id] = await sessions.read_session(session_id)
        assert not await product.verify_request_snapshots(
            sessions, product.SurfaceProjector(), session_id
        )
    main = next(
        rows
        for rows in events.values()
        if any(e.type == "tool/result" and e.data.get("tool_call_id") == "add-main" for e in rows)
    )
    child = next(
        rows
        for rows in events.values()
        if any(
            e.type == "tool/result" and e.data.get("tool_call_id") == "child-write" for e in rows
        )
    )
    own_work = next(
        e for e in main if e.type == "tool/result" and e.data.get("tool_call_id") == "add-main"
    )
    started = next(e for e in child if e.type == "turn/start")
    ended = next(e for e in child if e.type == "turn/end")
    # The main really worked inside the child's open Turn interval.
    assert started.occurred_at <= own_work.occurred_at <= ended.occurred_at
    verifications = [e for rows in events.values() for e in rows if e.type == "verification/result"]
    assert verifications and all(e.data["passed"] for e in verifications)
    summary = json.loads(verifications[-1].data["summary"])
    assert len(summary["integration"]) == 1
    assert summary["integration"][0]["artifact_id"] == artifact["artifact_id"]
    assert git("show", "HEAD:tracked.txt", cwd=source) == "base"
