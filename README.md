# TraceHarness Py v0.3

TraceHarness Py is an event-sourced, reconstructable Python runtime for building
traceable coding agents. The v0.3 base is intentionally small enough to understand,
but its public boundaries are designed for future plugins, scoped compositions,
subagents and workflows.

> Status: educational alpha. The code is runnable and tested, but the public API is not
> yet declared stable for third-party production use.

## What is included

- Append-only JSONL Session and Effect streams.
- Session / Turn / Step / Model Attempt / Tool Invocation lifecycle events.
- A thin async Agent Loop that delegates prompt, model, tool and continuation behavior.
- Frozen Composition Snapshots and independently reconstructable model requests.
- Scripted deterministic and OpenAI-compatible model providers.
- A unified Tool Runtime with schema validation, monotonic policies, timeout handling,
  read parallelism, exclusive write/process barriers and structured results.
- Five coding tools: `list_files`, `read_file`, `search_text`, `apply_patch`, `shell`.
- Workspace path confinement and child-process environment sanitization.
- Effect Intent / Dispatch / Outcome records for crash-window reasoning.
- Append-only recovery that closes orphaned calls, Steps and Turns without blindly
  replaying uncertain side effects.
- Evidence-driven completion through an optional external command verifier.
- Request reconstruction checks, protocol invariants, replay, manual Surface compaction and static HTML inspection.
- A deterministic benchmark runner and a no-key demo.
- Kernel primitives for future reversible plugin activation, typed hooks, hierarchical
  scopes and owned-task shutdown.

## Quick start without installing

Python 3.12 or newer is required.

```bash
cd traceharness-py-v0.3
export PYTHONPATH="$PWD/src"
python -m traceh.cli.main doctor
pytest
```

## Run the deterministic coding demo

The demo starts with an incorrect `add()` implementation. Copy it before running,
because the agent will modify the workspace.

```bash
cp -R examples/demo_bug /tmp/traceh-demo

PYTHONPATH=src python -m traceh.cli.main run \
  /tmp/traceh-demo \
  "Fix the addition bug and run the tests" \
  --script examples/demo_script.json \
  --verify-command "python -m unittest -v" \
  --data-dir /tmp/traceh-data
```

The command prints a `session_id`. Inspect and replay it:

```bash
PYTHONPATH=src python -m traceh.cli.main inspect <session-id> \
  --data-dir /tmp/traceh-data \
  --html /tmp/traceh-session.html

PYTHONPATH=src python -m traceh.cli.main replay <session-id> \
  --data-dir /tmp/traceh-data
```

## Install as a package

```bash
python -m pip install -e ".[dev]"
traceh doctor
```

The source tree has no runtime dependency outside the Python standard library.
`setuptools` is used as the build backend; normal `pip` build isolation installs it.

## Run the included benchmark

```bash
PYTHONPATH=src python -m traceh.cli.main eval benchmarks/basic \
  --output /tmp/traceh-eval
```

This writes `report.json`, `report.md`, a copied workspace and durable traces for each
case. A case succeeds only when the external verifier passes and protocol invariants
remain clean.

## Use an OpenAI-compatible endpoint

TraceHarness automatically loads `.env` from the current directory. Process environment
variables override `.env`, and explicit CLI options override both. Copy the included
template and keep the resulting `.env` file private:

```powershell
Copy-Item .env.example .env
```

For Alibaba Cloud Model Studio (Bailian) in the Beijing region, edit `.env` as follows:

```dotenv
TRACEH_PROVIDER=openai-compatible
TRACEH_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TRACEH_MODEL=qwen-plus
TRACEH_API_KEY_ENV=DASHSCOPE_API_KEY
DASHSCOPE_API_KEY=replace-with-your-api-key
```

Then verify that the file was loaded without printing the secret:

```powershell
traceh doctor
```

Run normally without API configuration arguments:

```powershell
traceh run . "Inspect the project and report what should be improved"
```

Use `--env-file path/to/file.env` to select another file. The supported runtime settings
are `TRACEH_PROVIDER`, `TRACEH_BASE_URL`, `TRACEH_MODEL`, `TRACEH_API_KEY_ENV`,
`TRACEH_DATA_DIR`, `TRACEH_MAX_STEPS` and `TRACEH_VERIFY_COMMAND`. An
`openai-compatible` provider requires an explicit Base URL and model; TraceHarness does
not silently select a vendor endpoint or model.

The equivalent process-environment configuration remains supported:

```bash
export OPENAI_API_KEY=...

PYTHONPATH=src python -m traceh.cli.main run ./your-project \
  "Investigate the failing tests and fix the smallest root cause" \
  --provider openai-compatible \
  --base-url https://your-endpoint.example/v1 \
  --model your-model \
  --verify-command "python -m pytest -q"
```

The adapter uses `/chat/completions` and non-streaming HTTP in v0.3. The event protocol
already separates Model Attempts, so streaming, retry and provider fallback can be
added without changing Step semantics.

## Programmatic use

```python
from pathlib import Path

from traceh.llm.scripted import ScriptedLlmProvider
from traceh.api.llm import ModelResponse
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime

provider = ScriptedLlmProvider((ModelResponse(content="done"),))
runtime = build_default_runtime(
    RuntimeConfig(
        data_dir=Path(".traceh"),
        provider="scripted",
        model="demo",
    ),
    provider=provider,
)

result = await runtime.run(Path("./workspace"), "Inspect the project")
await runtime.dispose()
```

