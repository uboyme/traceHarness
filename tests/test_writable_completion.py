"""Completion gates through real Product, Git, Sandbox, Runtime and replay."""

import asyncio
import json
from dataclasses import replace

import pytest
import test_product_f3_e2e as product
from collaboration_fixtures import run_failed_product
from promotion_fixtures import build_source_repository, git, make_bare_target, verification_plan
from test_patch_integration import IntegratingProvider
from test_writable_collaboration import writable_profile

from traceh.api.llm import ToolCall
from traceh.api.product import RequestedTaskMode
from traceh.api.promotion import VerifierCommand
from traceh.artifacts.cas import LocalArtifactCas
from traceh.product.verification_review import REVIEW_GUIDANCE
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService

BROKEN = "def normalize(items):\n    return [str(item) for item in items]"
FIXED = (
    "def normalize(items):\n"
    "    if not isinstance(items, list):\n"
    "        raise ValueError('items must be a list')\n"
    "    return [str(item) for item in items]"
)
CHECK = """from pathlib import Path
assert not Path('diagnostic.py').exists(), 'extra file'
namespace = {}
exec(Path('tracked.txt').read_text(), namespace)
normalize = namespace['normalize']
source = [1, 2]
assert normalize(source) == ['1', '2'] and source == [1, 2]
assert normalize([]) == []
for invalid in (None, {}, (), 'text'):
    try:
        normalize(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('top-level input must be rejected')
assert Path('added.txt').read_text() == 'added\\n'
print('PRIVATE-FIXED-OUTPUT')
"""


def configure(tmp_path, monkeypatch, *, code=CHECK):
    original = product._host_profile
    monkeypatch.setattr(
        product, "_host_profile",
        lambda mode: replace(
            original(mode), profile=writable_profile(mode),
            verification_plan=verification_plan(
                VerifierCommand('delivery-contract', ('python', '-B', '-c', code), 30000,
                                public_requirement='Delivery scope and input behavior.'),
                plan_id='product-plan',
            ),
        ),
    )
    source, _ = build_source_repository(tmp_path / 'source')
    git('config', 'core.autocrlf', 'false', cwd=source)
    (source / 'tracked.txt').write_bytes(b'base\n')
    (source / 'kept.txt').write_bytes(b'kept\n')
    git('add', '--renormalize', '.', cwd=source)
    assert not git('status', '--porcelain', cwd=source)
    return source, make_bare_target(source, tmp_path / 'target.git')


