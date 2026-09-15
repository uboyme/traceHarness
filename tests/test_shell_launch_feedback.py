"""Launch diagnostics reach the next model request through original evidence."""

import base64
import shlex
from dataclasses import replace

import pytest
from test_sandbox_docker import settings as settings

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.sandbox import SandboxConfiguration
from traceh.artifacts.cas import LocalArtifactCas
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.sandbox.reader import read_execution
from traceh.session.event_store import InMemoryEventStore
from traceh.session.surface import SurfaceProjector


@pytest.mark.parametrize(
    ('command', 'error'),
    [('cd . && python --version', 'FileNotFoundError'),
     ('missing-test-executable --version', 'FileNotFoundError'),
     ('./not-executable', 'PermissionError'),
     ('x' * 180, 'FileNotFoundError')],
)
async def test_launch_failure_and_corrected_command_have_replayable_evidence(
    tmp_path, settings, command, error,
):
    workspace = tmp_path / 'workspace'
    settings = replace(settings, limits=replace(settings.limits, output_bytes=128))
    workspace.mkdir()
    (workspace / 'not-executable').write_text('not an executable')
    provider = ScriptedLlmProvider((
        ModelResponse(tool_calls=(ToolCall('bad', 'shell', {'command': command}),)),
        ModelResponse(tool_calls=(ToolCall('good', 'shell', {'command': shlex.join((
            'python', '-c',
            "from pathlib import Path; Path('proof').write_text('ran'); print('ran')",
        ))}),)),
        ModelResponse(content='done'),
    ))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path/'data', model='test-model',
                      sandbox=SandboxConfiguration(settings, tmp_path/'cas')),
        event_store=InMemoryEventStore(), provider=provider,
    )
    try:
        sid = await runtime.create_session(workspace)
        result = await runtime.run_existing(sid, 'Run a command and correct a launch error')
        assert result.reason == 'completed'
        assert (workspace/'proof').read_text() == 'ran'
        events = await runtime.sessions.read_session(sid)
        bad = next(e for e in events if e.type == 'tool/result' and e.data['tool_call_id'] == 'bad')
        assert bad.data['status'] == 'failed'
        assert error in bad.data['content'] and 'start-failed' in bad.data['content']
        next_request = next(e for e in events if e.type == 'request/snapshot' and e.seq > bad.seq)
        request = next_request.data['dispatch_request']
        assert any(error in m['content'] for m in request['messages'])
        tool = next(t for t in request['tools'] if t['name'] == 'shell')
        assert 'working directory is already the workspace' in tool['description']
        description = tool['input_schema']['properties']['command']['description']
        assert 'shell operators are literal' in description
        effects = await runtime.sessions.read_effects(sid)
        failed_request = next(
            e for e in effects
            if e.type == 'sandbox/request' and e.data['owner']['tool_call_id'] == 'bad'
        )
        view = await read_execution(runtime.sessions.store, LocalArtifactCas(tmp_path/'cas'),
                                    stream_id=failed_request.stream_id,
                                    execution_id=failed_request.data['execution_id'])
        assert view.outcome['status'] == 'start-failed' and view.outcome['converged']
        assert view.publication is None
        stderr = base64.b64decode(view.result['payload']['stderr'])
        assert error.encode() in stderr
        assert len(stderr) <= settings.limits.output_bytes
        if len(command) > settings.limits.output_bytes:
            assert len(stderr) == settings.limits.output_bytes
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, SurfaceProjector(), sid)
    finally:
        await runtime.dispose()
