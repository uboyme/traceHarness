"""Explicit WC-1F task and oracle, never installed as runtime defaults."""

REQUIREMENT = (
    "实现 reconciliation.py 中的 reconcile(base, updates)。先查看 INDEX.md，按两份规格"
    "实现记录合并、版本冲突和删除语义。只修改 reconciliation.py，保留接口，不改规格，"
    "不新增文件或安装依赖。最终说明实现与实际验证结果，未运行的检查不要声称已运行。"
)
FILES = {
    ".gitignore": "__pycache__/\n",
    "INDEX.md": "# Reconciliation specification\n"
    "identity.md: input validation and output contract\n"
    "precedence.md: revision precedence, conflicts and tombstones\n"
    "reconciliation.py: implementation target\n",
    "identity.md": "# Identity and shape\n"
    "Both inputs must be lists. Each record must be a dict with exactly key, revision, value.\n"
    "key must be a nonempty str with no leading or trailing whitespace.\n"
    "revision must be a nonnegative int; bool is not an integer for this contract.\n"
    "value must be a str or None. Reject invalid input with ValueError.\n"
    "Validate every record, including older records that will not win.\n"
    "Return a new list of winning records sorted by key, excluding deleted keys.\n"
    "Do not mutate either input or its records; output records must be fresh dicts.\n",
    "precedence.md": "# Revision rules\n"
    "Consider base and updates together. For each key choose its greatest revision.\n"
    "At that greatest revision, identical values are duplicates and are accepted.\n"
    "Different values at the greatest revision are a conflict: raise ValueError.\n"
    "Conflicts solely at lower revisions do not matter if a newer record exists.\n"
    "A winning value of None deletes the key. A newer string value can resurrect it.\n"
    "Results and conflict decisions must not depend on input order.\n",
    "reconciliation.py": "def reconcile(base, updates):\n    return None\n",
}

REFERENCE = """def reconcile(base, updates):
    if type(base) is not list or type(updates) is not list:
        raise ValueError('input shape')
    groups = {}
    for record in base + updates:
        if type(record) is not dict or set(record) != {'key', 'revision', 'value'}:
            raise ValueError('record shape')
        key, revision, value = record['key'], record['revision'], record['value']
        if type(key) is not str or not key or key.strip() != key:
            raise ValueError('key')
        if type(revision) is not int or revision < 0:
            raise ValueError('revision')
        if value is not None and type(value) is not str:
            raise ValueError('value')
        groups.setdefault(key, []).append(record)
    result = []
    for key, records in sorted(groups.items()):
        revision = max(r['revision'] for r in records)
        values = {r['value'] for r in records if r['revision'] == revision}
        if len(values) != 1:
            raise ValueError('conflict')
        value = next(iter(values))
        if value is not None:
            result.append(dict(key=key, revision=revision, value=value))
    return result
"""

CHECKS = """import copy,itertools
from reconciliation import reconcile
def r(key, rev, value): return dict(key=key, revision=rev, value=value)
passed = 0
def check(base, updates, want):
 global passed
 before = copy.deepcopy((base, updates))
 result = reconcile(base, updates)
 assert result == want, (result, want)
 assert (base, updates) == before
 assert all(out is not record for out in result for record in base + updates)
 passed += 1
check([], [], [])
check([r('z',0,'old')], [r('a',1,''),r('z',2,'new')], [r('a',1,''),r('z',2,'new')])
check([r('a',3,'current')], [r('a',1,'old')], [r('a',3,'current')])
check([r('a',1,'x')], [r('a',1,'x')], [r('a',1,'x')])
check([r('a',1,'x')], [r('a',2,None)], [])
check([r('a',2,None)], [r('a',3,'back')], [r('a',3,'back')])
for records in itertools.permutations([r('a',1,'x'),r('a',1,'y'),r('a',2,'latest')]):
 check(list(records[:1]), list(records[1:]), [r('a',2,'latest')])
invalid = [([r('a',2,'x')],[r('a',2,'y')]),
           ([r('a',2,None)],[r('a',2,'y')]),
           ((),[]), ([],{}), ([{}],[]), ([r('',0,'x')],[]),
           ([r(' a',0,'x')],[]), ([r('a',True,'x')],[]),
           ([r('a',-1,'x')],[]), ([r('a',0,9)],[r('a',3,'good')]),
           ([dict(r('a',0,'x'),extra=1)],[])]
for base, updates in invalid:
 try: reconcile(base, updates)
 except ValueError: passed += 1
 else: raise AssertionError(('expected ValueError', base, updates))
print(str(passed)+' functional cases and source boundaries passed')
"""
