"""Four closed preparation recipes using the original production owners."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from traceh.api.json_types import fingerprint
from traceh.api.memory import MemoryPolicy, ProjectMemoryConfig, ProjectScopeLimits
from traceh.api.plugins import PluginManifest
from traceh.api.sandbox import SandboxConfiguration
from traceh.api.skills import SkillLimits, SkillPolicy, SkillResourceRoot
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.evaluation.errors import BenchmarkExecutionError
from traceh.evaluation.evaluators.episode_manifest import skill_contribution
from traceh.evaluation.inputs import referenced_input
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.plugins.discovery import ENTRY_POINT_GROUP, DiscoveredPlugin, PluginDiscovery
from traceh.projects.events import reference
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.session.context_input import ContextInputPolicy
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision

SOURCE_TOOLS = {
    "history": frozenset({"search_history", "request_history_page"}),
    "skill": frozenset({"search_skill", "request_skill_reference"}),
    "memory": frozenset({"search_memory", "request_workspace_memory"}),
    "output": frozenset({"list_tool_outputs", "search_tool_output", "read_tool_output"}),
}


class SourceScope:
    name = "evaluation-source-isolated"

    def __init__(self, family, command=None):
        self.allowed = SOURCE_TOOLS[family]
        self.command = command
        self.preparing = True
        self.claimed = False

    async def check(self, call, tool, context):
        allowed = call.name in self.allowed
        if (
            self.preparing
            and not self.claimed
            and self.command is not None
            and call.name == "shell"
            and call.arguments.get("command") == self.command
        ):
            self.claimed = True
            allowed = True
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "Use the configured reference source. Workspace exploration and command reruns "
            "are outside this isolated episode.",
            self.name,
        )


class EpisodeResolver:
    def __init__(self, source_id, workspace):
        self.source_id, self.workspace = source_id, workspace

    async def project_fingerprint(self, source_id, workspace):
        if source_id != self.source_id or (workspace is not None and workspace != self.workspace):
            raise ValueError("project-workspace-source-mismatch")
        return fingerprint({"source": str(self.workspace)})


class MaterialPlugin:
    """Data-only trusted host contribution, activated by the original PluginManager."""

    def __init__(self, contribution):
        self.contribution = contribution
        owner = contribution.descriptor.plugin
        self.manifest = PluginManifest(owner.plugin_id, owner.version)

    async def setup(self, context, config):
        context.register_skill(self.contribution)


class MaterialEntry:
    def __init__(self, plugin):
        self.plugin = plugin

    def load(self):
        return self.plugin


class MaterialDiscovery(PluginDiscovery):
    """Explicit host inventory, no package scan, callback path or test fixture imports."""

    def __init__(self, contribution):
        plugin = MaterialPlugin(contribution)
        self.record = DiscoveredPlugin(
            plugin.manifest.plugin_id,
            __name__ + ":MaterialPlugin",
            ENTRY_POINT_GROUP,
            "traceh-evaluation-material",
            plugin.manifest.version,
            plugin.manifest.requires_traceh,
            entry_point=MaterialEntry(plugin),
        )

    def discover(self):
        return (self.record,)


@dataclass
class PreparedEpisode:
    runtime: object
    store: object
    session_id: str
    setup_session_id: str
    workspace: object
    scope: SourceScope
    contribution: object


@asynccontextmanager
async def episode_runtime(context, case, manifest, *, provider, model_id, retry_policy, sandbox):
    folder = context.directory
    folder.mkdir(parents=True, exist_ok=False)
    workspace = folder / "workspace"
    workspace.mkdir()
    data, settings = case.data, manifest.task_settings
    family, setup = data["family"], data["setup"]
    scope = SourceScope(family, setup["command"] if family == "output" else None)
    options, contribution, memory, skill_policy = {}, None, None, None
    if family == "memory":
        memory = ProjectMemoryConfig(
            ProjectScopeLimits(**settings["project_limits"]),
            MemoryPolicy(
                **{
                    **settings["memory_policy"],
                    "denied_patterns": tuple(settings["memory_policy"]["denied_patterns"]),
                }
            ),
            EpisodeResolver(setup["source_id"], workspace),
        )
    if family == "skill":
        contribution = skill_contribution(setup)
        resources = folder / "plugin-resources"
        resources.mkdir()
        for descriptor, ref in zip(
            contribution.descriptor.resources, setup["resources"], strict=True
        ):
            target = resources / descriptor.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(referenced_input(manifest.directory, ref).content)
        skill_policy = SkillPolicy(
            SkillLimits(**settings["skill_limits"]),
            (SkillResourceRoot(contribution.descriptor.plugin, resources),),
        )
        options = {
            "enabled_plugins": (contribution.descriptor.plugin.plugin_id,),
            "plugin_discovery": MaterialDiscovery(contribution),
        }
    raw = settings["runtime"]
    config = RuntimeConfig(
        data_dir=folder / "data",
        provider=provider.name,
        model=model_id,
        **{k: v for k, v in raw.items() if k != "token_budget"},
        token_budget=TokenBudgetPolicy(**raw["token_budget"]),
        model_retry_policy=retry_policy,
        context_input=ContextInputPolicy.from_dict(settings["contexts"][family]),
        memory=memory,
        skill_policy=skill_policy,
        sandbox=SandboxConfiguration(sandbox, (folder / "cas").resolve())
        if sandbox is not None
        else None,
    )
    store, runtime, primary = SqliteEventStore(folder / "events"), None, None
    try:
        runtime = await build_default_runtime_async(
            config, provider=provider, event_store=store, policies=(scope,), **options
        )
        session = await runtime.create_session(workspace)
        prepared = PreparedEpisode(runtime, store, session, session, workspace, scope, contribution)
        yield prepared
    except BaseException as error:
        primary = error
    finally:

        async def close():
            failure = None
            if runtime is not None:
                try:
                    await runtime.dispose()
                except BaseException as error:
                    failure = error
            try:
                await store.aclose()
            except BaseException as error:
                failure = combine_failures(failure, error, "episode store close failed")
            if failure is not None:
                raise failure

        worker = asyncio.create_task(close())
        try:
            await asyncio.shield(worker)
        except BaseException as error:
            await await_worker_convergence(worker)
            cleanup = worker.exception() if not worker.cancelled() else error
            primary = combine_failures(primary, cleanup or error, "episode cleanup failed")
        if primary is not None:
            raise primary


async def _memory(prepared, setup):
    runtime, session = prepared.runtime, prepared.session_id
    project, source = setup["project_id"], setup["source_id"]
    await runtime.project_scope.create(
        project_id=project,
        label=setup["label"],
        operation_id="create",
        actor_id="evaluation-host",
        expected_head=0,
    )
    await runtime.project_scope.bind_source(
        project_id=project,
        source_id=source,
        operation_id="source",
        actor_id="evaluation-host",
        expected_head=1,
    )
    await runtime.project_scope.bind_session(
        session,
        project_id=project,
        operation_id="bind",
        actor_id="evaluation-host",
        expected_head=2,
    )
    approved = {}
    for index, item in enumerate(setup["items"]):
        head = (await runtime.memory.read(session)).head
        proposal = await runtime.memory.declare(
            session,
            proposal_id=f"proposal-{index}",
            body=item["body"],
            statement=item["body"],
            declaration_id=f"declaration-{index}",
            operation_id=f"declare-{index}",
            actor_id="evaluation-host",
            expected_head=head,
        )
        arguments = dict(
            proposal_ref=reference(proposal),
            proposal_digest=proposal.data["proposal_digest"],
            memory_id=item["memory_id"],
            fact_slot=item["fact_slot"],
            operation_id=f"approve-{index}",
            actor_id="evaluation-host",
            expected_head=head + 1,
        )
        if item["predecessor_id"] is not None:
            previous, activation = approved[item["predecessor_id"]]
            activation = await runtime.memory.supersede(
                session,
                **arguments,
                predecessor_ref=reference(activation),
                predecessor_digest=previous.data["proposal_digest"],
            )
        else:
            activation = await runtime.memory.approve(session, **arguments)
        approved[item["memory_id"]] = proposal, activation
    if setup["revoke_id"] is not None:
        proposal, activation = approved[setup["revoke_id"]]
        await runtime.memory.revoke(
            session,
            memory_id=setup["revoke_id"],
            fact_slot=activation.data["fact_slot"],
            predecessor_ref=reference(activation),
            predecessor_digest=proposal.data["proposal_digest"],
            operation_id="revoke",
            actor_id="evaluation-host",
            expected_head=(await runtime.memory.read(session)).head,
        )
    if setup["second_session"]:
        prepared.session_id = await runtime.create_session(prepared.workspace)
        await runtime.project_scope.bind_session(
            prepared.session_id,
            project_id=project,
            operation_id="bind-reader",
            actor_id="evaluation-host",
            expected_head=3,
        )
    await runtime.memory.rebuild_index(prepared.session_id)


async def setup_episode(prepared, case, manifest):
    data, runtime = case.data, prepared.runtime
    family, setup, session = data["family"], data["setup"], prepared.session_id
    if family == "history":
        for text in setup["turns"]:
            result = await runtime.run_existing(session, setup["prompt_prefix"] + text)
            if result.reason != "completed":
                raise BenchmarkExecutionError("evaluation-history-setup-failed")
        events = await runtime.sessions.read_session(session)
        await runtime.compaction.replace_through(
            session, through_seq=events[-1].seq, summary=setup["summary"]
        )
    elif family == "skill":
        descriptor = prepared.contribution.descriptor
        await runtime.skill_context.select(
            session,
            operation_id="select",
            expected_head=0,
            actor_id="evaluation-host",
            skills=({"skill_id": descriptor.skill_id, "version": descriptor.version},),
        )
        await runtime.skill_context.rebuild_index(session)
    elif family == "memory":
        await _memory(prepared, setup)
    else:
        target = prepared.workspace / setup["script_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(referenced_input(manifest.directory, setup["script"]).content)
        result = await runtime.run_existing(session, setup["prompt"])
        counter = prepared.workspace / setup["counter_path"]
        if result.reason != "completed" or not counter.is_file() or counter.read_text() != "1":
            raise BenchmarkExecutionError("evaluation-output-setup-not-executed-once")
    prepared.scope.preparing = False
