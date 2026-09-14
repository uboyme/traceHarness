"""WC-2 through real Product, Supervisor, independent Git trees and Capture."""

import asyncio
import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from collaboration_fixtures import MAIN_WORK, run_failed_product
from promotion_fixtures import build_source_repository, make_bare_target
from test_product_adaptive import profile

from traceh.agents import AgentDirectoryReader
from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.capture import PatchCaptureService
from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.artifacts.reader import PatchArtifactReader
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector
from traceh.supervision.authority import AgentToolBindingError
from traceh.supervision.writable_collaboration import WritableControl

PLAN = {
    "main_work": MAIN_WORK,
    "children": [{
        "assignment_id": "explicit-test-rules",
        "role": "patch_author",
        "goal": "Update the initial tracked text.",
        "scope": "Only tracked.txt.",
        "exclusions": "Do not create the main added.txt deliverable or run commands.",
        "deliverable": "A Patch changing base to child in tracked.txt.",
        "briefing": "The main agent separately creates added.txt and verifies final work.",
        "paths": ["tracked.txt"],
    }],
}


def writable_profile(mode):
    base = profile(mode)
    return replace(
        base,
        patch_author=replace(
            base.investigator,
            preset="explicit-test-patch-author",
            capability_grants=("list_files", "read_file", "search_text", "apply_patch"),
            budget=replace(base.investigator.budget, max_steps=6, max_tool_calls=6),
        ),
    )


class WritableProvider:
    name = "product-provider"

    def __init__(self, failure=None):
        self.failure = failure
        self.child_requests = []
        self.handoff = None

    async def complete(self, request):
        names = {t.name for t in request.tools}
        if "traceh.product.patch-author" in request.system_prompt:
            self.child_requests.append(request)
            assert names == {"list_files", "read_file", "search_text", "apply_patch"}
            if self.failure == "provider":
                raise RuntimeError("bounded test child failure")
            work = next(
                json.loads(m.content)
                for m in request.messages
                if m.role == "user" and m.content.startswith('{"assembly_digest":')
            )
            assert work["kind"] == "writable-assignment" and work["format"] == 1
            assert work["main_work"] == PLAN["main_work"]
            assert work["assignment_id"] == PLAN["children"][0]["assignment_id"]
            assert work["role"] == "patch_author"
            if not any(m.role == "tool" for m in request.messages):
                return product._response(
                    "", ToolCall("child-read", "read_file", {"path": "tracked.txt"})
                )
            if not any(m.name == "apply_patch" for m in request.messages):
                arguments = (
                    {"path": "outside.txt", "old_text": "", "new_text": "outside\n", "create": True}
                    if self.failure == "scope"
                    else {"path": "tracked.txt", "old_text": "base", "new_text": "child"}
                )
                return product._response("", ToolCall("child-write", "apply_patch", arguments))
            return product._response("Changed the assigned file; no functional tests were run.")
        if "submit_collaboration_plan" in names:
            return product._response("", ToolCall("plan", "submit_collaboration_plan", PLAN))
        if "apply_patch" not in names:
            return product._response("Ready to allocate the source change.")
        self.handoff = next(
            json.loads(m.content) for m in request.messages if m.name == "submit_collaboration_plan"
        )
        assert self.handoff["outcome"] == "completed"
        assert [row["artifact"]["changed_paths"] for row in self.handoff["children"]] == [
            ["tracked.txt"]
        ]
        if not any(m.name == "read_file" for m in request.messages):
            return product._response(
                "", ToolCall("main-read", "read_file", {"path": "tracked.txt"})
            )
        read = next(json.loads(m.content) for m in request.messages if m.name == "read_file")
        assert "base" in read["text"] and "child" not in read["text"]
        if not any(m.name == "apply_patch" for m in request.messages):
            return product._response(
                "",
                ToolCall(
                    "main-write",
                    "apply_patch",
                    {"path": "added.txt", "old_text": "", "new_text": "added\n", "create": True},
                ),
            )
        return product._response(
            "Main file created. Child Patch exists but is not integrated in WC-2."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "provider", "scope"])
