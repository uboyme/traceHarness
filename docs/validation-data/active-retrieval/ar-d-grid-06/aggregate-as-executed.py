"""Aggregate completed immutable reports and individually reviewed judgments offline."""
import hashlib
import json
import math
from pathlib import Path
from live_active_retrieval.grid import SourceOnly, source_files, support_files

root = Path('docs/validation-data/active-retrieval')
grid = root / 'ar-d-grid-06'
def read(p):
    return json.loads(p.read_text(encoding='utf-8'))
def write(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
packets = read(grid/'review-packets.json')
reviews = read(grid/'manual-review.json')
assert reviews['complete'] and len(reviews['reviews']) == len(packets) == 144
judgments = {(r['arm'], r['identity']): r for r in reviews['reviews']}
assert len(judgments) == 144
frozen=read(grid/'frozen.json')
previous=read(root/'ar-d-grid-05/frozen.json')
manifest = frozen['manifest']
assert manifest==previous['manifest']
assert (grid/'fixtures.json').read_bytes()==(root/'ar-d-grid-05/fixtures.json').read_bytes()
assert frozen['source_files']['baseline']==previous['source_files']['baseline']
assert source_files(Path('src'))==frozen['source_files']['candidate']
assert support_files()==frozen['support_files']==previous['support_files']
cases = {c['id']: c for c in manifest['cases']}
violations = ['scope_violations','duplicate_side_effects','unsupported_test_facts','stale_memory_as_current']
def metric(values):
    if not values:
        return {'n':0, 'mean':None, 'p95':None}
    return {'n':len(values),'mean':sum(values)/len(values),'p95':sorted(values)[math.ceil(.95*len(values))-1], 'scope':'completed target answers only; missing observations are not zero'}
summary = {'decision':'NO-GO','completed_grid':True,'planned_cases_per_arm':72,'manual_review_complete':True,'arms':{}}
costs = {'runs':[], 'note':'Provider-reported tokens only; unknown usage is retained, not estimated as zero. Preparation and target are separated.'}
for arm in ['baseline','candidate']:
    ps = [p for p in packets if p['arm']==arm]
    assert len(ps)==72
    reports=[]
    for p in ps:
        folder=grid/arm/p['identity']
        j=judgments[(arm,p['identity'])]
        for key, filename in [('report_sha256','report.json'),('source_events_sha256','source-events.json')]:
            digest=hashlib.sha256((folder/filename).read_bytes()).hexdigest()
            assert digest==j[key]==p[key]
        assert not p['audit_error'] and not p['replay_errors'] and not p['invariant_errors']
        allowed=SourceOnly(p['family'],folder/'workspace').allowed
        assert all(t['name'] in allowed for t in p['successful_tools']), (arm,p['identity'],p['successful_tools'])
        if p['expected']=='value' and j['joint_pass']:
            assert p['dispatched_evidence']
        if p['family']=='output':
            assert p['output_executions']==1
        reports.append(read(folder/'report.json'))
    reopen=read(grid/(arm+'-reopen.json'))
    assert reopen['cases']==72 and reopen['provider_calls']==0
    for r in reopen['results']:
        assert not r['replay_errors'] and not r['invariant_errors']
        assert r['source_events_equal_recorded_export'] and r['attempts_started']==r['attempts_ended']
    completed=[r for r in reports if r.get('reason')=='completed' and not r.get('error')]
    aj=[judgments[(arm,p['identity'])] for p in ps]
    result={'joint_pass':sum(j['joint_pass'] for j in aj),'completed_answers':len(completed),
            'execution_errors':[{'identity':p['identity'],'error':p['error']} for p in ps if p['error']],
            'connection_failures':sum('provider-tls-eof' in str(p['error']) for p in ps),
            'by_family':{},'by_language':{},
            'observed_violations':{k:sum(j[k] for j in aj) for k in violations},
            'reopened_requests':sum(r['requests'] for r in reopen['results']),
            'replay_invariant_errors':0,'unended_attempts':0}
    for field in ['family','language']:
        names=['history','skill','memory','output'] if field=='family' else ['zh','en']
        for name in names:
            subset=[p for p in ps if cases[p['identity'].split('-',1)[1]][field]==name]
            result['by_'+field][name]={'planned':len(subset),'joint_pass':sum(judgments[(arm,p['identity'])]['joint_pass'] for p in subset)}
    for key in ['steps','search_read_count','target_seconds']:
        result[key]=metric([r[key] for r in completed if r.get(key) is not None])
    summary['arms'][arm]=result
    for expectation in ['value','no-evidence']:
        subset=[p for p in ps if p['expected']==expectation]
        result[expectation]={'planned':len(subset),'joint_pass':sum(judgments[(arm,p['identity'])]['joint_pass'] for p in subset)}
    phases={}
    for phase,key in [('target','target_usage'),('all','all_usage')]:
        us=[r[key] for r in reports]
        phases[phase]={'attempts_ended':sum(u['attempts'] for u in us),'inflight_usage_unknown':0}
        for name in ['unknown_usage','estimated_usage','input_tokens','output_tokens','total_tokens']:
            phases[phase][name]=sum(u[name] for u in us)
    phases['preparation']={k:phases['all'][k]-phases['target'][k] for k in phases['all']}
    costs['runs'].append({'run':grid.name,'arm':arm,'phases':phases})
identities=sorted(p['identity'] for p in packets if p['arm']=='candidate')
summary['discovery_gain_cases']=[i for i in identities if cases[i.split('-',1)[1]]['discovery'] and judgments[('candidate',i)]['joint_pass'] and not judgments[('baseline',i)]['joint_pass']]
summary['observed_lost_pass_cases']=[i for i in identities if judgments[('baseline',i)]['joint_pass'] and not judgments[('candidate',i)]['joint_pass']]
summary['all_gained_pass_cases']=[i for i in identities if judgments[('candidate',i)]['joint_pass'] and not judgments[('baseline',i)]['joint_pass']]
by_key={(p['arm'],p['identity']):p for p in packets}
matched=[i for i in identities if not any(by_key[(a,i)]['error'] for a in ['baseline','candidate'])]
summary['both_arms_without_execution_error']={
 'paired_cases':len(matched),
 'scores':{a:sum(judgments[(a,i)]['joint_pass'] for i in matched) for a in ['baseline','candidate']},
 'note':'Supplementary matched observations excluding a pair when either arm has an execution error. Fixed 72-case totals above remain primary; this does not remove unfinished answers.'}
summary['discovery_gain_note']='Matched observations, not a causal estimate. Skill gains include repaired disclosure receipt retention. Stochastic per-case losses are reported, not concealed.'
c=summary['arms']['candidate']
passed=c['joint_pass']>=66 and all(v['joint_pass']>=15 for v in c['by_family'].values()) and not any(c['observed_violations'].values()) and bool(summary['discovery_gain_cases'])
summary['candidate_thresholds']={'joint_required':66,'actual':c['joint_pass'],'per_family_required':15,'quality_pass':passed,'release_allowed':False}
summary['decision']='GO' if passed else 'NO-GO'
summary['interpretation']='One contemporary run per arm on the original fixed 72 questions, with individual answer/evidence review and independent offline replay. No prompt, model, question, budget or acceptance threshold tuned during the run. These questions have been used before, so this is regression measurement, not a new held-out benchmark or statistical model-quality guarantee.'
write(grid/'summary.json',summary)
write(grid/'status.json',{'status':'complete-'+summary['decision'].lower(),'release_allowed':False,'summary':'summary.json','all_failures_retained':True,'prior_rounds_preserved':True})
write(grid/'costs.json',costs)
print(json.dumps(summary,ensure_ascii=False,indent=2))
