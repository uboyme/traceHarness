import json
import re
from pathlib import Path
from urllib.parse import unquote

docs=[Path(p) for p in ['docs/note/project-context.md','docs/note/project-context-plain-zh.md','docs/plan/TRACEHARNESS_ACTIVE_RETRIEVAL_EXECUTION_PLAN.md','docs/validation-active-retrieval.md','docs/deal/007-active-retrieval-final-comparison.md']]
errors=[]; links=0; mermaids=0
chapters=[]
secret=re.compile(r'(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{24,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16})')
for i,p in enumerate(docs):
 text=p.read_text(encoding='utf-8')
 if i<2: chapters.append(re.findall(r'^##\s+(\d+)[.．、\s]',text,re.M))
 if secret.search(text): errors.append(str(p)+': credential pattern')
 fence=None
 plain=[]
 for line in text.splitlines():
  m=re.match(r'^\s*(`{3,}|~{3,})(.*)$',line)
  if m:
   if fence is None:
    fence=m[1]
    if m[2].strip()=='mermaid': mermaids+=1
   elif m[1][0]==fence[0] and len(m[1])>=len(fence): fence=None
   continue
  if fence is None: plain.append(line)
 if fence: errors.append(str(p)+': unclosed fence')
 for target in re.findall(r'\]\(([^\n]+?)\)', '\n'.join(plain)):
  target=target.strip().strip('<>')
  if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',target) or target.startswith(('#','/')): continue
  target=unquote(target.split('#',1)[0])
  if not target: continue
  links+=1
  if not (p.parent/target).exists(): errors.append(str(p)+': missing '+target)
assert chapters[0]==chapters[1], chapters
count=0
for p in [*Path('docs/validation-data/active-retrieval/ar-d-grid-06').rglob('*.json'), *Path('docs/validation-data/active-retrieval/ar-final-controls-01').rglob('*.json')]:
 text=p.read_text(encoding='utf-8'); json.loads(text); count+=1
 if secret.search(text): errors.append(str(p)+': credential pattern')
result={'documents':len(docs),'chapter_correspondence':chapters[0],'relative_links':links,'mermaid_blocks_closed':mermaids,'evidence_json_parsed_and_secret_pattern_checked':count,'errors':errors,'manual_flow_check':'Validation-only update: existing Projection/Reader, bounded search, source authority and protocol unchanged. Current grid-06 results distinguished from historical grid-05. Existing diagrams unchanged. Secret pattern scan is not an exhaustive credential detector.'}
Path('docs/validation-data/active-retrieval/ar-d-grid-06/doc-qa.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
assert not errors
