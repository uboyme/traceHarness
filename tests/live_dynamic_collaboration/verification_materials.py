"""One new, explicit review-checkpoint fixture; never a runtime default."""

REQUIREMENT = (
    "实现 coverage_windows.py 的 coverage(windows, horizon)。先读 INDEX.md，按两份规格"
    "完成校验、裁剪和覆盖区间合并。只修改 coverage_windows.py，保留接口，不改规格，"
    "不新增文件或安装依赖。最终说明实现与实际验证结果，未运行的检查不要声称已运行。"
)
FILES = {
    ".gitignore": "__pycache__/\n",
    "INDEX.md": "# Coverage contract\n"
    "validation.md: input validity and ownership\n"
    "intervals.md: clipping and merging\n"
    "coverage_windows.py: implementation target\n",
    "validation.md": "# Validation\n"
    "horizon must be a positive int, excluding bool. windows must be a list.\n"
    "Each item must be a list of exactly two ints, excluding bool, with start < end.\n"
    "Negative endpoints are valid. Invalid input must raise ValueError.\n"
    "Validate every item before discarding out-of-range intervals.\n"
    "Do not mutate input lists. Return a new list containing fresh two-element lists.\n",
    "intervals.md": "# Coverage\n"
    "Intervals are half-open [start, end). Clip each to [0, horizon).\n"
    "Discard empty clipped intervals. Merge overlapping OR touching intervals.\n"
    "Return merged intervals sorted by start. Results must not depend on input order.\n"
    "Duplicates and nested intervals do not add separate output entries. Empty input returns [].\n",
    "coverage_windows.py": "def coverage(windows, horizon):\n    return None\n",
}

REFERENCE = """def coverage(windows, horizon):
    if type(horizon) is not int or horizon <= 0 or type(windows) is not list:
        raise ValueError('input')
    parts = []
    for item in windows:
        if type(item) is not list or len(item) != 2:
            raise ValueError('shape')
        a, b = item
        if type(a) is not int or type(b) is not int or a >= b:
            raise ValueError('endpoint')
        a, b = max(0, a), min(horizon, b)
        if a < b:
            parts.append([a, b])
    out = []
    for a, b in sorted(parts):
        if out and a <= out[-1][1]:
            out[-1][1] = max(b, out[-1][1])
        else:
            out.append([a, b])
    return out
"""

CHECKS = """import copy,itertools
from coverage_windows import coverage
passed = 0
def check(items, horizon, want):
 global passed
 before = copy.deepcopy(items)
 result = coverage(items, horizon)
 assert result == want, (result, want)
 assert items == before and result is not items
 assert all(out is not item for out in result for item in items)
 passed += 1
check([], 8, [])
check([[-4,2],[6,12]], 8, [[0,2],[6,8]])
check([[-4,0],[8,12]], 8, [])
check([[1,7],[2,3],[1,7]], 8, [[1,7]])
check([[0,1],[2,3]], 8, [[0,1],[2,3]])
for items in itertools.permutations([[1,3],[3,5],[4,7]]):
 check(list(items), 8, [[1,7]])
invalid = [(None,8), ((),8), ([],True), ([],0), ([],1.5),
           ([[2,2]],8), ([[4,2]],8), ([[True,3]],8), ([[1,False]],8),
           ([[1,2,3]],8), ([(1,2)],8), ([[1,'2']],8),
           ([[9,8]],8), ([[1,2],[-3,-4]],8)]
for items,horizon in invalid:
 try: coverage(items,horizon)
 except ValueError: passed += 1
 else: raise AssertionError(('expected ValueError',items,horizon))
print(str(passed)+' functional cases and source boundaries passed')
"""
