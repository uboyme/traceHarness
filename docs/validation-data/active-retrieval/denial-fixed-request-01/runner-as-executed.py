import asyncio
import copy
import hashlib
import json
from pathlib import Path

from live_active_retrieval.history_smoke import load_provider
from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelRequest

root = Path('docs/validation-data/active-retrieval/denial-fixed-request-01')

async def main():
    root.mkdir(exist_ok=False)
    cases = []
    selected = [
        ('ar-e-denial-04', '113-o-nearby'),
        ('ar-e-denial-04', '419-o-nearby'),
        ('ar-e-denial-04', '227-s-english'),
        ('ar-d-grid-05', '113-s-english'),
    ]
    for experiment, identity in selected:
        arm = 'candidate' if experiment=='ar-d-grid-05' else 'baseline'
        folder = root.parent/experiment/arm/identity
        report = json.loads((folder/'report.json').read_text(encoding='utf-8'))
        events = json.loads((folder/'source-events.json').read_text(encoding='utf-8'))
        denial = next(e for e in events if e['seq']>report['target_start_seq']
                      and e['type']=='tool/result' and e['data']['status']=='denied')
        snap = next(e for e in events if e['seq']>denial['seq'] and e['type']=='request/snapshot')
        original = snap['data']['dispatch_request']
        changed = copy.deepcopy(original)
        for msg in changed['messages']:
            if msg.get('tool_call_id') == denial['data']['tool_call_id']:
                msg['content'] = canonical_json({'status':'denied','error_type':denial['data']['error_type'],
                                                'reason':denial['data']['content'],
                                                'policy':denial['data']['data'].get('policy')})
        assert changed != original
        cases.append({'identity':identity,'source':str(folder),'snapshot_seq':snap['seq'],
                      'source_sha256':hashlib.sha256((folder/'source-events.json').read_bytes()).hexdigest(),
                      'requests':{'baseline':original,'candidate':changed}})
    (root/'frozen.json').write_text(json.dumps({'scope':'Four fixed post-denial requests; one response each arm; no tools executed, no end-to-end score.',
                                              'cases':cases},ensure_ascii=False,indent=2),encoding='utf-8')
    provider,model = load_provider(Path('C:/Users/caojie/Desktop/instudy/tracehtest/launch.json'))
    results = []
    for i,case in enumerate(cases):
        for arm in (('baseline','candidate') if i%2==0 else ('candidate','baseline')):
            request = ModelRequest.from_dict(case['requests'][arm])
            assert request.model==model and request.provider==provider.name
            result = {'identity':case['identity'],'arm':arm}
            try:
                response=await provider.complete(request)
                result['response']=response.to_dict()
            except Exception as error:
                result['error_type']=type(error).__name__
            results.append(result)
            (root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False),flush=True)

asyncio.run(main())
