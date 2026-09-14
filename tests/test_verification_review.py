"""Current receipts reach a real managed main request; they do not award quality."""

import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from promotion_fixtures import build_source_repository, make_bare_target
from test_product_adaptive import profile
from test_structured_product import StructuredProvider

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.artifacts.cas import LocalArtifactCas
from traceh.product.verification_review import REVIEW_GUIDANCE, execution_receipts
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [None, 0, 1])
@pytest.mark.parametrize("checked_before_review", [False, True])
async def test_review_reconciles_actual_calls_without_inventing_tests(
    tmp_path, monkeypatch, exit_code, checked_before_review
):
    original = product._host_profile
    monkeypatch.setattr(
        product, "_host_profile", lambda mode: replace(original(mode), profile=profile(mode))
    )
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()

    class Provider(StructuredProvider):
        review_requests = 0

        async def complete(self, request):
            if REVIEW_GUIDANCE not in request.system_prompt:
                if (
                    checked_before_review and exit_code is not None
                    and any(m.role == "tool" and m.name == "apply_patch"
                            for m in request.messages)
                    and not any(m.role == "tool" and m.name == "shell"
                                for m in request.messages)
                ):
                    return product._response(
                        "", ToolCall("observed-check", "shell", {
                            "command": f'python -c "assert {exit_code} == 0"'
                        })
                    )
                return await super().complete(request)
            self.review_requests += 1
            assert "entering this checkpoint does not require rereading" in request.system_prompt
            assert "The host has returned the report" not in request.system_prompt
            packet = json.loads(request.system_prompt.split(REVIEW_GUIDANCE, 1)[1])
            assert packet["delivery"]["status"] == "observed"
            assert packet["delivery"]["changed_paths"] == ["added.txt"]
            assert "no semantic" in packet["delivery"]["interpretation"]
            assert packet["total_calls"] == len(packet["receipts"])
            assert all(r["tool_call_id"] != "child-read" for r in packet["receipts"])
            checks = [r for r in packet["receipts"] if r["tool"] == "shell"]
            if exit_code is not None and not checks:
                return product._response(
                    "",
                    ToolCall(
                        "observed-check",
                        "shell",
                        {"command": f'python -c "assert {exit_code} == 0"'},
                    ),
                )
            if exit_code is None:
                assert checks == []
                return product._response("File modified. No functional verification was executed.")
            assert len(checks) == 1
            assert checks[0]["tool_status"] == "succeeded"
            assert checks[0]["exit_code"] == exit_code
            return product._response(
                f"observed-check exited {exit_code}; this is a fixture check only."
            )

    provider = Provider("delegate")
    await product._run_to_barrier(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        RequestedTaskMode.MULTI,
        provider,
    )
    assert provider.review_requests == (1 if exit_code is None or checked_before_review else 2)
    sessions = SessionService(store)
    for stream in await store.list_streams(prefix="session:"):
        sid = stream.removeprefix("session:")
        events = await sessions.read_session(sid)
        assert not await verify_request_snapshots(sessions, SurfaceProjector(), sid)
        assert json.loads(execution_receipts(events, "unrelated-turn"))["total_calls"] == 0


@pytest.mark.asyncio
async def test_receipts_bound_history_and_bind_calls_to_current_turn(tmp_path):
    (tmp_path / "note.txt").write_text("evidence", encoding="utf-8")

    class Provider:
        name = "receipt-fixture"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            if self.calls <= 9 or self.calls == 11:
                return product._response(
                    "", ToolCall(f"read-{self.calls}", "read_file", {"path": "note.txt"})
                )
            return product._response("Read complete.")

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="receipt-fixture", model="fixture"),
        provider=Provider(),
        event_store=InMemoryEventStore(),
    )
    try:
        sid = await runtime.create_session(tmp_path)
        await runtime.run_existing(sid, "Read the note")
        await runtime.run_existing(sid, "Read it again")
        events = await runtime.sessions.read_session(sid)
        turns = list(dict.fromkeys(e.data["turn_id"] for e in events if e.type == "step/start"))
        first, second = [json.loads(execution_receipts(events, t)) for t in turns]
        assert first["total_calls"] == 9 and first["omitted_calls"] == 1
        assert len(first["receipts"]) == len({r["step_id"] for r in first["receipts"]}) == 8
        assert second["total_calls"] == 1 and second["omitted_calls"] == 0
        for receipt in first["receipts"] + second["receipts"]:
            assert receipt["tool_status"] == "succeeded"
            assert receipt["exit_code"] is None
            assert receipt["result_seq"] > receipt["call_seq"]
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