`build_default_runtime()` also accepts a custom `EventStore`, `ContinuationRuntime`,
additional `Tool` objects, policies, prompt assembler and verifier. For deeper changes,
provide another `CompositionRuntime` to the loop so a Step leases one coherent provider,
tool set and snapshot generation.

## Architecture

```text
CLI / SDK / Evaluator
        |
   AgentRuntime
        |
    AgentLoop                 <- minimal control flow
   /    |     \
Prompt  LLM   ToolRuntime     <- replaceable capabilities
   \    |     /
 RequestBuilder
        |
Session Stream + Effect Stream
        |
Projectors / Recovery / Inspector / Invariants
```

The control loop does not know how tools are implemented, how providers call a model,
how a verifier checks success, or how future plugins are discovered. It only coordinates
stable services.

### Source of truth

- Durable facts: Session Stream.
- External side-effect facts: Effect Stream.
- Runtime state: a projection derived from events.
- Model-visible history: a Surface projection derived from events.
- Model request: a function of Surface + frozen Composition Snapshot.

### Extension seams already present

| Future feature | Existing seam |
|---|---|
| New model provider | `LlmProvider` + `LlmRegistry` |
| New tool | `Tool` + `ToolRegistry` |
| Tool authorization | `ToolPolicy` |
| Prompt extension | `PromptSection` + `PromptAssembler` |
| Custom completion behavior | `ContinuationRuntime` |
| Custom verification | `CompletionVerifier` |
| New persistence backend | `EventStore` |
| Observability plugin | typed NOTIFY hooks |
| Reversible plugin setup | `Activation`, `Lifespan`, `OwnedTaskSet` |
| Step-safe plugin/model/tool generations | `CompositionRuntime.lease()` |
| Agent-specific capabilities | hierarchical `Scope` |
| Subagents | future `AgentSupervisor` built above `AgentLoop` |
| Multi-agent workflows | future workflow layer calling `AgentSupervisor` |

See [docs/architecture.md](docs/architecture.md) and
[docs/plugin-evolution.md](docs/plugin-evolution.md).

## Important design choices

### Requests are reconstructable

Before a provider call, TraceHarness persists:

- the effective provider and model;
- the assembled system prompt;
- the exact visible tool schemas;
- the Composition revision;
- the Surface boundary sequence;
- the complete request snapshot and fingerprint.

`traceh replay` rebuilds the request from earlier durable events and reports any
fingerprint mismatch.

### Side effects are not blindly retried

The Tool Runtime writes `effect/intent` before dispatch and `effect/outcome` after the
operation. If the process dies in between, recovery marks the operation
`unknown_after_crash` unless durable evidence proves an outcome. It does not repeat a
write or process merely because `tool/result` is missing.

### Completion requires evidence

With `--verify-command`, a final model response is checked against the real workspace.
A failed verifier is fed back into the next Step, subject to the configured retry and
Step budgets.

## Project layout

```text
src/traceh/api          stable protocols and immutable DTOs
src/traceh/kernel       scope, activation, hooks, reversible lifetime
src/traceh/session      event stores, projectors, recovery, invariants
src/traceh/runtime      AgentLoop, Composition leases, requests, continuation, verification
src/traceh/llm          provider registry and adapters
src/traceh/tools        policies, scheduling, effects and built-in coding tools
src/traceh/inspector    text replay and static HTML trace
src/traceh/evaluation   deterministic benchmark runner
tests                   contract, recovery, cancellation and E2E tests
```

## CLI

```text
traceh run
traceh resume
traceh recover
traceh inspect
traceh replay
traceh compact
traceh sessions
traceh eval
traceh doctor
```

Use `traceh <command> --help` for details.

## Known v0.3 limits

- Plugin entry-point discovery and hot replacement are intentionally deferred to v0.4+.
  The kernel activation/scope primitives are present, but there is no third-party Plugin
  Manager yet.
- There is one live Agent Runtime per process and no `AgentSupervisor` yet.
- JSONL provides one-writer optimistic concurrency but is not a distributed database.
- The OpenAI-compatible adapter is non-streaming and has no retry/fallback middleware.
- `apply_patch` performs exact text replacement rather than parsing unified diffs.
- The default shell policy is a guardrail, not a security sandbox. Run untrusted agents
  in a container or remote sandbox.
- Effect reconciliation is generic. Domain-specific reconcilers for Git, remote APIs and
  transactional systems belong in later plugins.

## Development

```bash
PYTHONPATH=src pytest
PYTHONPATH=src python -m compileall -q src
```

The test suite covers JSONL recovery, expected-sequence conflicts, scopes, reversible
activation, hook semantics, Surface replacement, workspace confinement, exact patches,
request reconstruction, end-to-end coding, cancellation, crash recovery and benchmark
reporting.

## Design documents

- [Architecture](docs/architecture.md)
- [Event protocol](docs/event-protocol.md)
- [Recovery semantics](docs/recovery-semantics.md)
- [Plugin and multi-agent evolution](docs/plugin-evolution.md)
- [Testing strategy](docs/testing.md)
- [ADRs](docs/adr/)

## Attribution

This project is an independent Python implementation inspired by ideas found in
DeepSeek Harness, including append-only sessions, reconstructable requests, capability
seams, scoped lifetimes and executable invariants. It does not copy or promise API
compatibility with DeepSeek Harness and is not an official DeepSeek project.

## License

MIT.
