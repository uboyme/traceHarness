"""Record explicit human/agent-reviewed decisions, never infer from provisional scores."""
import json
import sys
from pathlib import Path
root=Path('docs/validation-data/active-retrieval/ar-d-grid-06')
packets={(p['arm'],p['identity']):p for p in json.loads(Path('.pytest-tmp-codex/ar-d-06-review.json').read_text(encoding='utf-8'))}
document=json.loads((root/'manual-review.json').read_text(encoding='utf-8')) if (root/'manual-review.json').exists() else {
 'complete':False,
 'criteria':'Same frozen grid-05 criteria: correct attribution and actual successful dispatched source evidence for positives; qualified lack-of-evidence consistent with inspected scope for negatives. Partial-scope absence must be qualified. Transport failures and unfinished answers retained. No-lookup controls scored separately, without altering the 72-case denominator.',
 'reviews':[]}
existing={(r['arm'],r['identity']) for r in document['reviews']}
reasons={
 'pass':'Expected value correctly attributed; original evidence present in successful target dispatch.',
 'negative':'Qualified lack-of-evidence answer consistent with actual inspected scope; no invented requested value.',
 'negative-extra':'Requested negative conclusion supported by full selected-output search, but answer adds an incorrect output line count/coverage description. Target joint pass does not mean all prose is correct.',
 'partial':'Categorical absence for the whole source after inspecting only part of it; insufficient evidence coverage.',
 'missing':'Answer lacks requested fact and actual successful target dispatch has no source evidence for it.',
 'wrong':'Explicitly gives an unsupported requested fact or misattributes process status as business validation code.',
 'unfinished':'Reached the frozen Step limit without completing the requested answer.',
 'transport':'Provider connection failed; no completed target answer. Retained in fixed denominator and separately reported, not a model-behavior failure.',
}
for line in sys.stdin:
 if not line.strip(): continue
 arm,identity,decision=line.split()
 arm={'b':'baseline','c':'candidate'}[arm]
 key=(arm,identity)
 assert key not in existing and decision in reasons
 p=packets[key]
 if decision=='pass': assert p['dispatched_evidence']
 document['reviews'].append(dict(arm=arm,identity=identity,joint_pass=decision in {'pass','negative','negative-extra'},reason=reasons[decision],unsupported_test_facts=int(decision in {'wrong','negative-extra'}),stale_memory_as_current=0,scope_violations=0,duplicate_side_effects=0,report_sha256=p['report_sha256'],source_events_sha256=p['source_events_sha256']))
 existing.add(key)
(root/'manual-review.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('reviewed',len(existing))
