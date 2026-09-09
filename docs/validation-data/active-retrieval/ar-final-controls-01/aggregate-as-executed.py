"""Persist already reviewed no-lookup controls; verify their observed calls and replay."""
import hashlib
import json
from pathlib import Path

root=Path('docs/validation-data/active-retrieval/ar-final-controls-01')
def read(p): return json.loads(p.read_text(encoding='utf-8'))
packets=read(root/'review-packets.json')
assert len(packets)==8
reviews=[]
for p in packets:
    assert p['reason']=='completed' and not p['error'] and not p['calls']
    assert not p['replay_errors'] and not p['invariant_errors'] and not p['audit_error']
    folder=root/p['arm']/p['identity']
    for key,name in [('report_sha256','report.json'),('source_events_sha256','source-events.json')]:
        assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==p[key]
    events=read(folder/'source-events.json')
    requests=[e['data']['dispatch_request'] for e in events if e['type']=='request/snapshot']
    assert len(requests)==1
    tools={t['name'] for t in requests[0]['tools']}
    assert 'request_skill_reference' in tools
    if p['arm']=='candidate': assert 'search_skill' in tools
    reviews.append({k:p[k] for k in ['arm','identity','answer','report_sha256','source_events_sha256']} | {
        'answer_pass':True,'unnecessary_tool_calls':0,'read_tool_exposed':True,
        'active_search_tool_exposed':'search_skill' in tools,
        'reason':'Individually reviewed: answers the greeting/rewrite/arithmetic/repetition request directly; no lookup needed or performed.'})
summary={'complete':True,'acceptance_credit':False,'manual_reviews':reviews,'arms':{}}
for arm in ['baseline','candidate']:
    ps=[p for p in packets if p['arm']==arm]
    reopened=read(root/(arm+'-reopen.json'))
    assert reopened['cases']==4 and reopened['provider_calls']==0
    assert all(not r['replay_errors'] and not r['invariant_errors'] and r['source_events_equal_recorded_export'] and r['attempts_started']==r['attempts_ended'] for r in reopened['results'])
    summary['arms'][arm]={'cases':4,'correct_direct_answers':4,'tool_calls':0,'steps_per_case':1,
        'requests_replayed':sum(r['requests'] for r in reopened['results']),
        'provider_reported_usage':{k:sum(p['all_usage'][k] for p in ps) for k in ps[0]['all_usage']}}
summary['boundary']='Four explicitly no-lookup tasks per arm, one run each with unrelated selected Skill and exposed read/search tools. Does not prove all possible irrelevant-source situations avoid search. Separate from the frozen 72-question grid.'
(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary['arms'],ensure_ascii=False))
