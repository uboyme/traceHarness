# Plugin and multi-agent evolution

v0.3 deliberately stops before automatic third-party plugin discovery, but it contains
the primitives needed to add it without replacing the loop.

## v0.4 plugin manager

A plugin package can be discovered through Python entry points and loaded into a private
`Activation`:

1. validate manifest and API major versions;
2. create a private Scope;
3. run `setup()`;
4. collect registrations, cleanups and owned tasks;
5. run conflict and health checks;
6. atomically publish a new Composition generation;
7. drain and dispose the old generation after existing Step leases finish.

No plugin should mutate a live registry dictionary directly. Every registration must be
owned by an Activation and reversible.

## Extension categories

- LLM provider: register `LlmProvider`.
- Tool bundle: register `Tool` objects.
- Policy: register `ToolPolicy`.
- Prompt: register deterministic `PromptSection` values.
- Persistence: implement `EventStore` and pass contract tests.
- Verification: implement `CompletionVerifier`.
- Observability: subscribe to typed NOTIFY hooks.

Behavior that changes a model request must be represented in the Composition Snapshot,
otherwise request reconstruction will correctly report a mismatch.

## v0.6 AgentSupervisor

Multi-agent support should be built above `AgentLoop` with:

- durable Agent identity and Session;
- one FIFO Inbox per Agent;
- at most one live Activation per Session;
- explicit lifecycle ownership;
- separate history lineage, communication and workspace relationships;
- child-first cancellation and quiescent disposal;
- budget allocation and depth limits.

Subagent operations become normal tools (`spawn_agent`, `send_agent_message`,
`wait_agent`, `collect_artifact`) backed by `AgentSupervisor`. The loop remains unaware
that a tool creates another Agent.

## v0.7 workspaces and workflows

Writable coding children should receive isolated worktrees or overlay workspaces and
return Patch Artifacts plus test evidence. A workflow layer can compose Agent tasks,
parallel maps, joins, approvals and verification by calling public Supervisor and Tool
APIs.
