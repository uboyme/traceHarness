"""One explicit two-assistant fixture; no production defaults.

Three independently specified modules: two are allocated to writable assistants
with disjoint paths, and the main agent owns the third plus integration and
verification. The requirement names the required number of assistants, so this
measures the mechanism, never a model's spontaneous preference.
"""

EDITABLE = ("telemetry_rules.py", "telemetry_format.py", "telemetry_report.py")
REQUIREMENT = (
    "Implement the telemetry report specified in INDEX.md. This acceptance explicitly requires "
    "two writable assistants in one plan: one implements telemetry_rules.py and the other "
    "implements telemetry_format.py. Give each assignment its own exact path; the two path sets "
    "must not overlap. The main agent owns telemetry_report.py, the integration of both returned "
    "Patches, and the final verification. INDEX.md specifies all three modules completely, so "
    "telemetry_report.py can be written from that specification alone while the assistants work. "
    "Read each returned Patch completely and integrate both explicitly before final verification. "
    "Only modify those three Python files; preserve all specifications, add no dependencies or "
    "files. Explain the checks you actually ran; do not claim checks you did not run."
)
FILES = {
    ".gitattributes": "* -text\n",
    ".gitignore": "__pycache__/\n",
    "INDEX.md": "# Telemetry batch report\n"
    "telemetry_rules.normalize(rows) accepts a list of dicts with exactly device/value keys.\n"
    "device must be a string with nonempty strip() result; normalize with strip().lower().\n"
    "value must be an int excluding bool, in inclusive range -1000..1000.\n"
    "Invalid shape/type/range raises ValueError, including an invalid late row.\n"
    "Return fresh dicts in input order without mutating inputs. Empty list is valid.\n"
    "\n"
    "telemetry_format.render(summary) accepts a list of dicts with exactly "
    "device/count/total/above keys.\n"
    "device must be a nonempty string; count/total/above must be ints excluding bool.\n"
    "count and above must be >= 0. Invalid shape/type/value raises ValueError.\n"
    "Return one string per row, formatted exactly as "
    "'<device>: count=<count> total=<total> above=<above>'.\n"
    "Return [] for an empty list. Do not mutate the input.\n"
    "\n"
    "telemetry_report.summarize(rows, threshold) must call telemetry_rules.normalize(rows).\n"
    "threshold must be an int excluding bool; any integer is valid.\n"
    "Return one dict per normalized device sorted by device, "
    "keys exactly device/count/total/above.\n"
    "count counts all its rows, total sums values, above counts values strictly > threshold.\n"
    "Return [] for an empty valid list. Do not mutate inputs or retain state across calls.\n"
    "telemetry_report.report(rows, threshold) must return "
    "telemetry_format.render(summarize(rows, threshold)).\n",
    "telemetry_rules.py": "def normalize(rows):\n    return None\n",
    "telemetry_format.py": "def render(summary):\n    return None\n",
    "telemetry_report.py": (
        "def summarize(rows, threshold):\n    return None\n\n\n"
        "def report(rows, threshold):\n    return None\n"
    ),
}
REFERENCE = {
    "telemetry_rules.py": """def normalize(rows):
    if type(rows) is not list:
        raise ValueError("rows")
    result = []
    for row in rows:
        if type(row) is not dict or set(row) != {"device", "value"}:
            raise ValueError("row")
        device, value = row["device"], row["value"]
        if not isinstance(device, str) or not device.strip():
            raise ValueError("device")
        if type(value) is not int or not -1000 <= value <= 1000:
            raise ValueError("value")
        result.append({"device": device.strip().lower(), "value": value})
    return result
""",
    "telemetry_format.py": """def render(summary):
    if type(summary) is not list:
        raise ValueError("summary")
    lines = []
    for row in summary:
        if type(row) is not dict or set(row) != {"device", "count", "total", "above"}:
            raise ValueError("row")
        device = row["device"]
        if not isinstance(device, str) or not device:
            raise ValueError("device")
        for key in ("count", "total", "above"):
            if type(row[key]) is not int:
                raise ValueError(key)
        if row["count"] < 0 or row["above"] < 0:
            raise ValueError("negative")
        lines.append(
            f"{device}: count={row['count']} total={row['total']} above={row['above']}"
        )
    return lines
""",
    "telemetry_report.py": """import telemetry_format
import telemetry_rules


def summarize(rows, threshold):
    if type(threshold) is not int:
        raise ValueError("threshold")
    grouped = {}
    for row in telemetry_rules.normalize(rows):
        key, value = row["device"], row["value"]
        bucket = grouped.setdefault(key, {"device": key, "count": 0, "total": 0, "above": 0})
        bucket["count"] += 1
        bucket["total"] += value
        bucket["above"] += int(value > threshold)
    return [grouped[key] for key in sorted(grouped)]


def report(rows, threshold):
    return telemetry_format.render(summarize(rows, threshold))
""",
}
CHECKS = """import copy
from telemetry_format import render
from telemetry_report import report, summarize
from telemetry_rules import normalize
checks = 0
def check(value):
    global checks
    assert value
    checks += 1
def rejects(fn, *args):
    try:
        fn(*args)
    except ValueError:
        check(True)
    else:
        raise AssertionError("invalid input accepted")
rows = [{"device":" B ", "value":3}, {"device":"a", "value":-2},
        {"device":"b", "value":5}, {"device":" A", "value":0}]
saved = copy.deepcopy(rows)
normal = normalize(rows)
check(normal == [{"device":"b","value":3}, {"device":"a","value":-2},
                 {"device":"b","value":5}, {"device":"a","value":0}])
check(all(a is not b for a,b in zip(rows,normal)))
check(summarize(rows,3) == [{"device":"a","count":2,"total":-2,"above":0},
                          {"device":"b","count":2,"total":8,"above":1}])
check(rows == saved)
check(normalize([]) == [] and summarize([],0) == [])
check(render([]) == [] and report([],0) == [])
check(report(rows,3) == ["a: count=2 total=-2 above=0", "b: count=2 total=8 above=1"])
summary = summarize(rows,3)
kept = copy.deepcopy(summary)
check(render(summary) == ["a: count=2 total=-2 above=0", "b: count=2 total=8 above=1"])
check(summary == kept)
for bad in (None, {}, (), [None], [{}], [{"device":"a","value":0,"x":1}],
            [{"device":" ","value":0}], [{"device":1,"value":0}],
            [{"device":"a","value":True}], [{"device":"a","value":1.0}],
            [{"device":"a","value":1001}], [{"device":"a","value":-1001}]):
    rejects(normalize,bad)
    rejects(summarize,bad,0)
rejects(normalize, rows + [{"device":"z","value":None}])
for bad in (True, 0.0, None, "0"):
    rejects(summarize, rows,bad)
    rejects(report, rows,bad)
for bad in (None, {}, (), [None], [{}], ["a"],
            [{"device":"a","count":1,"total":1,"above":0,"x":1}],
            [{"device":"","count":1,"total":1,"above":0}],
            [{"device":1,"count":1,"total":1,"above":0}],
            [{"device":"a","count":True,"total":1,"above":0}],
            [{"device":"a","count":1.0,"total":1,"above":0}],
            [{"device":"a","count":-1,"total":1,"above":0}],
            [{"device":"a","count":1,"total":1,"above":-1}]):
    rejects(render,bad)
for threshold in (-1001,-1000,0,1000,1001):
    check(summarize([{"device":"X","value":-1000},{"device":"x","value":1000}],threshold)
          == [{"device":"x","count":2,"total":0,
               "above":int(-1000>threshold)+int(1000>threshold)}])
check(summarize(list(reversed(rows)),3) == summarize(rows,3))
print("fixed_functional_checks",checks)
"""
