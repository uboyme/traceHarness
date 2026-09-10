"""Typed AO services on the real Plugin/Generation lifecycle, no model API.

The analysis implementation here is explicitly a scripted contract fixture;
it proves borrowing/cancellation, not the future AO-2 provider adapter.
"""

import asyncio
from dataclasses import replace

import pytest
from optimization_fixtures import example
from plugin_fixtures import entry_point_for, manifest, provider_for

from traceh.api.json_types import fingerprint
from traceh.api.llm import Usage, UsageQuality
from traceh.api.optimization import (
    OPTIMIZATION_ANALYSIS,
    OPTIMIZATION_STRATEGY,
    NoCandidate,
    OptimizationAnalysisResult,
)
from traceh.concurrency import await_worker_convergence
from traceh.evolution.optimization_contract import admit_proposal, validate_request
from traceh.kernel.registry import ServiceNotFoundError
from traceh.kernel.scope import ScopedServiceBinding, ScopeKind
from traceh.plugins import PluginActivationError, PluginDiscovery
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.session.event_store import InMemoryEventStore


class ScriptedAnalysis:
    def __init__(self, *, wait=False, error=None):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        if not wait:
            self.finish.set()
        self.cleanup_started = asyncio.Event()
        self.cleanup_gate = asyncio.Event()
        if not wait:
            self.cleanup_gate.set()
        self.closed_calls = 0
        self.disposed = False
        self.error = error
        self.requests = []

    async def dispose(self):
        self.disposed = True

    async def analyze(self, request):
        assert not self.disposed
        self.requests.append(request.digest)
        self.started.set()
        try:
            await self.finish.wait()
            if self.error:
                raise self.error
            return OptimizationAnalysisResult(
                request.digest,
                "fixture-analysis-session",
                "fixture-analysis-turn",
                fingerprint("fixture analysis evidence"),
                "Fixture: no defensible candidate.",
                Usage(5, 6, UsageQuality.EXACT),
            )
        finally:

            async def cleanup():
                self.cleanup_started.set()
                await self.cleanup_gate.wait()
                self.closed_calls += 1

            closing = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await await_worker_convergence(closing)
                raise


class StrategyService:
    def __init__(self, analysis, proposal):
        self.analysis = analysis
        self.proposal = proposal
        self.closed = False

    async def propose(self, request):
        assert not self.closed
        if self.proposal is not None:
            return replace(self.proposal, request_digest=request.digest)
        result = await self.analysis.analyze(request)
        assert result.request_digest == request.digest
        return NoCandidate(request.digest, result.text)


class StrategyPlugin:
    def __init__(self, *, version="1.0.0", proposal=None, fail_setup=False, block_setup=False):
        self.manifest = manifest("fixture.strategy", version=version)
        self.proposal = proposal
        self.fail_setup = fail_setup
        self.block_setup = block_setup
        self.entered = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_gate = asyncio.Event()
        if not block_setup:
            self.cleanup_gate.set()
        self.cleaned = 0
        self.service = None

    async def setup(self, context, config):
        del config
        analysis = context.require(OPTIMIZATION_ANALYSIS)
        self.service = StrategyService(analysis, self.proposal)
        await context.provide(OPTIMIZATION_STRATEGY, self.service)

        async def cleanup():
            self.cleanup_started.set()
            await self.cleanup_gate.wait()
            self.service.closed = True
            self.cleaned += 1

        context.add_cleanup(cleanup)
        self.entered.set()
        if self.block_setup:
            await asyncio.Event().wait()
        if self.fail_setup:
            raise RuntimeError("explicit setup failure after registration")


def discovery(plugin):
    return PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))


async def build(tmp_path, analysis, plugin=None):
    return await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=InMemoryEventStore(),
        enabled_plugins=() if plugin is None else (plugin.manifest.plugin_id,),
        plugin_discovery=None if plugin is None else discovery(plugin),
        service_bindings=(
            ScopedServiceBinding(ScopeKind.APPLICATION, OPTIMIZATION_ANALYSIS, analysis),
        ),
    )


def lease(runtime, tmp_path):
    return runtime.loop.compositions.lease(
        workspace=tmp_path,
        session_id="fixture-session",
        turn_id="fixture-turn",
        step_id="fixture-step",
    )


async def test_plugin_proposal_is_admitted_through_original_ue3_owner(tmp_path):
    files, contract, request, proposal, *_ = example()
    analysis = ScriptedAnalysis()
    plugin = StrategyPlugin(proposal=proposal)
    runtime = await build(tmp_path, analysis, plugin)
    try:
        async with lease(runtime, tmp_path) as current:
            validate_request(contract, request, files)
            response = await current.services.require(OPTIMIZATION_STRATEGY).propose(request)
            accepted = admit_proposal(contract, request, response, files, seen_candidate_digests=())
            assert accepted.source_digest != contract.base_source_digest
            assert not plugin.service.closed
    finally:
        await runtime.dispose()
    assert plugin.cleaned == 1
    assert not analysis.disposed
    assert analysis.requests == []  # An explicit manual proposal needs no analysis call.