async def test_writable_product_isolated_capture_and_failures(tmp_path, monkeypatch, failure):
    original = product._host_profile
    monkeypatch.setattr(
        product,
        "_host_profile",
        lambda mode: replace(original(mode), profile=writable_profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    provider = WritableProvider(failure)
    captures = []
    handoffs = []
    original_capture = PatchCaptureService.capture
    original_handoff = WritableControl.capture_and_collect

    async def repeated(control, arguments, context):
        result = await original_handoff(control, arguments, context)
        handoffs.append((control, arguments, context, result.content))
        before = len(captures)
        again = await original_handoff(control, arguments, context)
        assert again.content == result.content
        assert len(captures) == before
        with pytest.raises(AgentToolBindingError):
            await control.collect(arguments, replace(context, session_id="wrong-owner-session"))
        return result

    async def capture(service, agent_id, message_id):
        captures.append(agent_id)
        return await original_capture(service, agent_id, message_id)

    monkeypatch.setattr(PatchCaptureService, "capture", capture)
    monkeypatch.setattr(WritableControl, "capture_and_collect", repeated)
    if failure:
        await run_failed_product(tmp_path, store, source, target, provider)
        assert provider.handoff is None
        assert len(captures) == (1 if failure == "scope" else 0)
    else:
        _, events = await run_failed_product(tmp_path, store, source, target, provider)
        catalog = await PatchArtifactCatalogReader(store).load()
        assert len(tuple(catalog)) == 1
        child = provider.handoff["children"][0]
        artifact = await PatchArtifactReader(store, cas).load(child["artifact"]["artifact_id"])
        assert b"+child" in artifact.content
        assert len(captures) == 1
        records = (await AgentDirectoryReader(store).load()).records
        child_record = next(r for r in records if r.agent_id == child["agent_id"])
        main = next(r for r in records if r.agent_id == child_record.owner_agent_id)
        assert child_record.workspace_id != main.workspace_id
        checks = [e for e in events if e.type == "verification/result"]
        assert len(checks) == 2 and all(not e.data["passed"] for e in checks)
        assert all("integration is missing" in e.data["summary"] for e in checks)
        assert all(
            json.loads(e.data["summary"])["results"][0]["status"] == "passed"
            for e in checks
        )  # Missing integration and functional results are reported together.
        assert not await store.read("patch-promotions:ledger")
        # A completed child is still a valid, repeatable handoff. The main may
        # not capture/deliver until its required explicit integration succeeds.
        control, arguments, context, content = handoffs[0]
        assert (await control.collect(arguments, context)).content == content
        assert len(captures) == 1
    assert (source / "tracked.txt").read_text() == "base\n"
    for stream in await store.list_streams(prefix="session:"):
        sid = stream.removeprefix("session:")
        assert not await verify_request_snapshots(SessionService(store), SurfaceProjector(), sid)


@pytest.mark.parametrize(
    "changed",
    ["format", "revision", "owner_agent_id", "session_id", "budget_digest", "assembly_digest"],
)
def test_writable_work_rejects_wrong_version_and_binding(changed):
    from traceh.api.agents import AgentSpec
    from traceh.api.json_types import canonical_json, fingerprint
    from traceh.supervision.writable_work import (
        WritableBinding,
        validate_writable_work,
        work_content,
    )

    binding = WritableBinding(
        AgentSpec("test-preset", "test-workspace"),
        "test-task",
        "test-source",
        "a" * 40,
        "b" * 64,
        "c" * 64,
    )
    identities = dict(
        owner_id="parent",
        child_id="child",
        session_id="session",
        message_id="message",
        assignment_id="explicit-test-rules",
    )
    arguments = {
        **{k: v for k, v in PLAN["children"][0].items() if k not in ("assignment_id", "role")},
        "main_work": PLAN["main_work"],
    }
    content = work_content(arguments, binding, **identities)
    assert validate_writable_work(content, binding, **identities)["paths"] == ["tracked.txt"]
    wrong = json.loads(content)
    wrong[changed] = 0 if changed == "format" else "foreign"
    wrong["input_digest"] = fingerprint({k: v for k, v in wrong.items() if k != "input_digest"})
    with pytest.raises(AgentToolBindingError):
        validate_writable_work(canonical_json(wrong), binding, **identities)


@pytest.mark.asyncio
async def test_patch_author_profile_denies_expanded_permissions():
    from traceh.api.product import ProductRole
    from traceh.product.errors import ProductProfileError
    from traceh.product.registry import ProductProfileBinding, ProductProfileRegistry
    from traceh.product.runtime import BuiltinProductAssemblyResolver

    base = writable_profile(RequestedTaskMode.MULTI)
    plan = product._host_profile(RequestedTaskMode.MULTI).verification_plan
    for forbidden in ("shell", "delegate_investigation", "approve", "network"):
        denied = replace(
            base,
            patch_author=replace(
                base.patch_author,
                capability_grants=(*base.patch_author.capability_grants, forbidden),
            ),
        )
        with pytest.raises(ProductProfileError):
            ProductProfileRegistry(
                (("explicit", ProductProfileBinding(denied, plan)),),
                assemblies=BuiltinProductAssemblyResolver(),
            )
    resolved = await ProductProfileRegistry(
        (("explicit", ProductProfileBinding(base, plan)),),
        assemblies=BuiltinProductAssemblyResolver(),
    ).resolve("explicit")
    assert "apply_patch" in resolved.assembly(ProductRole.PATCH_AUTHOR).tool_ids
    assert "apply_patch" not in resolved.assembly(ProductRole.INVESTIGATOR).tool_ids


@pytest.mark.asyncio
async def test_writable_repeated_cancel_waits_for_child_convergence(tmp_path, monkeypatch):
    original = product._host_profile
    monkeypatch.setattr(
        product,
        "_host_profile",
        lambda mode: replace(original(mode), profile=writable_profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    entered, cancelled, release, finished = (asyncio.Event() for _ in range(4))

    class GatedProvider(WritableProvider):
        async def complete(self, request):
            if "traceh.product.patch-author" not in request.system_prompt:
                return await super().complete(request)
            entered.set()
            try:
                await release.wait()
                raise RuntimeError("child was released without cancellation")
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                finished.set()
                raise

    actions = product.ProductTurnActions()
    host = await product._build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.MULTI,
        GatedProvider(),
    )
    runtime = product._chat_runtime(tmp_path, store, actions)
    console = product._Console(("please add the accepted file", "yes, do it", "START"))
    chat_workspace = tmp_path / "chat-workspace"
    chat_workspace.mkdir()
    running = asyncio.create_task(
        product.run_chat(
            runtime, console.console, workspace=chat_workspace, timeline=False, product=host
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 30)
        running.cancel()
        await asyncio.wait_for(cancelled.wait(), 30)
        running.cancel()
        assert not running.done()
    finally:
        release.set()
        assert await asyncio.wait_for(asyncio.shield(running), 30) == 130
    assert finished.is_set()
    assert not tuple(await PatchArtifactCatalogReader(store).load())
    from traceh.agents import AgentInboxReader
    from traceh.supervision import AgentRunReportReader

    child = next(
        r
        for r in (await AgentDirectoryReader(store).load()).records
        if "apply_patch" in r.capability_grants and "shell" not in r.capability_grants
    )
    accepted = (await AgentInboxReader(store).load(child.agent_id)).messages[0]
    report = await AgentRunReportReader(store).load(child.agent_id, accepted.message.message_id)
    assert report.status == "cancelled"
    assert (await product.BudgetLedgerReader(store).load()).account(
        child.agent_id
    ).closed_seq is not None
