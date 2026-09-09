"""RE material/transport/setup checks stay offline and exercise the original owners."""

import json
import urllib.request
from pathlib import Path

import pytest
from live_active_retrieval.grid import prepare_runtime, setup_source
from live_active_retrieval.reliability import DirectTransport, connection_identity
from live_active_retrieval.reliability_fixtures import frozen_materials

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider


def manifest():
    return json.loads(Path("tests/live_active_retrieval/manifest.json").read_text(encoding="utf-8"))


def test_materials_are_frozen_distinct_and_hidden_answers_do_not_enter_questions():
    first = frozen_materials(manifest())
    assert first == frozen_materials(manifest())
    assert len(first["holdout"]) == 24
    development = [f for group in first["development"].values() for f in group] + first["controls"]
    assert len(development) == 28
    assert not {f["question"] for f in development} & {f["question"] for f in first["holdout"]}
    for f in development + first["holdout"]:
        if f["grading"] == "value":
            assert f["value"] not in f["question"]
            assert f["value"] in f["source"]


def test_direct_opener_has_no_proxy_and_restores_after_failure():
    before = urllib.request._opener
    with pytest.raises(RuntimeError, match="intentional"):
        with DirectTransport() as transport:
            assert urllib.request._opener is transport.opener
            assert not any(
                isinstance(h, urllib.request.ProxyHandler) for h in transport.opener.handlers
            )
            transport._audit("socket.connect", (object(), ("127.0.0.1", 9443)))
            assert transport.connections == [{"loopback": True, "port": 9443}]
            raise RuntimeError("intentional")
    assert urllib.request._opener is before
    assert not transport.active


async def test_unbound_memory_fixture_uses_real_runtime_without_creating_binding(tmp_path):
    frozen = {"manifest": manifest()}
    fixture = next(
        f
        for f in frozen_materials(frozen["manifest"])["development"]["re2"]
        if f["mode"] == "unbound"
    )
    provider = ScriptedLlmProvider((ModelResponse(content="No project bound."),))
    runtime, store, session, scope, value = await prepare_runtime(
        tmp_path, fixture, frozen, provider, "offline-fixture"
    )
    try:
        await setup_source(runtime, session, scope, value, fixture, frozen["manifest"])
        assert not scope.setup
        events = await store.read("session:" + session)
        assert not any(e.type.startswith("project/") for e in events)
        result = await runtime.run_existing(session, fixture["question"])
        assert result.final_text == "No project bound."
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()
        await store.aclose()


def test_credential_url_not_accepted_as_frozen_identity():
    class Provider:
        name = "fixture"
        base_url = "https://name:secret@example.invalid/api"

    with pytest.raises(ValueError, match="credentials"):
        connection_identity(Provider(), "fixture-model")