async def test_old_strategy_remains_borrowed_until_inflight_analysis_converges(tmp_path):
    request = example()[2]
    analysis = ScriptedAnalysis(wait=True)
    first, second = StrategyPlugin(), StrategyPlugin(version="2.0.0")
    runtime = await build(tmp_path, analysis, first)
    try:
        async with lease(runtime, tmp_path) as old:
            task = asyncio.create_task(old.services.require(OPTIMIZATION_STRATEGY).propose(request))
            await asyncio.wait_for(analysis.started.wait(), 5)
            await runtime.replace_plugin_composition(
                (second.manifest.plugin_id,), plugin_discovery=discovery(second)
            )
            assert runtime.services.require(OPTIMIZATION_STRATEGY) is second.service
            assert old.services.require(OPTIMIZATION_STRATEGY) is first.service
            assert first.cleaned == 0
            analysis.finish.set()
            await asyncio.wait_for(analysis.cleanup_started.wait(), 5)
            assert not task.done()
            analysis.cleanup_gate.set()
            result = await task
            assert isinstance(result, NoCandidate)
            assert analysis.closed_calls == 1 and first.cleaned == 0
        await runtime.loop.compositions.drain()
        assert first.cleaned == 1 and first.service.closed
    finally:
        analysis.finish.set()
        analysis.cleanup_gate.set()
        await runtime.dispose()
    assert second.cleaned == 1 and not analysis.disposed


async def test_analysis_failure_propagates_without_proposal_or_borrowed_disposal(tmp_path):
    error = RuntimeError("explicit analysis failure")
    analysis = ScriptedAnalysis(error=error)
    plugin = StrategyPlugin()
    runtime = await build(tmp_path, analysis, plugin)
    try:
        async with lease(runtime, tmp_path) as current:
            with pytest.raises(RuntimeError) as caught:
                await current.services.require(OPTIMIZATION_STRATEGY).propose(example()[2])
            assert caught.value is error
            assert analysis.closed_calls == 1
    finally:
        await runtime.dispose()
    assert plugin.cleaned == 1 and not analysis.disposed


async def test_repeated_cancel_does_not_release_lease_before_analysis_closes(tmp_path):
    analysis = ScriptedAnalysis(wait=True)
    plugin = StrategyPlugin()
    runtime = await build(tmp_path, analysis, plugin)
    exited = asyncio.Event()

    async def call():
        async with lease(runtime, tmp_path) as current:
            try:
                return await current.services.require(OPTIMIZATION_STRATEGY).propose(example()[2])
            finally:
                exited.set()

    task = asyncio.create_task(call())
    try:
        await asyncio.wait_for(analysis.started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(analysis.cleanup_started.wait(), 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)  # Yield cancellation delivery; Event owns the actual ordering.
            assert not task.done() and not exited.is_set()
        analysis.cleanup_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert analysis.closed_calls == 1 and exited.is_set()
    finally:
        analysis.finish.set()
        analysis.cleanup_gate.set()
        await runtime.dispose()
    assert plugin.cleaned == 1 and not analysis.disposed


@pytest.mark.parametrize("cancel", [False, True])
async def test_setup_failure_or_repeated_cancel_rolls_back_strategy_registration(tmp_path, cancel):
    analysis = ScriptedAnalysis()
    runtime = await build(tmp_path, analysis)
    plugin = StrategyPlugin(fail_setup=not cancel, block_setup=cancel)
    before = runtime.loop.compositions.current_generation
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            (plugin.manifest.plugin_id,),
            plugin_discovery=discovery(plugin),
        )
    )
    try:
        await asyncio.wait_for(plugin.entered.wait(), 5)
        if cancel:
            replacement.cancel()
            await asyncio.wait_for(plugin.cleanup_started.wait(), 5)
            for _ in range(3):
                replacement.cancel()
                await asyncio.sleep(0)
                assert not replacement.done()
            plugin.cleanup_gate.set()
        with pytest.raises(asyncio.CancelledError if cancel else PluginActivationError):
            await replacement
        assert plugin.cleaned == 1 and plugin.service.closed
        assert runtime.loop.compositions.current_generation is before
        with pytest.raises(ServiceNotFoundError):
            runtime.services.require(OPTIMIZATION_STRATEGY)
        assert runtime.services.require(OPTIMIZATION_ANALYSIS) is analysis
    finally:
        plugin.cleanup_gate.set()
        await runtime.dispose()
    assert not analysis.disposed


async def test_no_hidden_analysis_provider_when_service_is_missing(tmp_path):
    plugin = StrategyPlugin()
    with pytest.raises(PluginActivationError):
        await build_default_runtime_async(
            RuntimeConfig(data_dir=tmp_path / "data"),
            event_store=InMemoryEventStore(),
            enabled_plugins=(plugin.manifest.plugin_id,),
            plugin_discovery=discovery(plugin),
        )
    assert plugin.service is None and not plugin.entered.is_set()
