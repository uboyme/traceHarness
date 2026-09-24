"""One-off diagnostic: retain original source fix, restore one candidate-added test."""
import asyncio
import hashlib
import json
import shutil
from pathlib import Path

from tests.real_repository_evaluation.materials import apply_patch
from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.evaluation.evaluators.product_manifest import load_product_suite
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.sandbox.config import load_sandbox_file
from traceh.sandbox.service import GUEST_ENVIRONMENT, SandboxExecutionService
from traceh.session.sqlite import SqliteEventStore


async def main(root, material):
    suite = load_product_suite(load_benchmark_manifest(material),
                               provider_id='offline', model_id='offline')
    task = next(t for t in suite.tasks if t.task_id == 'pylint-dev__astroid-1196')
    workspace = root / 'candidate'
    shutil.copytree(task.initial_dir, workspace)
    patch_hash = '2696cccbede368d2ffc46130f9206295258cc42d3f91544603eefc612e0c7c4f'
    patch_path = Path('.traceh/p3/p04/arms/01/run/attempts/001/cas/sha256')
    patch = (patch_path / patch_hash[:2] / patch_hash).read_bytes()
    assert hashlib.sha256(patch).hexdigest() == patch_hash
    apply_patch(workspace, patch.decode('utf-8'))
    extra_test = Path('tests/unittest_inference.py')
    before = hashlib.sha256((workspace / extra_test).read_bytes()).hexdigest()
    original = (task.initial_dir / extra_test).read_bytes()
    (workspace / extra_test).write_bytes(original)
    assert before != hashlib.sha256(original).hexdigest()
    store = SqliteEventStore(root / 'events')
    service = SandboxExecutionService(
        store=store, cas=LocalArtifactCas(root / 'cas'),
        policy=load_sandbox_file(Path('.traceh/rr-eval/sandbox.json')).policy,
    )
    plan = task.settings.host_profile.verification_plan
    env = dict(GUEST_ENVIRONMENT)
    env.update(plan.environment.overrides)
    try:
        async with service.scope(
            SandboxOwner('verification', 'astroid-extra-test-diagnostic'),
            stream_id='diagnostic:astroid-extra-test', workspace=workspace,
            data_dir=None, publish_changes=False,
            environment=tuple(sorted(env.items())), retain_output=True,
        ) as scope:
            command = plan.commands[0]
            result = await scope.run(
                command.argv, timeout_seconds=command.timeout_ms / 1000,
                max_output_bytes=plan.max_output_bytes,
            )
        (root / 'stdout.txt').write_bytes(result.stdout)
        (root / 'stderr.txt').write_bytes(result.stderr)
        reports = [json.loads(line.split('=', 1)[1])
                   for line in result.stdout.decode('utf-8').splitlines()
                   if line.startswith('TRACEH_RR_RESULT=')]
        summary = dict(
            kind='diagnostic-not-reclassified-original-trial',
            original_patch_sha256=patch_hash,
            diagnostic_action='restore only candidate-added unselected test file to base bytes',
            restored_path=extra_test.as_posix(),
            restored_sha256=hashlib.sha256(original).hexdigest(),
            source_fix_retained=True, frozen_verifier_unchanged=True,
            exit_code=result.exit_code, status=result.status,
            report=reports[0] if len(reports) == 1 else None,
            receipt=result.receipt,
        )
        (root / 'summary.json').write_text(
            json.dumps(summary, indent=2) + '\n', encoding='utf-8',
        )
        print(json.dumps({k: v for k, v in summary.items() if k not in ('report', 'receipt')},
                         ensure_ascii=False))
        if reports:
            print('test counts', {k: len(reports[0][k])
                                  for k in ('passed', 'failed', 'errors', 'missing')})
    finally:
        await store.aclose()


diagnostic_root = Path('.traceh/cf-a4').resolve()
diagnostic_material = Path('.traceh/rr-eval/live-pilot-v3/material').resolve()
diagnostic_root.mkdir(exist_ok=False)
asyncio.run(main(diagnostic_root, diagnostic_material))
