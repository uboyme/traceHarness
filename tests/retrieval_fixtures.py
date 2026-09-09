"""Explicit F2 test policy and real runtime assembly."""

import unicodedata
from dataclasses import replace

from plugin_fixtures import ScriptedPlugin, manifest
from skill_fixtures import contribution, discovery, policy

from traceh.api.llm import ModelResponse
from traceh.api.retrieval import ReferenceRetrievalPolicy
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.session.context_input import ContextInputPolicy
from traceh.session.sqlite import SqliteEventStore


def retrieval_policy(**kwargs):
    return ReferenceRetrievalPolicy(
        **{
            "unicode_version": unicodedata.unidata_version,
            "default_tier": "summary",
            "match_fields": ("id", "tag", "path", "symbol", "error"),
            "k1": 1.2,
            "b": 0.75,
            "rrf_constant": 60,
            "exact_weight": 2,
            "fts_weight": 1,
            "context_bytes": 20000,
            "max_catalog_bytes": 20000,
            "max_terms": 100,
            "max_corpus_items": 40,
            "max_corpus_bytes": 40000,
            "max_candidates": 20,
            "max_requests": 8,
            **kwargs,
        }
    )


def context_policy(**kwargs):
    return ContextInputPolicy(
        **{
            "history_tier": None,
            "total_bytes": 30000,
            "history_bytes": 0,
            "item_bytes": 12000,
            "max_blocks": 20,
            "max_exclusions": 40,
            "max_query_bytes": 4000,
            "skills": retrieval_policy(),
            **kwargs,
        }
    )


def sample_skills():
    first = contribution("reference.author", "boundary.notes", "SECTION ORIGINAL 界限与职责")
    first = replace(
        first,
        descriptor=replace(
            first.descriptor,
            title="结构边界",
            summary="模块边界和所有权的工作方法",
            tags=("architecture",),
        ),
    )
    second = contribution("reference.author", "orbital.notes", "UNSELECTED PRIVATE BODY")
    second = replace(
        second,
        descriptor=replace(
            second.descriptor,
            title="轨道计算",
            summary="UNSELECTED SUMMARY planetary orbit calculation",
            tags=("orbital",),
        ),
    )
    return first, second


async def build_case(
    tmp_path,
    *,
    provider=None,
    context=None,
    values=None,
    activation_policy=None,
    config_changes=None,
    runtime_options=None,
):
    values = values if values is not None else sample_skills()
    plugin = ScriptedPlugin(manifest("reference.author"), skills=values)
    store = SqliteEventStore(tmp_path / "events")
    provider = provider or ScriptedLlmProvider((ModelResponse(content="answer"),), repeat_last=True)
    runtime = await build_default_runtime_async(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            context_input=context or context_policy(),
            skill_policy=activation_policy or policy(),
            **(config_changes or {}),
        ),
        provider=provider,
        event_store=store,
        enabled_plugins=("reference.author",),
        plugin_discovery=discovery(plugin),
        **(runtime_options or {}),
    )
    session = await runtime.create_session(tmp_path)
    return runtime, store, provider, session, values


async def select(runtime, session, *values, operation="select", head=0):
    return await runtime.skill_context.select(
        session,
        operation_id=operation,
        expected_head=head,
        actor_id="host-user",
        skills=tuple(
            {"skill_id": v.descriptor.skill_id, "version": v.descriptor.version}
            for v in sorted(values, key=lambda item: item.descriptor.skill_id)
        ),
    )
