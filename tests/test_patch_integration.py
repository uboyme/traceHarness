"""WC-3 explicit Patch integration through the real Product and Tool owners."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from collaboration_fixtures import run_failed_product
from promotion_fixtures import build_source_repository, git, make_bare_target
from test_writable_collaboration import WritableProvider, writable_profile

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.cas import LocalArtifactCas
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


class IntegratingProvider(WritableProvider):
    def __init__(self, *, conflict=False, unread=False, repeat=False, paged=False):
        super().__init__()
        self.conflict = conflict
        self.unread = unread
        self.repeat = repeat
        self.paged = paged
        self.receipt = None

    def artifact_id(self, request):
        """The child Artifact identity as the waiting handoff returned it."""
        handoff = next(
            json.loads(m.content) for m in request.messages if m.name == "submit_collaboration_plan"
        )
        return handoff["children"][0]["artifact"]["artifact_id"]

    async def complete(self, request):
        names = {t.name for t in request.tools}
        if (
            "traceh.product.patch-author" in request.system_prompt
            or "integrate_child_patch" not in names
        ):
            return await super().complete(request)
        artifact_id = self.artifact_id(request)
        reads = [m for m in request.messages if m.name == "read_child_patch"]
        if not reads:
            tool = next(t for t in request.tools if t.name == "read_child_patch")
            assert "Unicode character" in tool.description
            assert "read_tool_output" not in tool.description
            assert tool.input_schema["properties"]["count"]["default"] == 2000
            return product._response(
                "",
                ToolCall(
                    "read-patch",
                    "read_child_patch",
                    {
                        "artifact_id": artifact_id,
                        **({"count": 1} if self.unread else {"count": 80} if self.paged else {}),
                    },
                ),
            )
        page = json.loads(reads[-1].content)
        if not self.unread and not self.paged:
            assert page["offset"] == 0 and page["next_offset"] is None
        if self.paged and page["next_offset"] is not None:
            return product._response(
                "",
                ToolCall(
                    f"read-patch-{len(reads)}",
                    "read_child_patch",
                    {
                        "artifact_id": artifact_id,
                        "offset": page["next_offset"],
                        "count": 80,
                    },
                ),
            )
        if self.conflict and not any(m.tool_call_id == "main-drift" for m in request.messages):
            return product._response(
                "",
                ToolCall(
                    "main-drift",
                    "apply_patch",
                    {
                        "path": "tracked.txt",
                        "old_text": "base",
                        "new_text": "main",
                    },
                ),
            )
        integrated = [m for m in request.messages if m.name == "integrate_child_patch"]
        if not integrated:
            return product._response(
                "",
                ToolCall(
                    "integrate",
                    "integrate_child_patch",
                    {
                        "artifact_id": artifact_id,
                        "request_digest": page["request_digest"],
                        "read_tool_call_id": "read-patch",
                    },
                ),
            )
        self.receipt = integrated[-1].content
        if self.conflict or self.unread:
            return product._response("Integration was refused; no completion claimed.")
        receipt = json.loads(self.receipt)
        assert receipt["outcome"] == ("conflict" if len(integrated) == 2 else "applied"), receipt
        if self.repeat and len(integrated) == 1:
            return product._response(
                "",
                ToolCall(
                    "integrate-again",
                    "integrate_child_patch",
                    {
                        "artifact_id": artifact_id,
                        "request_digest": page["request_digest"],
                        "read_tool_call_id": "read-patch",
                    },
                ),
            )
        if not any(m.name == "inspect_patch_integration" for m in request.messages):
            return product._response(
                "",
                ToolCall(
                    "inspect",
                    "inspect_patch_integration",
                    {
                        "tool_call_id": "integrate",
                    },
                ),
            )
        if not any(m.tool_call_id == "add-main" for m in request.messages):
            return product._response(
                "",
                ToolCall(
                    "add-main",
                    "apply_patch",
                    {
                        "path": "added.txt",
                        "old_text": "",
                        "new_text": "added\n",
                        "create": True,
                    },
                ),
            )
        return product._response("Read and integrated the child Patch, then added the main file.")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "conflict", "unread", "repeat", "paged"])
async def test_product_explicit_patch_integration(tmp_path, monkeypatch, failure):
    original = product._host_profile
    monkeypatch.setattr(
        product,
        "_host_profile",
        lambda mode: replace(original(mode), profile=writable_profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    # This fixture explicitly uses raw blob bytes. Production rejects byte
    # drift caused by checkout filters rather than guessing a conversion.
    git("config", "core.autocrlf", "false", cwd=source)
    (source / "tracked.txt").write_bytes(b"base\n")
    (source / "kept.txt").write_bytes(b"kept\n")
    git("add", "--renormalize", ".", cwd=source)
    assert not git("status", "--porcelain", cwd=source)
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    provider = IntegratingProvider(
        conflict=failure == "conflict",
        unread=failure == "unread",
        repeat=failure == "repeat",
        paged=failure == "paged",
    )
    cas = LocalArtifactCas(tmp_path / "cas")
    if failure in {"conflict", "unread"}:
        await run_failed_product(tmp_path, store, source, target, provider)
    else:
        await product._run_to_barrier(
            tmp_path, store, source, target, cas, RequestedTaskMode.MULTI, provider
        )
    sessions = SessionService(store)
    if failure is None:
        from traceh.agents import AgentDirectoryReader
        from traceh.evaluation.evaluators.product_handoffs import investigations

        directory = await AgentDirectoryReader(store).load()
        child = next(
            r
            for r in directory.records
            if r.owner_agent_id
            and "apply_patch" in r.capability_grants
            and any(
                parent.agent_id == r.owner_agent_id and "apply_patch" in parent.capability_grants
                for parent in directory.records
            )
        )
        from traceh.agents import AgentInboxReader
        from traceh.supervision.writable_work import read_writable_work

        message = tuple(await AgentInboxReader(store).load(child.agent_id))[0]
        revision = read_writable_work(message.message.content)["revision"]
        handoffs = await investigations(store, child.owner_agent_id, revision)
        assert len(handoffs) == 1 and handoffs[0]["role"] == "patch_author"
        assert handoffs[0]["parent_report_dispatches"]
    effects = [
        e
        for stream in await store.list_streams(prefix="effects:")
        for e in await store.read(stream)
    ]
    outcomes = [
        e
        for e in effects
        if e.type == "effect/outcome" and e.data.get("tool_name") == "integrate_child_patch"
    ]
    assert len(outcomes) == (2 if failure == "repeat" else 1)
    outcome = outcomes[0]
    if failure == "unread":
        assert outcome.data["status"] == "failed"
        assert "patch-original-not-fully-read" in outcome.data["message"]
    else:
        receipt = outcome.data.get("retained_output", outcome.data)["data"]
        assert receipt["outcome"] == ("conflict" if failure == "conflict" else "applied")
        assert receipt["files"][0]["status"] == ("conflict" if failure == "conflict" else "applied")
        assert receipt["request"]["files"][0]["before"]["exists"]
    if failure in {None, "repeat", "paged"}:
        inspected = next(
            e
            for e in effects
            if e.type == "effect/outcome" and e.data.get("tool_name") == "inspect_patch_integration"
        )
        data = inspected.data.get("retained_output", inspected.data)["data"]
        assert data["action"] == "read_only_no_retry"
        assert data["files"][0]["current_image"] == "after"
        assert data["original_receipt"]["outcome"] == "applied"
    if failure == "repeat":
        second = outcomes[1].data.get("retained_output", outcomes[1].data)["data"]
        assert second["outcome"] == "conflict"
    for stream in await store.list_streams(prefix="session:"):
        assert not await product.verify_request_snapshots(
            sessions, product.SurfaceProjector(), stream.removeprefix("session:")
        )
    assert git("show", "HEAD:tracked.txt", cwd=source) == "base"


@pytest.mark.asyncio
async def test_product_cancel_during_integration_retains_rollback(tmp_path, monkeypatch):
    import traceh.workspaces.service as service_module
    from traceh.workspaces import editing

    original_profile = product._host_profile
    monkeypatch.setattr(
        product,
        "_host_profile",
        lambda mode: replace(original_profile(mode), profile=writable_profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    git("config", "core.autocrlf", "false", cwd=source)
    (source / "tracked.txt").write_bytes(b"base\n")
    (source / "kept.txt").write_bytes(b"kept\n")
    git("add", "--renormalize", ".", cwd=source)
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    entered, converging = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_publish = editing.publish
    original_converge = service_module.await_worker_convergence
    publications = []

    def publish(root, edit, expected, replacement):
        original_publish(root, edit, expected, replacement)
        publications.append((root / edit.path).read_bytes())
        if replacement == edit.after:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(30)

    async def converge(task):
        converging.set()
        await original_converge(task)

    monkeypatch.setattr(editing, "publish", publish)
    monkeypatch.setattr(service_module, "await_worker_convergence", converge)
    actions = product.ProductTurnActions()
    host = await product._build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.MULTI,
        IntegratingProvider(),
    )
    runtime = product._chat_runtime(tmp_path, store, actions)
    console = product._Console(("please add the accepted file", "yes, do it", "START"))
    chat_workspace = tmp_path / "chat-workspace"
    chat_workspace.mkdir()
    task = asyncio.create_task(
        product.run_chat(
            runtime, console.console, workspace=chat_workspace, timeline=False, product=host
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        await asyncio.wait_for(converging.wait(), 10)
        task.cancel()
        assert not task.done()
    finally:
        release.set()
        assert await asyncio.wait_for(asyncio.shield(task), 30) == 130
    assert publications[0].startswith(b"child") and publications[-1] == b"base\n"
    effects = [
        e
        for stream in await store.list_streams(prefix="effects:")
        for e in await store.read(stream)
    ]
    outcome = next(
        e
        for e in effects
        if e.type == "effect/outcome" and e.data.get("tool_name") == "integrate_child_patch"
    )
    assert outcome.data["status"] == "cancelled"
    receipt = outcome.data.get("retained_output", outcome.data)["data"]
    assert receipt["outcome"] == "cancelled"
    assert receipt["files"][0]["status"] == "rolled_back"
