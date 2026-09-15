"""One explicit decomposition-granularity fixture; no production defaults.

INDEX.md specifies four modules independently plus one that composes them. The
requirement states that property as a fact and **never names a count**, so the
number of assignments is the model's own judgment inside the host ceiling.
"""

EDITABLE = (
    "telemetry_parse.py",
    "telemetry_rules.py",
    "telemetry_format.py",
    "telemetry_stats.py",
    "telemetry_report.py",
)
REQUIREMENT = (
    "Implement the telemetry pipeline specified in INDEX.md. Each module section in "
    "INDEX.md is a complete specification on its own: a module can be written from its own "
    "section without reading any other module's code, and a stated call between modules is "
    "an interface contract, not a dependency on someone else's work in progress. "
    "The main agent owns telemetry_report.py, the integration of every returned Patch, and "
    "the final verification. Allocate the work as you judge appropriate; give each "
    "assignment its own exact relative file paths and do not let two assignments touch the "
    "same file. Read each returned Patch completely and integrate it explicitly before "
    "final verification. Only modify the five specified Python files; preserve all "
    "specifications, add no dependencies or files. Explain the checks you actually ran; do "
    "not claim checks you did not run."
)
FILES = {
    ".gitattributes": "* -text\n",
    ".gitignore": "__pycache__/\n",
    "INDEX.md": "# Telemetry pipeline\n"
    "\n"
    "## telemetry_parse.parse(lines)\n"
    "lines must be a list of strings shaped exactly 'device=value'.\n"
    "device is the text before the first '='; value is the text after it.\n"
    "Return one dict per line with exactly device/value keys, in input order.\n"
    "value must parse as a base-10 int, optionally signed; otherwise raise ValueError.\n"
    "A line without '=', with an empty device, or that is not a string raises ValueError.\n"
    "An empty list returns []. Do not mutate the input.\n"
    "\n"
    "## telemetry_rules.normalize(rows)\n"
    "rows must be a list of dicts with exactly device/value keys.\n"
    "device must be a string with nonempty strip() result; normalize with strip().lower().\n"
    "value must be an int excluding bool, in inclusive range -1000..1000.\n"
    "Invalid shape/type/range raises ValueError, including an invalid late row.\n"
    "Return fresh dicts in input order without mutating inputs. Empty list is valid.\n"
    "\n"
    "## telemetry_format.render(summary)\n"
    "summary must be a list of dicts with exactly device/count/total/above keys.\n"
    "device must be a nonempty string; count/total/above must be ints excluding bool.\n"
    "count and above must be >= 0. Invalid shape/type/value raises ValueError.\n"
    "Return one string per row, formatted exactly as "
    "'<device>: count=<count> total=<total> above=<above>'.\n"
    "Return [] for an empty list. Do not mutate the input.\n"
    "\n"
    "## telemetry_stats.summarize(rows, threshold)\n"
    "summarize must call telemetry_rules.normalize(rows) to validate and normalize.\n"
    "threshold must be an int excluding bool; any integer is valid.\n"
    "Return one dict per normalized device sorted by device, "
    "keys exactly device/count/total/above.\n"
    "count counts all its rows, total sums values, above counts values strictly > threshold.\n"
    "Return [] for an empty valid list. Do not mutate inputs or retain state across calls.\n"
    "\n"
    "## telemetry_report.report(lines, threshold)\n"
    "report must return telemetry_format.render(\n"
    "    telemetry_stats.summarize(telemetry_parse.parse(lines), threshold))\n"
    "and must not re-implement any of those steps.\n",
    "telemetry_parse.py": "def parse(lines):\n    return None\n",
    "telemetry_rules.py": "def normalize(rows):\n    return None\n",
    "telemetry_format.py": "def render(summary):\n    return None\n",
    "telemetry_stats.py": "def summarize(rows, threshold):\n    return None\n",
    "telemetry_report.py": "def report(lines, threshold):\n    return None\n",
}
REFERENCE = {
    "telemetry_parse.py": """def parse(lines):
    if type(lines) is not list:
        raise ValueError("lines")
    result = []
    for line in lines:
        if not isinstance(line, str) or "=" not in line:
            raise ValueError("line")
        device, _, raw = line.partition("=")
        if not device:
            raise ValueError("device")
        text = raw[1:] if raw[:1] in {"+", "-"} else raw
        if not text.isdigit():
            raise ValueError("value")
        result.append({"device": device, "value": int(raw)})
    return result
""",
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
    "telemetry_stats.py": """import telemetry_rules


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
""",
    "telemetry_report.py": """import telemetry_format
import telemetry_parse
import telemetry_stats


def report(lines, threshold):
    return telemetry_format.render(
        telemetry_stats.summarize(telemetry_parse.parse(lines), threshold)
    )
""",
}
CHECKS = """import copy
from telemetry_format import render
from telemetry_parse import parse
from telemetry_report import report
from telemetry_rules import normalize
from telemetry_stats import summarize
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
lines = [" B =3", "a=-2", "b=5", " A=0"]
saved = copy.deepcopy(lines)
parsed = parse(lines)
check(parsed == [{"device":" B ","value":3}, {"device":"a","value":-2},
                 {"device":"b","value":5}, {"device":" A","value":0}])
check(lines == saved)
check(parse([]) == [])
for bad in (None, {}, (), [None], [1], ["nodelimiter"], ["=5"], ["a=x"], ["a="], ["a=1.5"]):
    rejects(parse, bad)
rows = parse(lines)
normal = normalize(rows)
check(normal == [{"device":"b","value":3}, {"device":"a","value":-2},
                 {"device":"b","value":5}, {"device":"a","value":0}])
check(all(a is not b for a,b in zip(rows,normal)))
check(normalize([]) == [] and summarize([],0) == [] and render([]) == [])
summary = summarize(rows,3)
check(summary == [{"device":"a","count":2,"total":-2,"above":0},
                  {"device":"b","count":2,"total":8,"above":1}])
kept = copy.deepcopy(summary)
check(render(summary) == ["a: count=2 total=-2 above=0", "b: count=2 total=8 above=1"])
check(summary == kept)
check(report(lines,3) == ["a: count=2 total=-2 above=0", "b: count=2 total=8 above=1"])
check(report([],0) == [])
for bad in (None, {}, (), [None], [{}], [{"device":"a","value":0,"x":1}],
            [{"device":" ","value":0}], [{"device":1,"value":0}],
            [{"device":"a","value":True}], [{"device":"a","value":1.0}],
            [{"device":"a","value":1001}], [{"device":"a","value":-1001}]):
    rejects(normalize,bad)
    rejects(summarize,bad,0)
rejects(normalize, rows + [{"device":"z","value":None}])
for bad in (True, 0.0, None, "0"):
    rejects(summarize, rows,bad)
    rejects(report, lines,bad)
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
