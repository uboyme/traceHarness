"""Product START reaches the same isolated shell and fixed review verifier."""

import shlex
from dataclasses import replace

from promotion_fixtures import (
    build_source_repository,
    capture_limits,
    make_bare_target,
    promotion_targets,
)
from test_product_f3_e2e import _chat_runtime, _Console, _profile, _response
from test_sandbox_docker import settings as settings
from test_sandbox_promotion import plan

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.api.sandbox import SandboxConfiguration
from traceh.artifacts.cas import LocalArtifactCas
from traceh.cli.chat import run_chat
from traceh.cli.product import LineProductAdapter
from traceh.product.chat import ProductTurnActions
from traceh.product.host import ProductHostProfile, build_product_chat_host
from traceh.promotion.projection import PromotionLedgerReader
from traceh.session.event_feed import PublishingEventStore, SessionEventFeed
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


class ProductShellProvider:
    name = "product-provider"

    async def complete(self, request):
        if any(message.role == "tool" for message in request.messages):
            return _response("completed the isolated edit")
        command = shlex.join(
            ("python", "-c", "from pathlib import Path; Path('added.txt').write_text('added\\n')")
        )
        return _response("", ToolCall("isolated-edit", "shell", {"command": command}))


async def test_product_start_runs_isolated_edit_and_fixed_verification(tmp_path, settings):
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    feed = SessionEventFeed()
    connected = PublishingEventStore(store, feed)
    cas = LocalArtifactCas(tmp_path / "cas")
    actions = ProductTurnActions()
    profile = _profile(RequestedTaskMode.SINGLE)
    verification = replace(
        plan("from pathlib import Path; assert Path('added.txt').read_text()=='added\\n'"),
        plan_id=profile.verification_plan_id,
    )
    host = await build_product_chat_host(
        store=connected,
        sessions=SessionService(connected),
        data_dir=tmp_path / "product-data",
        host_profile=ProductHostProfile("product-profile", profile, verification),
        providers={"product-provider": ProductShellProvider()},
        workspace_provider=LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed", sources={"product-source": source}
        ),
        artifact_cas=cas,
        promotion_targets=promotion_targets("product-target", target),
        capture_limits=capture_limits(),
        approver_id="human",
        max_report_chars=4096,
        event_feed=feed,
        actions=actions,
        sandbox=SandboxConfiguration(settings, cas.local_root),
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    console = _Console(("please add the accepted file", "yes, do it", "START"))
    workspace = tmp_path / "chat"
    workspace.mkdir()
    try:
        result = await run_chat(
            runtime,
            console.console,
            workspace=workspace,
            timeline=False,
            product=LineProductAdapter(host, data_dir=tmp_path / "product-data"),
        )
        assert result == 0
        assert "awaiting_approval" in console.output, console.output
        ledger = await PromotionLedgerReader(store).load()
        assert len(ledger.reviews) == 1
        report = ledger.reviews[0]
        assert report.passed and report.results[0].execution is not None
        executions = [
            e
            for stream in await store.list_streams()
            for e in await store.read(stream)
            if e.type == "sandbox/request"
        ]
        assert {e.data["owner"]["kind"] for e in executions} == {"effect", "promotion"}
        owner = next(e.data["owner"] for e in executions if e.data["owner"]["kind"] == "effect")
        assert owner["agent_id"] and owner["budget_admission"] and owner["budget_reservation"]
        assert not (source / "added.txt").exists()
    finally:
        await host.aclose()
        await runtime.dispose()