class RepairingProvider(IntegratingProvider):
    def __init__(self, defect):
        super().__init__()
        self.defect = defect
        self.feedback = []

    async def complete(self, request):
        if 'traceh.product.patch-author' in request.system_prompt:
            response = await super().complete(request)
            return replace(response, tool_calls=tuple(
                replace(call, arguments={
                    **call.arguments, 'new_text': FIXED if self.defect == 'scope' else BROKEN,
                }) if call.name == 'apply_patch' else call for call in response.tool_calls
            ))
        calls = {message.tool_call_id for message in request.messages}
        feedback = [
            m.content for m in request.messages if m.role == 'user'
            and m.content.startswith('The external completion verifier failed.')
        ]
        if REVIEW_GUIDANCE in request.system_prompt and feedback:
            self.feedback = feedback
            assert 'delivery-contract' in feedback[-1]
            if self.defect == 'ignore':
                return product._response('I still claim completion without repairing anything.')
            repairs = []
            if self.defect != 'scope' and 'repair-input' not in calls:
                repairs.append(ToolCall('repair-input', 'apply_patch', {
                    'path': 'tracked.txt', 'old_text': BROKEN, 'new_text': FIXED,
                }))
            if self.defect in {'scope', 'both'} and 'repair-scope' not in calls:
                repairs.append(ToolCall('repair-scope', 'shell', {
                    'command': (
                        'python -c "from pathlib import Path; Path(\'diagnostic.py\').unlink()"'
                    ),
                }))
            return product._response('Repairing the failed delivery checks.', *repairs)
        if (
            self.defect in {'scope', 'both'} and 'add-main' in calls
            and 'extra-file' not in calls
        ):
            return product._response('', ToolCall('extra-file', 'apply_patch', {
                'path': 'diagnostic.py', 'old_text': '', 'new_text': 'print(1)\n', 'create': True,
            }))
        return await super().complete(request)


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['scope', 'input', 'both', 'ignore'])
async def test_fixed_completion_rejects_repairs_and_stops_at_original_bound(
    tmp_path, monkeypatch, defect,
):
    source, target = configure(tmp_path, monkeypatch)
    store = InMemoryEventStore()
    provider = RepairingProvider(defect)
    if defect == 'ignore':
        await run_failed_product(tmp_path, store, source, target, provider)
    else:
        await product._run_to_barrier(
            tmp_path, store, source, target, LocalArtifactCas(tmp_path / 'cas'),
            RequestedTaskMode.MULTI, provider,
        )
    sessions = SessionService(store)
    verifications = []
    events = []
    for stream in await store.list_streams(prefix='session:'):
        current = await sessions.read_session(stream.removeprefix('session:'))
        events.extend(current)
        verifications.extend(e for e in current if e.type == 'verification/result')
        assert not await product.verify_request_snapshots(
            sessions, product.SurfaceProjector(), stream.removeprefix('session:'),
        )
    assert len(verifications) == 2
    assert [e.data['passed'] for e in verifications] == [False, defect != 'ignore']
    assert len(provider.feedback) == 1
    for event in verifications:
        view = next(e for e in events if e.type == 'request/view'
                    and e.data['step_id'] == event.data['step_id'])
        assert view.data['label'] == 'collaboration-review'
        result = json.loads(event.data['summary'])
        assert result['results'][0]['public_requirement'] == 'Delivery scope and input behavior.'
        assert result['raw_output_availability'] == 'not-retained'
        assert result['failure_cause'] == 'unknown'
        assert result['candidate_unchanged']
        integration, = result['integration']
        assert integration['tool_call_id'] == 'integrate'
        execution = result['results'][0]['execution']
        outcome = next(e for e in events if e.type == 'sandbox/outcome'
                       and e.data['execution_id'] == execution['execution_id'])
        assert outcome.data['converged']
    assert 'PRIVATE-FIXED-OUTPUT' not in json.dumps([e.to_dict() for e in events])
    ledger = await store.read('patch-promotions:ledger')
    assert not any(
        e.type in {'patch/approval-recorded', 'patch/promotion-committed'} for e in ledger
    )
    assert len(ledger) == (0 if defect == 'ignore' else 1)
    assert (source / 'tracked.txt').read_text() == 'base\n'


