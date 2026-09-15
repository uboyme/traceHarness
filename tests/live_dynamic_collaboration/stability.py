"""Three finite probes reusing the WC-4 driver and original Evaluation owner."""

import argparse
import asyncio
import json
from pathlib import Path

from live_dynamic_collaboration import writable_acceptance as driver
from live_dynamic_collaboration.materials import write
from live_dynamic_collaboration.stability_materials import material
from traceh.evaluation.inputs import digest_bytes

CASES = ('invoice', 'command-recovery', 'integration-conflict')


def select(case):
    driver.FILES, driver.REFERENCE, driver.CHECKS, driver.REQUIREMENT = material(case)


def prepare(repository, sandbox, output):
    output.mkdir(exist_ok=False)
    for case in CASES:
        select(case)
        root = output / case
        driver.prepare(repository, sandbox, root)
        dataset_path = root / 'material/dataset.json'
        dataset = json.loads(dataset_path.read_text())
        dataset['cases'][0].update(case_id=case, group_id=case)
        write(dataset_path, dataset)
        manifest_path = root / 'material/benchmark.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['dataset']['sha256'] = digest_bytes(dataset_path.read_bytes())
        write(manifest_path, manifest)
        for name in ('stability.py', 'stability_materials.py'):
            (root / name).write_bytes(Path(__file__).with_name(name).read_bytes())
        contract_path = root / 'contract.json'
        contract = json.loads(contract_path.read_text())
        contract['files'] = {
            p.relative_to(root).as_posix(): digest_bytes(p.read_bytes())
            for p in root.rglob('*') if p.is_file() and p != contract_path
        }
        write(contract_path, contract)
        driver.preflight(root)
    write(output / 'batch.json', {
        'cases': list(CASES), 'max_real_calls': 96, 'per_case_calls': 32,
        'per_case_seconds': 600, 'sum_case_seconds': 1800,
        'connection_seconds': 60, 'provider_retries': 0,
        'policy': 'One run per case; stop the batch on unexpected failure. No baseline arm.',
        'contracts': {case: digest_bytes((output/case/'contract.json').read_bytes())
                      for case in CASES},
    })


async def run(profile, output, case):
    batch = json.loads((output/'batch.json').read_text())
    root = output/case
    assert digest_bytes((root/'contract.json').read_bytes()) == batch['contracts'][case]
    for name in ('stability.py', 'stability_materials.py'):
        assert (root/name).read_bytes() == Path(__file__).with_name(name).read_bytes()
    for previous in CASES[:CASES.index(case)]:
        report = json.loads((output/previous/'run/report.json').read_text())
        assert report['task_report']['attempts'][0]['success'], 'prior case failed; batch stopped'
    select(case)
    await driver.run(profile, root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'run'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', type=Path)
    parser.add_argument('--sandbox', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--case', choices=CASES)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.repository.resolve(), args.sandbox.resolve(), args.output.resolve())
    else:
        asyncio.run(run(args.profile.resolve(), args.output.resolve(), args.case))
