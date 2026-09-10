"""Bounded analysis/review calls assembled from the original Runtime and Budget."""

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from traceh.agents import AgentRegistrar
from traceh.api.agents import AgentSpec
from traceh.api.budgets import BudgetLimits
from traceh.api.json_types import fingerprint
from traceh.api.prompts import PromptSection
from traceh.api.turns import TurnInput
from traceh.budgets import BudgetEnforcement, BudgetLedgerService
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.evaluation.model_evidence import observe_model_call
from traceh.evaluation.variant_execution import write_json
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.continuation import Finish
from traceh.runtime.prompt import PromptAssembler
from traceh.session.sqlite import SqliteEventStore
from traceh.supervision.execution import AgentRuntimeExecution


@dataclass(frozen=True)
class ModelCallConfig:
    provider: str
    model: str
    temperature: float
    encoding: str
    token_limit: int
    output_tokens: int
    safety_tokens: int
    timeout_seconds: int
    connection_digest: str

    def __post_init__(self):
        TokenBudgetPolicy(self.encoding, self.token_limit, self.output_tokens, self.safety_tokens)
        if (
            type(self.timeout_seconds) is not int
            or self.timeout_seconds < 1
            or not self.provider
            or not self.model
            or not self.connection_digest
        ):
            raise ValueError("evaluation-model-config-invalid")


def error_codes(error):
    if error is None:
        return []
    if isinstance(error, BaseExceptionGroup):
        return [c for e in error.exceptions for c in error_codes(e)]
    return [getattr(error, "code", type(error).__name__)]


class _SingleResponse:
    async def decide(self, *, response, **context):
        # Existing continuation seam: one response is the whole control task.
        return Finish("unexpected-tool-call" if response.tool_calls else "completed")


async def run_model_call(*, provider, config, system, input_text, binding, output_dir):
    """One no-Tool Step; failed/cancelled calls also retain original evidence."""
    if provider.name != config.provider or not system or not input_text:
        raise ValueError("evaluation-model-binding-invalid")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)  # noqa: ASYNC240 - exclusive admission before first await
    workspace = root / "workspace"
    workspace.mkdir()
    session_id, agent_id = str(uuid4()), str(uuid4())
    definition = {
        "format": 1,
        "config": asdict(config),
        "system": system,
        "input": input_text,
        "binding": binding,
        "session_id": session_id,
        "agent_id": agent_id,
    }
    write_json(root / "call.json", definition)
    store = SqliteEventStore(root / "data")
    runtime, primary = None, None
    budgets = BudgetLedgerService(store)
    try:
        enforcement = BudgetEnforcement(
            budgets,
            agent_id=agent_id,
            session_id=session_id,
            continuation=_SingleResponse(),
        )
        runtime = await build_default_runtime_async(
            RuntimeConfig(
                data_dir=root / "data",
                provider=config.provider,
                model=config.model,
                max_steps=1,
                temperature=config.temperature,
                max_output_tokens=config.output_tokens,
                token_budget=TokenBudgetPolicy(
                    config.encoding,
                    config.token_limit,
                    config.output_tokens,
                    config.safety_tokens,
                ),
                model_retry_policy=NO_MODEL_RETRY,
            ),
            provider=provider,
            event_store=store,
            include_default_tools=False,
            prompt=PromptAssembler((PromptSection("traceh.evaluation.control-model", system),)),
            policies=(),
            continuation=enforcement.continuation,
            llm_runtime=enforcement.llm_runtime,
            tool_admission_gate=enforcement.tool_admission_gate,
        )
        await runtime.create_session(
            workspace, session_id=session_id, metadata={"control_call": fingerprint(definition)}
        )
        await AgentRegistrar(store).create_agent(
            AgentSpec(preset="evaluation-control-model", workspace_id=str(workspace.resolve())),
            request_id=str(uuid4()),
            agent_id=agent_id,
            session_id=session_id,
        )
        await budgets.grant_root(
            operation_id=str(uuid4()),
            agent_id=agent_id,
            limits=BudgetLimits(config.token_limit, 1, 0, config.timeout_seconds * 1000, 0, 0, 0),
        )
        execution = enforcement.wrap(AgentRuntimeExecution(runtime, session_id))
        worker = asyncio.create_task(
            execution.run_turn(TurnInput(input_text, str(uuid4()), "evaluation-control"))
        )
        try:
            await asyncio.shield(worker)
        except BaseException as error:
            if not worker.done():
                worker.cancel()
                await await_worker_convergence(worker)
                if not worker.cancelled() and worker.exception() is not None:
                    raise combine_failures(
                        error, worker.exception(), "control model and convergence failed"
                    ) from None
            raise
    except BaseException as error:
        primary = error

    async def close():
        error = None
        try:
            if runtime is not None:
                await runtime.dispose()
        except BaseException as failure:
            error = failure
        try:
            if (await budgets.ledger()).account(agent_id) is not None:
                await budgets.close_account(operation_id=str(uuid4()), agent_id=agent_id)
        except BaseException as failure:
            error = combine_failures(error, failure, "Runtime and Budget cleanup failed")
        try:
            await store.aclose()
        except BaseException as failure:
            error = combine_failures(error, failure, "Runtime and store cleanup failed")
        if error is not None:
            raise error

    closing = asyncio.create_task(close())
    try:
        await asyncio.shield(closing)
    except BaseException as error:
        if primary is None:
            primary = error
        await await_worker_convergence(closing)
    cleanup = None if closing.cancelled() else closing.exception()
    failure = combine_failures(primary, cleanup, "model execution and cleanup failed")
    try:
        receipt = {
            "observation": observe_model_call(root),
            "converged": cleanup is None,
            "errors": error_codes(failure),
        }
        write_json(root / "result.json", receipt)
    except BaseException as error:
        raise combine_failures(
            failure, error, "model execution and evidence publication failed"
        ) from None
    if failure is not None:
        raise failure
    return receipt
