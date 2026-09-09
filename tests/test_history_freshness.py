"""F4 freshness uses actual Git and host Tool envelopes, with frozen replay."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from memory_fixtures import bind, memory_policy
from retrieval_fixtures import retrieval_policy
from test_history_runtime import SelectingProvider, policy, select_page
from test_local_git_workspaces import _git, _repository

from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.memory import ProjectMemoryConfig, ProjectScopeLimits
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots
from traceh.session.context_input import parse_context_input, validate_context_input_sources
from traceh.session.event_store import InMemoryEventStore
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


@asynccontextmanager
async def git_case(tmp_path, *, resolver_type=LocalGitWorkspaceProvider):
    source, revision = _repository(tmp_path / "source")
    resolver = resolver_type(managed_root=tmp_path / "managed", sources={"source-orion": source})
    provider = SelectingProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("read-source", "read_file", {"path": "tracked.txt"}),)
            ),
            ModelResponse(content="historical observation"),
            select_page,
            ModelResponse(content="done"),
        ]
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            context_input=policy(memory=retrieval_policy(), workspace_observations=True),
            memory=ProjectMemoryConfig(ProjectScopeLimits(100, 100), memory_policy(), resolver),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    try:
        session = await runtime.create_session(source)
        await bind(runtime.project_scope, session)
        yield runtime, provider, session, resolver, source, revision
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "change,expected",
    [("none", "matched"), ("commit", "stale"), ("dirty", "unknown"), ("source-dirty", "unknown")],
)
async def test_real_tool_history_and_raw_page_freshness_are_frozen(tmp_path, change, expected):
    async with git_case(tmp_path) as (runtime, provider, session, _, source, revision):
        if change == "source-dirty":
            (source / "tracked.txt").write_text("dirty observation\n", encoding="utf-8")
        await runtime.run_existing(session, "Inspect tracked source")
        events = await runtime.sessions.read_session(session)
        tool = next(e for e in events if e.type == "tool/result")
        assert tool.data["status"] == "succeeded"
        assert ("dirty observation" if change == "source-dirty" else "base") in tool.data["content"]
        assert tool.data["workspace_observation"]["source_revision"] == (
            None if change == "source-dirty" else revision
        )
        await runtime.compaction.replace_through(
            session, through_seq=events[-1].seq, summary="Recorded workspace inspection"
        )
        if change in {"commit", "dirty"}:
            (source / "tracked.txt").write_text("changed source\n", encoding="utf-8")
        if change == "commit":
            _git("add", "tracked.txt", cwd=source)
            _git("commit", "-m", "fixture revision change", cwd=source)
        if change == "source-dirty":
            _git("restore", "tracked.txt", cwd=source)
        await runtime.run_existing(session, "Read earlier inspection")
        events = await runtime.sessions.read_session(session)
        contexts = [e for e in events if e.type == "context/input" and e.data["blocks"]]
        assert [e.data["blocks"][0]["tier"] for e in contexts] == ["directory", "chunk"]
        directory = json.loads(contexts[0].data["blocks"][0]["body"])
        assert directory["original_bytes"] == contexts[1].data["blocks"][0]["content_bytes"]
        assert [e.data["blocks"][0]["provenance"]["freshness"] for e in contexts] == [
            expected,
            expected,
        ]
        snapshot = [e for e in events if e.type == "request/snapshot"][-1]
        (source / "untracked.txt").write_text("later workspace mutation", encoding="utf-8")
        rebuilt = await reconstruct_request(runtime.sessions, runtime.surface, session, snapshot)
        assert rebuilt.request == provider.requests[-1]
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        # Rehashing a Context payload cannot change the derivation from source envelopes.
        data = contexts[-1].data.copy()
        data = parse_context_input(data).to_dict()
        data["blocks"][0]["provenance"]["freshness"] = "stale" if expected != "stale" else "matched"
        data["context_digest"] = fingerprint(
            {k: v for k, v in data.items() if k != "context_digest"}
        )
        with pytest.raises(ValueError):
            validate_context_input_sources(parse_context_input(data), events)


async def test_cancel_during_post_tool_observation_retains_outcome_and_converges(tmp_path):
    class GatedObserver(LocalGitWorkspaceProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.entered = asyncio.Event()
            self.finished = asyncio.Event()
            self.reads = 0

        async def project_observation(self, source_id, workspace):
            self.reads += 1
            # Context, before Tool, then after Tool: the actual file read has completed.
            if self.reads == 3:
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.finished.set()
            return await super().project_observation(source_id, workspace)

    async with git_case(tmp_path, resolver_type=GatedObserver) as (
        runtime,
        _,
        session,
        resolver,
        _,
        _,
    ):
        task = asyncio.create_task(runtime.run_existing(session, "Inspect source"))
        await asyncio.wait_for(resolver.entered.wait(), 20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resolver.finished.is_set()
        events = await runtime.sessions.read_session(session)
        results = [e for e in events if e.type == "tool/result"]
        assert len(results) == 1 and results[0].data["status"] == "succeeded"
        assert "base" in results[0].data["content"]
        assert results[0].data["workspace_observation"] is None
        assert events[-1].type == "turn/end"


async def test_git_change_during_observation_is_unknown(tmp_path):
    class ChangingObserver(LocalGitWorkspaceProvider):
        async def _run_required(self, command, *, cwd, **kwargs):
            result = await super()._run_required(command, cwd=cwd, **kwargs)
            if "status" in command:
                (cwd / "tracked.txt").write_text("new revision\n", encoding="utf-8")
                _git("add", "tracked.txt", cwd=cwd)
                _git("commit", "-m", "fixture concurrent change", cwd=cwd)
            return result

    source, _ = _repository(tmp_path / "source")
    resolver = ChangingObserver(
        managed_root=tmp_path / "managed", sources={"explicit-source": source}
    )
    observation = await resolver.project_observation("explicit-source", source)
    assert observation["source_revision"] is None