@pytest.mark.asyncio
async def test_cancel_during_completion_converges_observed_guest(tmp_path, monkeypatch):
    from sandbox_fixtures import real_sandbox_policy, wait_for_guest

    from traceh.sandbox import service as sandbox_module
    from traceh.sandbox.ledger import SandboxEventRecorder

    source, target = configure(
        tmp_path, monkeypatch,
        code="from pathlib import Path; import time; "
        "Path('completion-entered').write_text('flushed'); time.sleep(60)",
    )
    store = InMemoryEventStore()
    admitted, converging = asyncio.Event(), asyncio.Event()
    requests = []
    original = SandboxEventRecorder.__call__
    converge = sandbox_module.await_worker_convergence

    async def record(recorder, event_type, data):
        await original(recorder, event_type, data)
        if event_type == 'sandbox/request' and recorder.owner.kind == 'verification':
            requests.append(recorder.stream_id)
            admitted.set()

    async def observed_convergence(task):
        converging.set()
        return await converge(task)

    monkeypatch.setattr(SandboxEventRecorder, '__call__', record)
    monkeypatch.setattr(sandbox_module, 'await_worker_convergence', observed_convergence)
    actions = product.ProductTurnActions()
    host = await product._build_host(
        tmp_path, store, source, target, LocalArtifactCas(tmp_path / 'cas'), actions,
        RequestedTaskMode.MULTI, RepairingProvider('input'),
    )
    workspace = tmp_path / 'chat-workspace'
    workspace.mkdir()
    console = product._Console(('please add the accepted file', 'yes, do it', 'START'))
    running = asyncio.create_task(product.run_chat(
        product._chat_runtime(tmp_path, store, actions),
        console.console,
        workspace=workspace, timeline=False, product=host,
    ))
    observed = asyncio.create_task(admitted.wait())
    try:
        # This wait includes the entire real Product setup and child/integration
        # journey. The production turn still has its unchanged 60-second cap.
        await asyncio.wait((observed, running), timeout=80, return_when=asyncio.FIRST_COMPLETED)
        if not admitted.is_set():
            tail = [
                (e.type, e.data.get('tool_name'), e.data.get('status'), e.data.get('message'))
                for stream in await store.list_streams(prefix='session:')
                for e in await store.read(stream)
                if e.type in {'turn/error', 'tool/result', 'sandbox/request', 'sandbox/outcome'}
            ]
            pytest.fail(repr(tail[-5:]))
        await wait_for_guest(store, requests[0], real_sandbox_policy(), running)
        converging.clear()
        running.cancel()
        await asyncio.wait_for(converging.wait(), 10)
        running.cancel()
        assert await asyncio.wait_for(asyncio.shield(running), 30) == 130
    finally:
        observed.cancel()
        await asyncio.gather(observed, return_exceptions=True)
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    events = [
        e for stream in await store.list_streams(prefix='session:')
        for e in await store.read(stream)
    ]
    outcomes = [e for e in events if e.type == 'sandbox/outcome']
    assert len(outcomes) == 1
    assert outcomes[0].data['status'] == 'cancelled' and outcomes[0].data['converged']
    assert not any(e.type == 'verification/result' for e in events)
    assert not await store.read('patch-promotions:ledger')
    # Ctrl-C converges execution but does not invent terminal Product cancellation.
    # The original explicit cancel operation owns resource release/account closure.
    task_id = product._proposed_task_id(console.output)
    reopened = await product._build_host(
        tmp_path, store, source, target, LocalArtifactCas(tmp_path / 'cas'),
        product.ProductTurnActions(), RequestedTaskMode.MULTI, RepairingProvider('input'),
    )
    try:
        result = await reopened.control.cancel(task_id)
        assert result.summary.status.value == 'cancelled'
    finally:
        await reopened.aclose()
    ledger = await product.BudgetLedgerReader(store).load()
    assert all(account.status.value == 'closed' for account in ledger.accounts)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stream', 'session', 'turn', 'step', 'owner'])
async def test_completion_runner_rejects_unbound_owner_before_execution(tmp_path, change):
    from traceh.api.sandbox import SandboxOwner
    from traceh.promotion.verification import HostVerificationRunner

    owner = SandboxOwner('verification', 'step', 'session', 'turn', 'step')
    stream_id = 'session:session'
    if change == 'stream':
        stream_id = 'session:another'
    else:
        owner = replace(owner, **{
            {'session': 'session_id', 'turn': 'turn_id', 'step': 'step_id', 'owner': 'owner_id'}[
                change
            ]: 'another' if change in {'session', 'owner'} else None,
        })
    with pytest.raises(ValueError, match='promotion-verifier-owner-invalid'):
        await HostVerificationRunner().run(
            verification_plan(VerifierCommand('check', ('python', '-c', 'pass'), 1000)),
            cwd=tmp_path, owner=owner, stream_id=stream_id,
        )


@pytest.mark.asyncio
async def test_completion_never_labels_another_commands_result(tmp_path, monkeypatch):
    from traceh.promotion.models import verification_evidence_digest
    from traceh.promotion.verification import HostVerificationRunner

    source, target = configure(tmp_path, monkeypatch, code='assert True')
    store = InMemoryEventStore()
    original = HostVerificationRunner.run
    observed = []

    async def substituted(runner, plan, **kwargs):
        evidence = await original(runner, plan, **kwargs)
        observed.append(evidence)
        results = (replace(evidence.results[0], argv_digest='a'*64),)
        return replace(evidence, results=results,
                       evidence_digest=verification_evidence_digest(
                           evidence.definition_digest, results))

    monkeypatch.setattr(HostVerificationRunner, 'run', substituted)
    await run_failed_product(tmp_path, store, source, target, RepairingProvider('input'))
    assert len(observed) == 1 and observed[0].passed
    sessions = SessionService(store)
    events = [e for sid in await sessions.list_sessions()
              for e in await sessions.read_session(sid)]
    assert not any(e.type == 'verification/result' for e in events)
    assert not await store.read('patch-promotions:ledger')
