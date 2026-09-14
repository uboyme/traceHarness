"""One explicit WC-4 business fixture; no production defaults."""

EDITABLE = ("telemetry_rules.py", "telemetry_report.py")
REQUIREMENT = (
    "Implement the telemetry report specified in INDEX.md. This acceptance explicitly requires "
    "one writable assistant to implement telemetry_rules.py while the main agent owns "
    "telemetry_report.py and final verification. Delegate the rules module with its exact path, "
    "read the returned Patch completely, explicitly integrate it, then finish the report module. "
    "Only modify those two Python files; preserve all specifications, add no dependencies/files. "
    "Explain actual checks and their results; do not claim checks you did not run."
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
    "telemetry_report.summarize(rows, threshold) must call telemetry_rules.normalize(rows).\n"
    "threshold must be an int excluding bool; any integer is valid.\n"
    "Return one dict per normalized device sorted by device, "
    "keys exactly device/count/total/above.\n"
    "count counts all its rows, total sums values, above counts values strictly > threshold.\n"
    "Return [] for an empty valid list. Do not mutate inputs or retain state across calls.\n",
    "telemetry_rules.py": "def normalize(rows):\n    return None\n",
    "telemetry_report.py": "def summarize(rows, threshold):\n    return None\n",
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
    "telemetry_report.py": """import telemetry_rules

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
}
CHECKS = """import copy
import telemetry_rules
from telemetry_rules import normalize
from telemetry_report import summarize
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
for bad in (None, {}, (), [None], [{}], [{"device":"a","value":0,"x":1}],
            [{"device":" ","value":0}], [{"device":1,"value":0}],
            [{"device":"a","value":True}], [{"device":"a","value":1.0}],
            [{"device":"a","value":1001}], [{"device":"a","value":-1001}]):
    rejects(normalize,bad)
    rejects(summarize,bad,0)
rejects(normalize, rows + [{"device":"z","value":None}])
for bad in (True, 0.0, None, "0"):
    rejects(summarize, rows,bad)
for threshold in (-1001,-1000,0,1000,1001):
    check(summarize([{"device":"X","value":-1000},{"device":"x","value":1000}],threshold)
          == [{"device":"x","count":2,"total":0,
               "above":int(-1000>threshold)+int(1000>threshold)}])
check(summarize(list(reversed(rows)),3) == summarize(rows,3))
print("fixed_functional_checks",checks)
"""
