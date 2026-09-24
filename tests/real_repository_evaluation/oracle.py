"""Frozen verifier program, injected only into the verifier's disposable sandbox.

The model gets neither this command nor its payload. Test integrity checks do not
claim immunity to arbitrary malicious Python code modifying the test framework.
"""

import base64
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

import pytest

payload = json.loads(zlib.decompress(base64.b64decode("".join(sys.argv[1:]))))
root = Path.cwd()
for name, expected in payload["protected"].items():
    # These files are replaced by host-frozen tests below, in this disposable
    # verifier copy only. Candidate tests cannot influence that run, and adding
    # legitimate regressions to a replaced file must not invalidate a repair.
    if name in payload["test_files"]:
        continue
    path = root / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("protected test material changed: " + name)
for name, content in payload["test_files"].items():
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(content))
sys.path[:0] = [str(root / name) for name in payload["import_roots"]]
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"


class Results:
    def __init__(self):
        self.passed = set()
        self.failed = set()
        self.skipped = set()
        self.errors = set()

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.passed:
                self.passed.add(report.nodeid)
            elif report.failed:
                self.failed.add(report.nodeid)
        elif report.failed:
            self.errors.add(report.nodeid)
        if report.skipped:
            self.skipped.add(report.nodeid)


results = Results()
status = pytest.main(
    [
        "-q",
        "-o",
        "addopts=",
        "--disable-warnings",
        "-p",
        "no:cacheprovider",
        *payload["test_files"],
    ],
    plugins=[results],
)
expected = set(payload["fail_to_pass"] + payload["pass_to_pass"])
missing = expected - results.passed - results.failed - results.errors - results.skipped
report = {
    "pytest_exit": int(status),
    "passed": sorted(results.passed),
    "failed": sorted(results.failed),
    "errors": sorted(results.errors),
    "skipped": sorted(results.skipped),
    "missing": sorted(missing),
}
print("TRACEH_RR_RESULT=" + json.dumps(report, sort_keys=True), flush=True)
raise SystemExit(0 if status == 0 and not missing and expected <= results.passed else 1)
