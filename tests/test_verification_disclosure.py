"""Host-declared checks disclose requirements, never guessed causes or raw output."""

import json
from dataclasses import replace

import pytest
from promotion_fixtures import verification_plan
from sandbox_fixtures import real_sandbox_service
from test_product_config import _configuration, _write

from traceh.api.promotion import VerifierCommand
from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.product.config import load_product_host_file
from traceh.product.errors import ProductInputError
from traceh.promotion import PromotionInputError, freeze_verification_plan
from traceh.promotion.models import verifier_command_digest, verifier_definition_digest
from traceh.promotion.verification import HostVerificationRunner
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


@pytest.mark.parametrize('value', ['', ' ', 'x'*1001, 'line\nbreak', 1, True, {}])
def test_public_requirement_is_bounded_explicit_host_text(value):
    with pytest.raises(PromotionInputError, match='public_requirement'):
        freeze_verification_plan(verification_plan(
            VerifierCommand('check', ('python', '-c', 'pass'), 1000, value)
        ))


def test_disclosure_changes_frozen_identity_and_old_plans_are_rejected():
    command = VerifierCommand('check', ('python', '-c', 'pass'), 1000)
    public = replace(command, public_requirement='Output must preserve ordering.')
    assert verifier_command_digest(command) != verifier_command_digest(public)
    assert verifier_definition_digest(verification_plan(command)) != verifier_definition_digest(
        verification_plan(public)
    )
    with pytest.raises(PromotionInputError):
        freeze_verification_plan(replace(verification_plan(command), protocol_version=2))


@pytest.mark.parametrize('mode', ['public', 'private', 'missing', 'old'])
def test_host_configuration_requires_explicit_disclosure(tmp_path, mode):
    config = _configuration(tmp_path)
    plan = config['verification']
    command = plan['commands'][0]
    command['public_requirement'] = 'Required files must exist.' if mode == 'public' else None
    if mode == 'missing':
        del command['public_requirement']
    elif mode == 'old':
        plan['protocol_version'] = 2
    if mode in {'missing', 'old'}:
        with pytest.raises(ProductInputError):
            load_product_host_file(_write(tmp_path, config))
    else:
        loaded = load_product_host_file(_write(tmp_path, config))
        assert loaded.host_profile.verification_plan.commands[0].public_requirement == command[
            'public_requirement'
        ]


@pytest.mark.asyncio
async def test_independent_domains_and_private_check_keep_real_output_private(tmp_path):
    store = InMemoryEventStore()
    sessions = SessionService(store)
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    sid = await sessions.create_session(workspace)
    sandbox = real_sandbox_service(store, LocalArtifactCas(tmp_path/'cas'))
    commands = (
        VerifierCommand('arithmetic', ('python', '-c', 'assert sum([2,3]) == 5'), 10000,
                        'The declared sum must equal five.'),
        VerifierCommand('configuration', ('python', '-c',
                         'import json; assert "enabled" in json.loads("{}")'), 10000,
                        'The configuration must contain its required field.'),
        VerifierCommand('opaque', ('python', '-c',
                         'import sys; print("PRIVATE-CHECK-DATA",file=sys.stderr); sys.exit(1)'),
                        10000),
        VerifierCommand('missing-executable', ('missing-verifier-executable',), 10000,
                        'This check needs its configured executable.'),
    )
    evidence = await HostVerificationRunner(sandbox).run(
        verification_plan(*commands), cwd=workspace,
        owner=SandboxOwner('verification', 'step', sid, 'turn', 'step'),
        stream_id=sessions.session_stream(sid),
    )
    assert [r.status for r in evidence.results] == ['passed', 'failed', 'failed', 'start-failed']
    assert evidence.results[2].stderr_bytes > 0
    events = await sessions.read_session(sid)
    assert len([e for e in events if e.type == 'sandbox/outcome']) == 4
    assert all(e.data['converged'] for e in events if e.type == 'sandbox/outcome')
    assert 'PRIVATE-CHECK-DATA' not in json.dumps([e.to_dict() for e in events])
