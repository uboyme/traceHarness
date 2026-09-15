"""Reduced reproductions of repository contracts, NOT verbatim production bugs.

Reference repairs and hidden checks stay outside each task checkout. No case
requires delegation. These authored cases are development material, not an
independently sampled population or a model-quality benchmark in general.
"""

# Literal fixture programs stay intact for review and oracle reproduction.
# ruff: noqa: E501

from dataclasses import dataclass
from textwrap import dedent


@dataclass(frozen=True)
class Case:
    name: str
    category: str
    sources: tuple[str, ...]
    requirement: str
    files: dict[str, str]
    checks: str
    repaired: dict[str, str]
    split: str = "development"


def code(value):
    return dedent(value).lstrip()


CASES = [
    Case(
        "bounded-identity",
        "local",
        ("src/traceh/agents/identity.py",),
        "修复 identity.valid：接受长度 1 至 256、无首尾空白的单行字符串；"
        "拒绝其他类型、控制字符和 Unicode 格式控制字符。不要自动转换或清洗身份。",
        {
            "identity.py": code("""
            def valid(value):
                return bool(str(value).strip())
        """)
        },
        code("""
            from identity import valid
            assert valid('调查-7') and valid('x' * 256)
            for value in (None, True, 7, '', ' a', 'a ', 'a\\nb', 'x' * 257, 'a\\u202eb', 'a\\x00b'):
                assert valid(value) is False, repr(value)
        """),
        {
            "identity.py": code("""
            import unicodedata
            def valid(value):
                return (isinstance(value, str) and 0 < len(value) <= 256
                        and value == value.strip()
                        and all(unicodedata.category(c) not in {'Cc', 'Cf'} for c in value))
        """)
        },
    ),
    Case(
        "event-payload-isolation",
        "local",
        ("src/traceh/api/events.py",),
        "修复 MemoryLog 的事件隔离：append 输入、append 返回和 read 返回的嵌套 JSON "
        "都属于各自调用者，修改其中一个不能修改账本或其他调用者的数据。保留当前 API。",
        {
            "eventlog.py": code("""
            class MemoryLog:
                def __init__(self):
                    self.rows = []
                def append(self, payload):
                    self.rows.append(dict(payload))
                    return self.rows[-1]
                def read(self):
                    return list(self.rows)
        """)
        },
        code("""
            from eventlog import MemoryLog
            log = MemoryLog()
            source = {'nested': {'values': [1]}}
            returned = log.append(source)
            source['nested']['values'].append(2)
            returned['nested']['values'].append(3)
            assert log.read() == [{'nested': {'values': [1]}}]
            first, second = log.read(), log.read()
            first[0]['nested']['values'].clear()
            assert second == log.read() == [{'nested': {'values': [1]}}]
        """),
        {
            "eventlog.py": code("""
            from copy import deepcopy
            class MemoryLog:
                def __init__(self):
                    self.rows = []
                def append(self, payload):
                    self.rows.append(deepcopy(payload))
                    return deepcopy(self.rows[-1])
                def read(self):
                    return deepcopy(self.rows)
        """)
        },
    ),
    Case(
        "unicode-page-cursor",
        "local",
        ("src/traceh/session/tool_output.py",),
        "分页读取中文或 emoji 时会丢字。page 的 offset/count 和 next_offset 都应按 Unicode "
        "码点计算，不是 UTF-8 字节。到末尾 next_offset 为 None，越界 offset 或非正 count 报 ValueError。",
        {
            "pages.py": code("""
            def page(text, offset, count):
                if not 0 <= offset <= len(text) or count <= 0:
                    raise ValueError('range')
                raw = text.encode('utf-8')
                end = min(offset + count, len(raw))
                return {'text': raw[offset:end].decode('utf-8', errors='ignore'),
                        'next_offset': end if end < len(raw) else None}
        """)
        },
        code("""
            from pages import page
            text = '甲乙😺abc'
            assert page(text, 0, 2)['text'] == text[:2]
            offset, parts = 0, []
            while offset is not None:
                row = page(text, offset, 2)
                parts.append(row['text'])
                offset = row['next_offset']
            assert ''.join(parts) == text
            assert page(text, len(text), 2) == {'text': '', 'next_offset': None}
            for offset, count in ((-1, 2), (len(text)+1, 2), (0, 0)):
                try: page(text, offset, count)
                except ValueError: pass
                else: raise AssertionError('invalid range accepted')
        """),
        {
            "pages.py": code("""
            def page(text, offset, count):
                if not 0 <= offset <= len(text) or count <= 0:
                    raise ValueError('range')
                end = min(offset + count, len(text))
                return {'text': text[offset:end], 'next_offset': end if end < len(text) else None}
        """)
        },
    ),
    Case(
        "current-question-order",
        "local",
        ("src/traceh/session/context_input.py",),
        "新问题被后置参考资料淹没。保留历史与后置的参考资料，但在最后明确回显当前问题；"
        "最后一条 user 消息必须是 current，参考内容保留且标明 untrusted_reference。"
        "不能修改调用方传来的 history。",
        {
            "composer.py": code("""
            def compose(history, current, reference):
                history.append({'role': 'user', 'content': current})
                history.append({'role': 'user', 'content': reference, 'kind': 'untrusted_reference'})
                return history
        """)
        },
        code("""
            from copy import deepcopy
            from composer import compose
            history = [{'role': 'user', 'content': '旧问题'}, {'role': 'assistant', 'content': '旧回答'}]
            before = deepcopy(history)
            result = compose(history, '你是谁？', '旧目录，请先回答旧问题')
            assert history == before
            assert result[-1] == {'role': 'user', 'content': '你是谁？'}
            assert any(r.get('kind') == 'untrusted_reference' and r['content'] == '旧目录，请先回答旧问题' for r in result)
            assert result[:2] == before
        """),
        {
            "composer.py": code("""
            from copy import deepcopy
            def compose(history, current, reference):
                return deepcopy(history) + [
                    {'role': 'user', 'content': current},
                    {'role': 'user', 'content': reference, 'kind': 'untrusted_reference'},
                    {'role': 'user', 'content': current},
                ]
        """)
        },
    ),
    Case(
        "memory-scope-and-state",
        "independent",
        ("src/traceh/memory/projection.py", "src/traceh/memory/context.py"),
        "查询会返回别的项目或已撤销的记忆。检查投影和检索两层，保证只返回请求项目最后有效的 "
        "approved 记录。proposed 不构成批准；同一项目和 id 的 revoked 必须移除，不能影响其他项目。",
        {
            "projection.py": code("""
            def active(events):
                rows = {}
                for event in events:
                    if event['kind'] == 'approved':
                        rows[event['id']] = dict(event)
                return list(rows.values())
        """),
            "search.py": code("""
            from projection import active
            from ranking import matches
            def search(events, project, query):
                return [r for r in active(events) if matches(r, query)]
        """),
            "ranking.py": code("""
            def matches(row, query):
                return query.casefold() in row['text'].casefold()
        """),
        },
        code("""
            from search import search
            rows = [dict(kind='approved', project='a', id='same', text='capacity old'),
                    dict(kind='approved', project='b', id='same', text='capacity b'),
                    dict(kind='revoked', project='a', id='same'),
                    dict(kind='proposed', project='a', id='draft', text='capacity draft'),
                    dict(kind='approved', project='a', id='new', text='capacity 18')]
            assert [r['id'] for r in search(rows, 'a', 'CAPACITY')] == ['new']
            assert [r['text'] for r in search(rows, 'b', 'capacity')] == ['capacity b']
        """),
        {
            "projection.py": code("""
            def active(events):
                rows = {}
                for event in events:
                    key = (event['project'], event['id'])
                    if event['kind'] == 'approved': rows[key] = dict(event)
                    elif event['kind'] == 'revoked': rows.pop(key, None)
                return list(rows.values())
        """),
            "search.py": code("""
            from projection import active
            from ranking import matches
            def search(events, project, query):
                return [r for r in active(events) if r['project'] == project and matches(r, query)]
        """),
        },
    ),
    Case(
        "worker-receipt-identity",
        "independent",
        ("src/traceh/evaluation/comparison.py", "src/traceh/evaluation/variant_execution.py"),
        "候选进程先返回时比较结果被标反。compare 必须按 variant_id 配对，核对 report 的规范 JSON "
        "SHA256，拒绝未知、重复或缺失的臂以及摘要不符。输出仍按 planned 顺序；不要按返回先后猜身份。",
        {
            "comparison.py": code("""
            from hashing import digest
            def compare(planned, receipts):
                return [(name, receipt['report']) for name, receipt in zip(planned, receipts)]
        """),
            "hashing.py": code("""
            import hashlib
            import json
            def digest(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        """),
            "worker.py": code("""
            from hashing import digest
            def receipt(name, report):
                return {'variant_id': name, 'report': report, 'sha256': digest(report)}
        """),
        },
        code("""
            from comparison import compare
            from worker import receipt
            a, b = receipt('base', {'pass': 1}), receipt('new', {'pass': 2})
            assert compare(['base', 'new'], [b, a]) == [('base', {'pass': 1}), ('new', {'pass': 2})]
            for rows in ([a, a], [a], [a, receipt('other', {})], [a, {**b, 'sha256': 'wrong'}]):
                try: compare(['base', 'new'], rows)
                except ValueError: pass
                else: raise AssertionError('receipt binding not enforced')
        """),
        {
            "comparison.py": code("""
            from hashing import digest
            def compare(planned, receipts):
                by_id = {}
                for receipt in receipts:
                    name = receipt['variant_id']
                    if name not in planned or name in by_id or digest(receipt['report']) != receipt['sha256']:
                        raise ValueError('receipt')
                    by_id[name] = receipt['report']
                if set(by_id) != set(planned): raise ValueError('missing')
                return [(name, by_id[name]) for name in planned]
        """)
        },
    ),
    Case(
        "tool-content-retention",
        "independent",
        ("src/traceh/session/tool_output.py", "src/traceh/session/surface_replacement.py"),
        "大工具结果被裁剪后无法完整找回。检查编码、保存和分页读取，保持小结果直接可见；"
        "大结果保存完整原文到按 UTF-8 SHA256 寻址的 store，展示只给 preview/ref。读取 ref 时"
        "重新核对摘要，使用字符 offset/count，拒绝损坏内容，不能用预览替代原文。",
        {
            "output.py": code("""
            from codec import digest
            def present(text, limit, store):
                if len(text) <= limit: return {'text': text}
                ref = digest(text)
                store[ref] = text[:limit]
                return {'preview': text[:limit], 'ref': ref}
        """),
            "reader.py": code("""
            def read(store, ref, offset, count):
                return store[ref][offset:offset+count]
        """),
            "codec.py": code("""
            import hashlib
            def digest(text):
                return hashlib.sha256(text.encode('utf-8')).hexdigest()
        """),
        },
        code("""
            from output import present
            from reader import read
            store, original = {}, '甲乙丙丁' * 50
            row = present(original, 8, store)
            assert row['preview'] == original[:8] and store[row['ref']] == original
            assert read(store, row['ref'], 7, 35) == original[7:42]
            assert present('短', 8, store) == {'text': '短'}
            store[row['ref']] = 'tampered'
            try: read(store, row['ref'], 0, 1)
            except ValueError: pass
            else: raise AssertionError('corrupt source accepted')
        """),
        {
            "output.py": code("""
            from codec import digest
            def present(text, limit, store):
                if len(text) <= limit: return {'text': text}
                ref = digest(text)
                store[ref] = text
                return {'preview': text[:limit], 'ref': ref}
        """),
            "reader.py": code("""
            from codec import digest
            def read(store, ref, offset, count):
                text = store[ref]
                if digest(text) != ref: raise ValueError('digest')
                return text[offset:offset+count]
        """),
        },
    ),
    Case(
        "runtime-profile-binding",
        "independent",
        ("src/traceh/product/registry.py", "src/traceh/product/runtime.py"),
        "预检查展示的能力与实际启动不一致。launch 要通过 registry 唯一解析 profile，"
        "要求运行 Provider/model 与配置一致，再把全部且仅有声明的工具交给 factory。"
        "未知 profile、未知工具或模型连接不符必须拒绝，不能偷偷回退。",
        {
            "host.py": code("""
            from registry import resolve
            def launch(profiles, profile_id, provider, model, tools, factory):
                profile = resolve(profiles, profile_id)
                return factory(provider, model, tuple(tools))
        """),
            "registry.py": code("""
            def resolve(profiles, profile_id):
                return profiles.get(profile_id, next(iter(profiles.values())))
        """),
            "factory.py": code("""
            def build(provider, model, tools):
                return provider, model, tuple(tools)
        """),
        },
        code("""
            from host import launch
            from factory import build
            profiles = {'chosen': {'provider': 'p', 'model': 'm', 'tools': ['read']}}
            assert launch(profiles, 'chosen', 'p', 'm', ['read', 'write'], build) == ('p', 'm', ('read',))
            for key, provider, model, tools in [('unknown', 'p', 'm', ['read']), ('chosen', 'q', 'm', ['read']),
                                               ('chosen', 'p', 'other', ['read']), ('chosen', 'p', 'm', [])]:
                try: launch(profiles, key, provider, model, tools, build)
                except ValueError: pass
                else: raise AssertionError('unbound assembly accepted')
        """),
        {
            "registry.py": code("""
            def resolve(profiles, profile_id):
                if profile_id not in profiles: raise ValueError('unknown profile')
                return profiles[profile_id]
        """),
            "host.py": code("""
            from registry import resolve
            def launch(profiles, profile_id, provider, model, tools, factory):
                profile = resolve(profiles, profile_id)
                if profile['provider'] != provider or profile['model'] != model:
                    raise ValueError('connection')
                if any(name not in tools for name in profile['tools']): raise ValueError('tool')
                return factory(provider, model, tuple(profile['tools']))
        """),
        },
    ),
]

