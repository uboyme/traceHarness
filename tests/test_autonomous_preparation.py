"""Real readonly batches and corrected assignments, without model-specific defaults."""

import asyncio
import json
from copy import deepcopy

import pytest
from supervision_fixtures import SPEC, GatedProvider, RuntimeFactory
from test_investigation_tools import WORK, BoundPolicy
from test_product_f3_e2e import _response

from traceh.agents import AgentDirectoryReader
from traceh.api.llm import ToolCall
from traceh.product.collaboration import CollaborationContinuation, CollaborationPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.surface import SurfaceProjector
from traceh.supervision import ProcessAgentSupervisor
from traceh.supervision.delegation import InvestigationToolset
from traceh.supervision.structured_collaboration import (
    SUBMIT_COLLABORATION,
    CollaborationPlanTool,
    investigator_role,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["schema", "text", "duplicate", "denied", "none"])
async def test_batch_reads_continue_until_model_submits_corrected_assignment(tmp_path, invalid):
    store = InMemoryEventStore()
    child_provider = GatedProvider()
    child_provider.release.set()
    supervisor = ProcessAgentSupervisor(
        store=store,
        factory=RuntimeFactory(store, tmp_path, provider=child_provider),
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    paths = ("requirements.txt", "interface.txt", "constraints.txt")
    for path in paths:
        (tmp_path / SPEC.workspace_id / path).write_text(
            "source evidence " + path, encoding="utf-8"
        )
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )

    class Provider:
        name = "autonomous-test"
        calls = 0
        requests = []

        async def complete(self, request):
            self.calls += 1
            self.requests.append(request)
            names = {t.name for t in request.tools}
            if SUBMIT_COLLABORATION not in names:
                return _response("Completed using the child report.")
            assert {"read_file", "search_text", "list_files"} <= names
            assert not {"apply_patch", "shell"} & names
            if self.calls <= 4:
                return _response(
                    "",
                    *[
                        ToolCall(f"read-{self.calls}-{i}", "read_file", {"path": path})
                        for i, path in enumerate(paths)
                    ],
                )
            args = deepcopy(
                {
                    "main_work": WORK["main_work"],
                    "children": [
                        {
                            "assignment_id": "explicit-test-investigation",
                            "role": "investigator",
                            **{k: v for k, v in WORK.items() if k != "main_work"},
                        }
                    ],
                }
            )
            if self.calls == 5:
                if invalid == "schema":
                    args["children"] = None
                elif invalid == "text":
                    args["children"][0]["goal"] = " "
                elif invalid == "duplicate":
                    return _response(
                        "",
                        ToolCall("one", SUBMIT_COLLABORATION, args),
                        ToolCall("two", SUBMIT_COLLABORATION, args),
                    )
                elif invalid == "denied":
                    return _response(
                        "",
                        ToolCall("one", SUBMIT_COLLABORATION, args),
                        ToolCall(
                            "forbidden",
                            "apply_patch",
                            {
                                "path": "bad.txt",
                                "old_text": "",
                                "new_text": "bad",
                                "create": True,
                            },
                        ),
                    )
            return _response("", ToolCall(f"plan-{self.calls}", SUBMIT_COLLABORATION, args))

    provider = Provider()
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main", provider=provider.name, model="model", max_steps=16
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, owner.session_id),
        additional_tools=(*toolset.tools, CollaborationPlanTool((investigator_role(
            toolset.control
        ),))),
    )
    try:
        await asyncio.wait_for(
            runtime.run_existing(owner.session_id, "Understand and allocate."), 20
        )
        records = (await AgentDirectoryReader(store).load()).records
        assert len([r for r in records if r.owner_agent_id == owner.agent_id]) == 1
        events = await runtime.sessions.read_session(owner.session_id)
        reads = [
            e for e in events if e.type == "tool/result" and e.data["tool_name"] == "read_file"
        ]
        assert len(reads) == 12 and all(e.data["status"] == "succeeded" for e in reads)
        for path in paths:
            assert any(
                "source evidence " + path in m.content for m in provider.requests[4].messages
            )
        plans = [
            e
            for e in events
            if e.type == "tool/result" and e.data["tool_name"] == SUBMIT_COLLABORATION
        ]
        assert len(plans) == (1 if invalid == "none" else 3 if invalid == "duplicate" else 2)
        expected = (
            "invalid" if invalid == "schema" else "denied" if invalid == "denied" else "failed"
        )
        assert all(e.data["status"] == expected for e in plans[:-1])
        assert not (tmp_path / SPEC.workspace_id / "bad.txt").exists()
        assert json.loads(plans[-1].data["content"])["outcome"] == "completed"
        assert not await runtime.check_invariants(owner.session_id)
        assert not await verify_request_snapshots(
            runtime.sessions, SurfaceProjector(), owner.session_id
        )
    finally:
        await runtime.dispose()
        await supervisor.aclose()
