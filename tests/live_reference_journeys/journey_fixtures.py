"""Explicit synthetic fixtures on production authority, Runtime and real Git owners.

Only the opt-in journey runner consumes this module. Nothing here is a product default.
"""

import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from live_skill_navigation.run import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    fixture,
    policies,
    source_plugin,
)

from traceh.api.history import HistoryReadPolicy
from traceh.api.memory import MemoryPolicy, ProjectMemoryConfig, ProjectScopeLimits
from traceh.api.plugins import PluginIdentity
from traceh.api.skills import SkillLimits, SkillPolicy, SkillResourceRoot
from traceh.llm.retry import ModelRetryPolicy
from traceh.projects.events import reference
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


def git(workspace, *arguments):
    """Only isolated, explicitly supplied fixture repositories are modified."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={workspace.as_posix()}", *arguments],
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("fixture-git-failed")
    return result.stdout.decode("utf-8").strip()


def repository(path, files):
    path.mkdir()
    git(path, "init", "--initial-branch=main")
    for name, body in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git(path, "add", ".")
    commit(path, "synthetic fixture base")


def commit(path, message):
    git(
        path,
        "-c",
        "user.name=TraceHarness Evaluation Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
    )


class JourneyReadPolicy:
    name = "explicit-journey-read-tools"

    def __init__(self, names):
        self.names = frozenset(names)

    async def check(self, call, tool, context):
        del tool, context
        return ToolDecision(
            DecisionKind.ALLOW if call.name in self.names else DecisionKind.DENY,
            "Synthetic evaluation permits reference and workspace reading only.",
            self.name,
        )


def context_policy(case):
    """Explicit fixture limits; serialized by the runner before any model call."""
    baseline = policies("directory")
    return replace(
        baseline,
        memory=replace(baseline.skills, default_tier=case["memory_tier"]),
        history_tier="directory",
        history_bytes=60_000,
        history=HistoryReadPolicy(
            max_blocks=16,
            max_depth=16,
            page_bytes=24_000,
            page_messages=2 if case["setup"] == "history-two" else 30,
            max_source_events=2_000,
            max_source_bytes=4_000_000,
            max_requests=12,
        ),
        workspace_observations=True,
    )


@asynccontextmanager
async def build_journey(root, corpus, skill_corpus, case, repeat, model, provider):
    root.mkdir(parents=True, exist_ok=False)
    workspace, other = root / "workspace", root / "other-project"
    repository(
        workspace,
        {
            corpus["files"]["current"]["path"]: corpus["files"]["current"]["body"],
            corpus["files"]["observed"]["path"]: corpus["files"]["observed"]["before"],
        },
    )
    repository(other, {"fixture.txt": "Different synthetic project.\n"})
    inputs = root / "plugin-inputs"
    inputs.mkdir()
    values, references, selected = fixture(skill_corpus, inputs, repeat)
    discovery = source_plugin(inputs, values)

    def identity(name):
        return "j" + uuid5(NAMESPACE_URL, f"reference-journey/{repeat}/{name}").hex

    resolver = LocalGitWorkspaceProvider(
        managed_root=root / "uncreated-managed",
        sources={identity("source"): workspace, identity("other-source"): other},
    )
    memory_config = ProjectMemoryConfig(
        ProjectScopeLimits(120, 200),
        MemoryPolicy(2048, 4, 400, 400_000, 120, (r"fixture-private-input",)),
        resolver,
    )
    bounds = corpus["bounds"]
    config = RuntimeConfig(
        data_dir=root / "data",
        provider="openai-compatible",
        model=model,
        max_steps=bounds["max_steps"],
        max_output_tokens=bounds["max_output_tokens"],
        max_tool_output_chars=80_000,
        temperature=bounds["temperature"],
        model_retry_policy=ModelRetryPolicy(**bounds["retry"]),
        context_input=context_policy(case),
        memory=memory_config,
        skill_policy=SkillPolicy(
            SkillLimits(
                max_skills=8,
                max_catalog_bytes=64_000,
                max_summary_bytes=1000,
                max_content_bytes=64_000,
                max_resource_bytes=32_000,
            ),
            (SkillResourceRoot(PluginIdentity(PLUGIN_ID, PLUGIN_VERSION), inputs),),
        ),
    )
    store = SqliteEventStore(root / "events")
    runtime = None
    try:
        runtime = await build_default_runtime_async(
            config,
            provider=provider,
            event_store=store,
            enabled_plugins=(PLUGIN_ID,),
            plugin_discovery=discovery,
            policies=(JourneyReadPolicy(bounds["allowed_tools"]),),
        )
        session = await runtime.create_session(workspace)
        sessions = [session]
        scope = runtime.project_scope

        async def bind(session_id, suffix=""):
            catalog = await scope.catalog()
            project_id = identity(suffix + "project")
            if project_id not in catalog.projects:
                await scope.create(
                    project_id=project_id,
                    label="Synthetic evaluation project",
                    operation_id=identity(suffix + "create"),
                    actor_id="evaluation-host",
                    expected_head=catalog.head,
                )
                await scope.bind_source(
                    project_id=project_id,
                    source_id=identity(suffix + "source"),
                    operation_id=identity(suffix + "source-binding"),
                    actor_id="evaluation-host",
                    expected_head=(await scope.catalog()).head,
                )
            await scope.bind_session(
                session_id,
                project_id=project_id,
                operation_id="bind:" + session_id,
                actor_id="evaluation-host",
                expected_head=(await scope.catalog()).head,
            )

        await bind(session)
        yield {
            "runtime": runtime,
            "store": store,
            "session": session,
            "sessions": sessions,
            "workspace": workspace,
            "other": other,
            "identity": identity,
            "bind": bind,
            "references": references,
            "selected": selected,
            "config": config,
            "proposals": {},
            "activations": {},
        }
    finally:
        if runtime is not None:
            await runtime.dispose()
        await store.aclose()
        sys.path.remove(str(inputs))


async def seed_memory(state, corpus, name, *, session=None, approve=True, predecessor=None):
    runtime, identity = state["runtime"], state["identity"]
    session = session or state["session"]
    fact = corpus["facts"][name]
    proposal = await runtime.memory.declare(
        session,
        proposal_id=identity("proposal-" + name),
        body=fact["body"],
        statement=fact["body"],
        declaration_id=identity("declaration-" + name),
        operation_id=identity("declare-" + name),
        actor_id="evaluation-host",
        expected_head=(await runtime.memory.read(session)).head,
    )
    state["proposals"][name] = proposal
    if not approve:
        return
    arguments = {
        "proposal_ref": reference(proposal),
        "proposal_digest": proposal.data["proposal_digest"],
        "memory_id": identity("memory-" + name),
        "fact_slot": fact["fact_slot"],
        "operation_id": identity("approve-" + name),
        "actor_id": "evaluation-host",
        "expected_head": (await runtime.memory.read(session)).head,
    }
    if predecessor:
        arguments.update(
            predecessor_ref=reference(state["activations"][predecessor]),
            predecessor_digest=state["proposals"][predecessor].data["proposal_digest"],
        )
    method = runtime.memory.supersede if predecessor else runtime.memory.approve
    state["activations"][name] = await method(session, **arguments)