CASES += [
    Case(
        "cancel-before-worker-start",
        "dependent",
        ("src/traceh/evolution/background.py",),
        "后台 Job.start 后立即 cancel 有时缺少 settled 记录。保证每次 admitted 最终恰好一次 settled，"
        "cancel 返回前工作已终止，重复取消幂等，运行中取消也适用。不要用固定睡眠猜时序。",
        {
            "job.py": code("""
            import asyncio
            class Job:
                def __init__(self):
                    self.events, self.task = [], None
                    self.entered, self.release = asyncio.Event(), asyncio.Event()
                def start(self):
                    self.events.append('admitted')
                    self.task = asyncio.create_task(self._run())
                async def _run(self):
                    self.entered.set()
                    try: await self.release.wait()
                    finally: self.events.append('settled')
                async def cancel(self):
                    self.task.cancel()
                    try: await self.task
                    except asyncio.CancelledError: pass
        """)
        },
        code("""
            import asyncio
            from job import Job
            async def check():
                for immediate in (True, False):
                    job = Job()
                    job.start()
                    if not immediate: await job.entered.wait()
                    await asyncio.wait_for(job.cancel(), 1)
                    await asyncio.wait_for(job.cancel(), 1)
                    assert job.task.done()
                    assert job.events == ['admitted', 'settled']
            asyncio.run(check())
        """),
        {
            "job.py": code("""
            import asyncio
            class Job:
                def __init__(self):
                    self.events, self.task = [], None
                    self.entered, self.release = asyncio.Event(), asyncio.Event()
                def start(self):
                    self.events.append('admitted')
                    self.task = asyncio.create_task(self._run())
                async def _run(self):
                    self.entered.set()
                    try: await self.release.wait()
                    finally: self.events.append('settled')
                async def cancel(self):
                    if not self.task.done(): await self.entered.wait()
                    self.task.cancel()
                    try: await self.task
                    except asyncio.CancelledError: pass
        """)
        },
    ),
    Case(
        "commit-aware-compensation",
        "dependent",
        ("src/traceh/budgets/supervision.py", "src/traceh/agents/commit_reconciliation.py"),
        "创建回调可能先写入 created 再报错。目前异常补偿无条件退回额度，导致仍存在的资源失去预留。"
        "请按 request_id 重读实际创建记录：未创建才释放，已创建要保留；始终重抛原异常。"
        "成功路径保留预留并返回回调结果。不要凭异常类型判断是否提交。",
        {
            "provision.py": code("""
            def provision(book, created, request_id, create):
                book.reserve(request_id)
                try: return create(request_id)
                except BaseException:
                    book.release(request_id)
                    raise
        """),
            "book.py": code("""
            class Book:
                def __init__(self): self.held = set()
                def reserve(self, request_id):
                    if request_id in self.held: raise ValueError('duplicate reservation')
                    self.held.add(request_id)
                def release(self, request_id): self.held.remove(request_id)
        """),
            "storage.py": code("""
            def record(created, request_id, resource):
                created[request_id] = resource
                return resource
        """),
        },
        code("""
            from provision import provision
            from book import Book
            for committed in (False, True):
                book, created, error = Book(), {}, RuntimeError('original')
                def create(key):
                    if committed: created[key] = 'resource'
                    raise error
                try: provision(book, created, 'request', create)
                except RuntimeError as caught: assert caught is error
                else: raise AssertionError('error swallowed')
                assert ('request' in book.held) is committed
            book, created = Book(), {}
            assert provision(book, created, 'ok', lambda key: created.setdefault(key, 'done')) == 'done'
            assert book.held == {'ok'}
        """),
        {
            "provision.py": code("""
            def provision(book, created, request_id, create):
                book.reserve(request_id)
                try: return create(request_id)
                except BaseException:
                    if request_id not in created: book.release(request_id)
                    raise
        """)
        },
    ),
    Case(
        "stale-source-plan",
        "dependent",
        ("src/traceh/workspaces/catalog.py", "src/traceh/product/resources.py"),
        "准备好的读取计划会在重新绑定来源后静默读取新版本。Plan 必须绑定准备时的 source/revision，"
        "当前绑定不同则报 ValueError；合法计划读取原内容。不要自动刷新旧计划或按路径相同忽略版本。",
        {
            "catalog.py": code("""
            class Catalog:
                def __init__(self): self.entries = {}
                def bind(self, name, source, revision, content):
                    self.entries[name] = (source, revision, content)
                def plan(self, name):
                    source, revision, _ = self.entries[name]
                    return name, source, revision
        """),
            "reader.py": code("""
            def read(catalog, plan):
                name, source, revision = plan
                return catalog.entries[name][2]
        """),
        },
        code("""
            from catalog import Catalog
            from reader import read
            catalog = Catalog()
            catalog.bind('workspace', 'source', 'r1', 'one')
            old = catalog.plan('workspace')
            assert read(catalog, old) == 'one'
            for source, revision in [('source', 'r2'), ('different-source', 'r1')]:
                catalog.bind('workspace', source, revision, 'two')
                try: read(catalog, old)
                except ValueError: pass
                else: raise AssertionError('stale plan refreshed')
                assert read(catalog, catalog.plan('workspace')) == 'two'
        """),
        {
            "reader.py": code("""
            def read(catalog, plan):
                name, source, revision = plan
                current = catalog.entries[name]
                if current[:2] != (source, revision): raise ValueError('stale')
                return current[2]
        """)
        },
    ),
    Case(
        "unknown-usage-qualification",
        "dependent",
        (
            "src/traceh/evaluation/evaluators/product_metrics.py",
            "src/traceh/evaluation/comparison.py",
        ),
        "一个模型尝试没有 usage，比较器却说候选更便宜。total 必须统计所有尝试，包括失败/取消；"
        "任一 tokens 为 None 则整个合计未知，空集合才为零。qualifies 在任一成本未知时返回 False，"
        "质量不能退步，已知成本使用调用方给定 ratio，不能自行补估。",
        {
            "metrics.py": code("""
            def total(attempts):
                return sum(row['tokens'] or 0 for row in attempts if row['status'] == 'completed')
        """),
            "qualify.py": code("""
            from metrics import total
            def qualifies(base, candidate, base_score, candidate_score, ratio):
                return candidate_score >= base_score and total(candidate) <= total(base) * ratio
        """),
            "records.py": code("""
            def attempt(status, tokens):
                return {'status': status, 'tokens': tokens}
        """),
        },
        code("""
            from metrics import total
            from qualify import qualifies
            from records import attempt
            base = [attempt('completed', 100)]
            unknown = [attempt('completed', 30), attempt('cancelled', None)]
            assert total(unknown) is None and total([]) == 0
            assert total([attempt('failed', 20), attempt('completed', 30)]) == 50
            assert qualifies(base, unknown, 1, 2, 1.5) is False
            assert qualifies(unknown, base, 1, 2, 1.5) is False
            assert qualifies(base, [attempt('completed', 140)], 1, 2, 1.5)
            assert not qualifies(base, [attempt('completed', 10)], 2, 1, 1.5)
        """),
        {
            "metrics.py": code("""
            def total(attempts):
                if any(row['tokens'] is None for row in attempts): return None
                return sum(row['tokens'] for row in attempts)
        """),
            "qualify.py": code("""
            from metrics import total
            def qualifies(base, candidate, base_score, candidate_score, ratio):
                left, right = total(base), total(candidate)
                return (left is not None and right is not None
                        and candidate_score >= base_score and right <= left * ratio)
        """),
        },
    ),
]

