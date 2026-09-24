"""Explicit batch orchestration of the original CLI; derived diagnostics only."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from tests.real_repository_evaluation.recheck import recheck
from traceh.api.json_types import canonical_json
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.comparison import inspect_experiment
from traceh.evaluation.evaluators.product_review import trial_events
from traceh.evaluation.evidence import load_run
from traceh.evaluation.variants import source_digest, source_files


async def check_pair(root, number, rates, pair):
    comparison = inspect_experiment(pair)
    rows = []
    for arm in ('01', '02'):
        run = pair / 'arms' / arm / 'run'
        _, report, _ = load_run(run)
        if not report['complete'] or report['errors']:
            raise ValueError('incomplete-original-report')
        for trial in report['trials']:
            if trial['convergence'] != 'converged' or trial['invariants'] != 'passed':
                raise ValueError('unproven-trial')
        for attempt in report['task_report']['attempts']:
            evidence = attempt['evidence']
            if (not evidence['budget']['converged']
                    or not evidence['workspaces']['converged']
                    or evidence['workspaces']['live'] != 0
                    or evidence['execution']['provider_failure_categories']):
                raise ValueError('unsafe-to-continue')
            usage = evidence['execution']['tokens']
            extra = evidence['unattributed']['tokens']
            unknown = []
            if usage is None:
                trial = next(t for t in report['trials']
                             if t['identity']['trial_id'] == attempt['attempt_id'])
                _, events = trial_events(run, trial)
                snapshots = {(e['stream_id'], e['seq']): e['data']['dispatch_request']
                             for e in events if e['type'] == 'request/snapshot'}
                known_input = known_output = 0
                for event in events:
                    if event['type'] != 'model/attempt-end':
                        continue
                    data = event['data']
                    observed = data.get('usage')
                    if observed is not None and observed['quality'] == 'exact':
                        known_input += observed['input_tokens']
                        known_output += observed['output_tokens']
                    else:
                        if data['status'] != 'cancelled':
                            raise ValueError('unexplained-unknown-usage')
                        request = snapshots[event['stream_id'], data['request_snapshot_seq']]
                        if type(request['max_output_tokens']) is not int:
                            raise ValueError('unbounded-cancelled-request')
                        size = len(canonical_json(request).encode('utf-8'))
                        capacity = size + request['max_output_tokens']
                        unknown.append(dict(attempt_id=data['attempt_id'],
                                            request_bytes_and_output=capacity,
                                            planning_cny=capacity * max(rates) / 1_000_000))
                if not unknown:
                    raise ValueError('missing-usage-without-explanation')
                known_cost = (known_input * rates[0] + known_output * rates[1]) / 1_000_000
            else:
                if usage['quality'] != 'exact' or extra is None or extra['quality'] != 'exact':
                    raise ValueError('unknown-execution-or-unattributed-usage')
                known_cost = ((usage['input_tokens'] + extra['input_tokens']) * rates[0]
                              + (usage['output_tokens'] + extra['output_tokens']) * rates[1]
                              ) / 1_000_000
            cost = known_cost + sum(u['planning_cny'] for u in unknown)
            rows.append(dict(pair=number, case=attempt['benchmark_task_id'],
                             mode=attempt['requested_mode'],
                             product_status=evidence['product_status'],
                             workflow_status=evidence['workflow_status'],
                             review_passed=evidence['review_passed'],
                             failure_code=evidence['failure_code'],
                             execution_usage=usage, known_estimated_cny=known_cost,
                             cancelled_unknown_usage=unknown, planning_cny=cost,
                             model_attempts=evidence['execution']['model_attempts'],
                             report_sha256=hashlib.sha256((run/'report.json').read_bytes()).hexdigest()))
        recheck_path = root / f'recheck-{number:02d}-{arm}.json'
        if not recheck_path.exists():
            await recheck(run, root, recheck_path)
    result = dict(comparison_status=comparison['status'], rows=rows,
                  planning_cny=sum(row['planning_cny'] for row in rows))
    path = root / f'pair-{number:02d}-summary.json'
    if path.exists():
        if json.loads(path.read_bytes()) != result:
            raise ValueError('derived-summary-drift')
    else:
        path.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result), flush=True)
    return result['planning_cny']

async def main(args):
    root = args.root.resolve()
    prereg = json.loads((root/'preregistration.json').read_bytes())
    rates = (prereg['input_cny_per_million'], prereg['output_cny_per_million'])
    spent = args.prior_version_planning_cny
    layout = json.loads(args.output_map.read_bytes())
    for entry in prereg['schedule']:
        number = entry['pair']
        pair = await asyncio.to_thread(Path(layout[str(number)]).resolve)
        plan = root / entry['plan']
        if hashlib.sha256(plan.read_bytes()).hexdigest() != entry['plan_sha256']:
            raise ValueError('plan-drift')
        if source_digest(source_files()[1]) != prereg['source_digest']:
            raise ValueError('source-drift')
        if number >= args.first_pair:
            b = json.loads((root/'material/benchmark.json').read_bytes())
            planned = b['task_settings']['task_budget']['max_tokens'] * 2 * max(rates) / 1_000_000
            if spent + planned > prereg['operational_budget_cny']:
                raise ValueError('operational-budget-stop')
            cli = build_parser().parse_args(['eval',str(root/'material'),'--output',
                str(pair),'--run-plan',str(plan),
                '--env-file',str(args.env_file.resolve())])
            _configure_from_environment(cli)
            print(json.dumps(dict(event='pair-started',pair=number,case=entry['case'])),flush=True)
            code = await _eval(cli)
            if code != 0:
                raise ValueError('original-cli-incomplete')
        spent += await check_pair(root, number, rates, pair)
    print(json.dumps(dict(event='batch-complete',planning_spend_including_prior_unknown=spent)),flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output-map',type=Path,required=True)
    parser.add_argument('--first-pair',type=int,required=True)
    parser.add_argument('--prior-version-planning-cny',type=float,required=True)
    asyncio.run(main(parser.parse_args()))
