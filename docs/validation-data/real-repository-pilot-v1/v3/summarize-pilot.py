"""Derive descriptive pilot results from original bound reports and rechecks."""
import argparse
import hashlib
import json
from pathlib import Path

from traceh.evaluation.evaluators.product_review import trial_events
from traceh.evaluation.evidence import load_run


def phase_usage(run, report, attempt):
    trial = next(t for t in report['trials']
                 if t['identity']['trial_id'] == attempt['attempt_id'])
    _, events = trial_events(run, trial)
    execution = attempt['evidence']['execution']
    streams = {'session:' + session['session_id'] for session in execution['sessions']}
    views = {(e['stream_id'], e['data']['step_id']): e['data']['label']
             for e in events if e['type'] == 'request/view'}
    phases = {}
    for event in events:
        if event['type'] != 'model/attempt-end' or event['stream_id'] not in streams:
            continue
        data = event['data']
        phase = views.get((event['stream_id'], data['step_id']), 'unlabelled-execution')
        counters = phases.setdefault(phase, dict(
            model_attempts=0, known_input_tokens=0, known_output_tokens=0, unknown_usage_attempts=0,
        ))
        counters['model_attempts'] += 1
        usage = data.get('usage')
        if usage is not None and usage['quality'] == 'exact':
            counters['known_input_tokens'] += usage['input_tokens']
            counters['known_output_tokens'] += usage['output_tokens']
        else:
            counters['unknown_usage_attempts'] += 1
    if sum(p['model_attempts'] for p in phases.values()) != execution['model_attempts']:
        raise ValueError('phase attempt count differs from original execution report')
    if execution['tokens'] is not None:
        for key in ('input_tokens', 'output_tokens'):
            if sum(p['known_' + key] for p in phases.values()) != execution['tokens'][key]:
                raise ValueError('phase usage differs from original execution report')
    return phases


def summarize(root, output):
    prereg = json.loads((root / 'preregistration.json').read_bytes())
    layout = json.loads((root / 'output-layout.json').read_bytes())
    rows = []
    for entry in prereg['schedule']:
        number = entry['pair']
        pair = Path(layout[str(number)])
        summary_path = root / f'pair-{number:02d}-summary.json'
        if not summary_path.exists():
            raise ValueError(f'pair {number} not yet verified')
        summary = json.loads(summary_path.read_bytes())
        for arm in ('01', '02'):
            run = pair / 'arms' / arm / 'run'
            _, report, binding = load_run(run)
            if not report['complete'] or report['errors']:
                raise ValueError('incomplete original report')
            if len(report['task_report']['attempts']) != 1:
                raise ValueError('pilot pair must have one attempt per arm')
            attempt = report['task_report']['attempts'][0]
            if attempt['benchmark_task_id'] != entry['case']:
                raise ValueError('scheduled case differs')
            mode = attempt['requested_mode']
            cost_row = next(row for row in summary['rows'] if row['mode'] == mode)
            recheck = json.loads((root / f'recheck-{number:02d}-{arm}.json').read_bytes())
            digest = hashlib.sha256((run / 'report.json').read_bytes()).hexdigest()
            if (cost_row['report_sha256'] != digest or recheck['binding'] != binding
                    or not recheck['original_databases_unchanged']
                    or not recheck['target_and_evidence_rederived']):
                raise ValueError('diagnostic binding differs')
            evidence = attempt['evidence']
            if cost_row['execution_usage'] != evidence['execution']['tokens']:
                raise ValueError('usage differs')
            rows.append(dict(
                **cost_row, repetition=entry['repetition'], first_arm=entry['first_arm'],
                success=attempt['success'], timing=attempt['timing'],
                cumulative_work_duration_ms=evidence['execution']['cumulative_work_duration_ms'],
                provider_active_milliseconds=evidence['execution']['provider_active_milliseconds'],
                phase_usage=phase_usage(run, report, attempt),
                budget=evidence['budget'], workspaces=evidence['workspaces'],
                evidence_binding=binding, verified_request_snapshots=recheck['snapshots'],
            ))
    aggregates = {}
    for mode in sorted({row['mode'] for row in rows}):
        selected = [row for row in rows if row['mode'] == mode]
        successes = sum(row['success'] for row in selected)
        exact = all(row['execution_usage'] is not None for row in selected)
        cost = sum(row['known_estimated_cny'] for row in selected)
        aggregates[mode] = dict(
            executed_trials=len(selected), independent_tasks=len({r['case'] for r in selected}),
            successful_trials=successes,
            model_attempts=sum(row['model_attempts'] for row in selected),
            complete_usage_available=exact,
            total_execution_tokens=(sum(row['execution_usage']['total_tokens']
                                        for row in selected) if exact else None),
            known_estimated_cny=round(cost, 6),
            cancelled_unknown_calls=sum(len(row['cancelled_unknown_usage']) for row in selected),
            planning_cny=round(sum(row['planning_cny'] for row in selected), 6),
            estimated_cny_per_success=(round(cost / successes, 6) if exact and successes else None),
        )
    value = dict(
        format=1, study='development pilot; descriptive only; no official SWE-bench score',
        planned_trials=prereg['planned_trials'], executed_trials=len(rows),
        independent_tasks=len({row['case'] for row in rows}),
        rows=rows, by_mode=aggregates,
        verified_request_snapshots=sum(row['verified_request_snapshots'] for row in rows),
        all_budget_and_workspaces_converged=all(
            row['budget']['converged'] and row['workspaces']['converged']
            and row['workspaces']['live'] == 0 for row in rows),
        caveats=[
            'Known usage is priced at frozen published rates, not an actual invoice.',
            'Cancelled unknown usage is unavailable; planning charges do not restore actual usage.',
            'Timing is descriptive: shared host was used for diagnostic work during early runs.',
            'Three public development tasks and two repetitions '
            'do not establish statistical benefit.',
            'Mandatory multi allocation with readonly assistants; no adaptive-mode claim.',
            'Earlier v1 failures are retained separately, not pooled with v3.',
        ],
    )
    with output.open('x', encoding='utf-8', newline='\n') as file:
        json.dump(value, file, indent=2, ensure_ascii=False)
        file.write('\n')
    print(json.dumps(aggregates, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    summarize(args.root.resolve(), args.output.resolve())