CASES += [
    Case(
        "explicit-integer-quota",
        "local",
        ("src/traceh/budgets/events.py",),
        "quota.parse 只接受 1 到 max_value 的真正整数。JSON true/false、浮点数、字符串都必须拒绝，"
        "不能被转换成额度。max_value 是显式正整数，非法时同样报 ValueError。",
        {
            "quota.py": code("""
            def parse(value, max_value):
                value = int(value)
                if value < 1 or value > max_value: raise ValueError('range')
                return value
        """)
        },
        code("""
            from quota import parse
            assert parse(1, 8) == 1 and parse(8, 8) == 8
            for value, maximum in [(True, 8), (False, 8), (1.2, 8), ('2', 8), (0, 8), (9, 8), (1, True), (1, 0)]:
                try: parse(value, maximum)
                except ValueError: pass
                else: raise AssertionError('non-integer or invalid quota accepted')
        """),
        {
            "quota.py": code("""
            def parse(value, max_value):
                if type(value) is not int or type(max_value) is not int or not 1 <= value <= max_value:
                    raise ValueError('range')
                return value
        """)
        },
        split="holdout",
    ),
    Case(
        "resolved-path-containment",
        "local",
        ("src/traceh/tools/builtins/paths.py",),
        "文件读取目前只看字符串前缀，存在同名前缀目录、.. 和符号链接绕出工作区的问题。"
        "resolve(root, relative) 应解析真实路径，允许指向根内部的相对路径，拒绝绝对路径"
        "以及最终位置在根外的路径。保持返回 pathlib.Path，越界报 ValueError。",
        {
            "paths.py": code("""
            from pathlib import Path
            def resolve(root, relative):
                result = Path(root) / relative
                if not str(result).startswith(str(root)): raise ValueError('outside')
                return result
        """)
        },
        code("""
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from paths import resolve
            with TemporaryDirectory() as folder:
                root = Path(folder) / 'root'
                root.mkdir()
                outside = Path(folder) / 'root-other'
                outside.mkdir()
                (root / 'link').symlink_to(outside, target_is_directory=True)
                assert resolve(root, 'inside') == root / 'inside'
                for path in ('../root-other/file', 'link/file', str(outside / 'file')):
                    try: resolve(root, path)
                    except ValueError: pass
                    else: raise AssertionError('escape accepted')
        """),
        {
            "paths.py": code("""
            from pathlib import Path
            def resolve(root, relative):
                if Path(relative).is_absolute(): raise ValueError('absolute')
                root = Path(root).resolve()
                result = (root / relative).resolve()
                if not result.is_relative_to(root): raise ValueError('outside')
                return result
        """)
        },
        split="holdout",
    ),
    Case(
        "verification-artifact-binding",
        "independent",
        ("src/traceh/promotion/models.py", "src/traceh/promotion/verification.py"),
        "通过了旧补丁的验证报告被用于新补丁。review.accept 必须核对 request 中的 artifact_id、"
        "patch_sha、target 和 verifier_digest 全部相同且 passed 为 True；其他情况拒绝。"
        "status 字段或模型宣称不能代替这些绑定。",
        {
            "review.py": code("""
            def accept(request, report):
                return bool(report['passed'])
        """),
            "request.py": code("""
            def request(artifact_id, patch_sha, target, verifier_digest):
                return dict(artifact_id=artifact_id, patch_sha=patch_sha,
                            target=target, verifier_digest=verifier_digest)
        """),
            "report.py": code("""
            def result(request, passed):
                return {**request, 'passed': passed}
        """),
        },
        code("""
            from request import request
            from report import result
            from review import accept
            request = request('a', 'sha', 'target', 'verifier')
            report = result(request, True)
            assert accept(request, report) is True
            for field in request:
                assert accept(request, {**report, field: 'other'}) is False
            assert accept(request, {**report, 'passed': False}) is False
            assert accept(request, {**report, 'passed': 'true'}) is False
        """),
        {
            "review.py": code("""
            def accept(request, report):
                return report.get('passed') is True and all(
                    report.get(key) == request[key]
                    for key in ('artifact_id', 'patch_sha', 'target', 'verifier_digest'))
        """)
        },
        split="holdout",
    ),
    Case(
        "skill-resource-revision",
        "independent",
        ("src/traceh/session/skill_retrieval.py", "src/traceh/plugins/skills.py"),
        "Skill 卸载或更新后，旧目录引用仍能读取资源。read(catalog, reference) 必须核对插件仍活跃、"
        "version 与 catalog_digest 都与引用匹配，再返回该资源；缺失插件或资源、旧版本、旧目录都报 ValueError。",
        {
            "reader.py": code("""
            from catalog import lookup
            def read(catalog, reference):
                plugin = lookup(catalog, reference['plugin'])
                return plugin['resources'][reference['path']]
        """),
            "catalog.py": code("""
            def lookup(catalog, name):
                return catalog[name]
        """),
            "reference.py": code("""
            def reference(plugin, version, catalog_digest, path):
                return dict(plugin=plugin, version=version, catalog_digest=catalog_digest, path=path)
        """),
        },
        code("""
            from reader import read
            catalog = {'guide': {'active': True, 'version': 'v2', 'catalog_digest': 'new', 'resources': {'a': 'body'}}}
            ref = {'plugin': 'guide', 'version': 'v2', 'catalog_digest': 'new', 'path': 'a'}
            assert read(catalog, ref) == 'body'
            for field, value in [('version', 'v1'), ('catalog_digest', 'old'), ('plugin', 'missing'), ('path', 'missing')]:
                try: read(catalog, {**ref, field: value})
                except ValueError: pass
                else: raise AssertionError('stale reference accepted')
            catalog['guide']['active'] = False
            try: read(catalog, ref)
            except ValueError: pass
            else: raise AssertionError('inactive plugin accepted')
        """),
        {
            "reader.py": code("""
            from catalog import lookup
            def read(catalog, reference):
                try:
                    plugin = lookup(catalog, reference['plugin'])
                    if (plugin['active'] is not True or plugin['version'] != reference['version']
                            or plugin['catalog_digest'] != reference['catalog_digest']):
                        raise ValueError('stale')
                    return plugin['resources'][reference['path']]
                except KeyError: raise ValueError('missing') from None
        """)
        },
        split="holdout",
    ),
    Case(
        "atomic-delivery-claim",
        "dependent",
        ("src/traceh/agents/inbox_service.py", "src/traceh/supervision/delivery.py"),
        "两个协程可能同时领取一条消息。Queue.claim 在存在 await 的检查路径上仍须保证同一消息"
        "只有一个调用返回 True；另一个返回 False。消息未存在同样 False，领取后状态为 claimed。"
        "保留异步 checkpoint 注入以便验证并发，不要删除它或用固定 sleep。",
        {
            "queueing.py": code("""
            class Queue:
                def __init__(self, ids): self.states = {key: 'pending' for key in ids}
                async def claim(self, key, checkpoint):
                    if self.states.get(key) != 'pending': return False
                    await checkpoint()
                    self.states[key] = 'claimed'
                    return True
        """)
        },
        code("""
            import asyncio
            from queueing import Queue
            async def check():
                queue = Queue(['one'])
                entered, release = asyncio.Event(), asyncio.Event()
                async def checkpoint():
                    entered.set()
                    await release.wait()
                first = asyncio.create_task(queue.claim('one', checkpoint))
                await entered.wait()
                second = asyncio.create_task(queue.claim('one', checkpoint))
                release.set()
                results = await asyncio.wait_for(asyncio.gather(first, second), 1)
                assert sorted(results) == [False, True]
                assert queue.states['one'] == 'claimed'
                assert not await queue.claim('absent', checkpoint)
            asyncio.run(check())
        """),
        {
            "queueing.py": code("""
            import asyncio
            class Queue:
                def __init__(self, ids):
                    self.states = {key: 'pending' for key in ids}
                    self.lock = asyncio.Lock()
                async def claim(self, key, checkpoint):
                    async with self.lock:
                        if self.states.get(key) != 'pending': return False
                        await checkpoint()
                        self.states[key] = 'claimed'
                        return True
        """)
        },
        split="holdout",
    ),
    Case(
        "frozen-request-retry",
        "dependent",
        ("src/traceh/llm/retry.py",),
        "瞬时失败重试时重新生成 request，导致一次重试变成了不同请求。run(build, send, attempts) "
        "应只 build 一次，所有 send 收到同一请求对象；仅重试 Transient，次数耗尽重抛最后异常，"
        "其他错误立即传播。不要因重试重复构造请求或吞掉取消。",
        {
            "retrying.py": code("""
            class Transient(Exception): pass
            async def run(build, send, attempts):
                for index in range(attempts):
                    try: return await send(build())
                    except Transient:
                        if index + 1 == attempts: raise
        """)
        },
        code("""
            import asyncio
            from retrying import run, Transient
            async def check():
                built, sent = [], []
                def build():
                    value = {'id': len(built)}
                    built.append(value)
                    return value
                async def send(value):
                    sent.append(value)
                    if len(sent) < 3: raise Transient('retry')
                    return 'ok'
                assert await run(build, send, 3) == 'ok'
                assert len(built) == 1 and all(value is built[0] for value in sent)
                error = ValueError('permanent')
                calls = []
                async def fail(value):
                    calls.append(value)
                    raise error
                try: await run(build, fail, 3)
                except ValueError as caught: assert caught is error
                else: raise AssertionError('permanent error swallowed')
                assert len(calls) == 1
            asyncio.run(check())
        """),
        {
            "retrying.py": code("""
            class Transient(Exception): pass
            async def run(build, send, attempts):
                request = build()
                for index in range(attempts):
                    try: return await send(request)
                    except Transient:
                        if index + 1 == attempts: raise
        """)
        },
        split="holdout",
    ),
]
