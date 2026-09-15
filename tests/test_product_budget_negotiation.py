"""Budget negotiation through the real Product factory, Git and sandbox boundary."""

import json
from dataclasses import replace

import pytest
import test_product_adaptive as multi
import test_product_f3_e2e as product
from collaboration_fixtures import PLAN
from promotion_fixtures import build_source_repository, make_bare_target

from traceh.api.llm import ToolCall
from traceh.session.sqlite import SqliteEventStore


class PartialBudgetProvider:
    name = "product-provider"

    def __init__(self):
        self.child_steps = 0
        self.report = None

    async def complete(self, request):
        names = {t.name for t in request.tools}
        if "traceh.product.investigation" in request.system_prompt:
            self.child_steps += 1
            if self.child_steps == 1:
                return product._response("", ToolCall("read", "read_file", {"path": "tracked.txt"}))
            return product._response(
                "",
                ToolCall(
                    "ask",
                    "request_investigation_budget",
                    {
                        "tokens": 5000,
                        "progress": "tracked.txt:1 contains base",
                        "remaining_work": "Explain implications after more investigation",
                    },
                ),
            )
        if "submit_collaboration_plan" in names:
            return product._response(
                "",
                ToolCall(
                    "decision",
                    "submit_collaboration_plan",
                    PLAN,
                ),
            )
        if "apply_patch" not in names:
            return product._response("Ready to decide.")
        assert not names & {"decide_investigation_budget", "followup_investigation"}
        self.report = next(
            json.loads(m.content)["child"]
            for m in request.messages
            if m.role == "tool" and m.name == "submit_collaboration_plan"
        )
        assert self.report["reason"] == "investigation_budget_requested"
        assert "unfinished" in self.report["interpretation"]
        return await product._ProductProvider().complete(request)


def negotiation_profile(mode):
    base = multi.profile(mode)
    return replace(
        base,
        investigator_initial_tokens=20000,
        investigator=replace(
            base.investigator,
            budget=replace(
                base.investigator.budget,
                max_tokens=30000,
                max_steps=10,
                max_tool_calls=10,
                max_wall_milliseconds=120000,
            ),
            max_turn_wall_milliseconds=60000,
        ),
        coder=replace(base.coder, max_turn_wall_milliseconds=120000),
    )


@pytest.mark.asyncio
async def test_product_returns_partial_budget_report_without_grant_or_followup(
    tmp_path, monkeypatch
):
    original = product._host_profile
    monkeypatch.setattr(
        product,
        "_host_profile",
        lambda mode: replace(original(mode), profile=negotiation_profile(mode)),
    )
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    provider = PartialBudgetProvider()
    async with SqliteEventStore(tmp_path / "events.sqlite") as store:
        from collaboration_fixtures import run_failed_product

        _, events = await run_failed_product(tmp_path, store, source, target, provider)
        assert provider.child_steps == 2
        assert provider.report is None
        assert any(
            e.type == "runtime/error" and e.data["error_type"] == "CollaborationChildIncomplete"
            for e in events
        )
        result = next(
            e.data["data"]
            for e in events
            if e.type == "tool/result" and e.data.get("tool_name") == "submit_collaboration_plan"
        )
        assert result["outcome"] == "child_incomplete"
        assert result["child"]["budget_requests"][0]["decision"] is None
        assert result["child"]["budget"]["allocated_tokens"] == 20000
