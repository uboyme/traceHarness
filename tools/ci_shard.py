"""Split the test files across CI shards, balanced by measured duration.

Sharding is by *file*, not by test. A file keeps its module and class fixtures
inside one job, and each shard runs its own pytest serially, so nothing here
makes two tests share a database, a worktree or a process slot. That is the
difference from `-n auto`, which AGENTS.md rules out for this suite.

`ci_durations.json` holds one measured wall time per test file. A file the
table does not know about is treated as the median, which keeps a newly added
file from silently landing in the heaviest shard; the balance degrades slowly
as the table ages and is corrected by regenerating it.

Usage: python tools/ci_shard.py <shard-number> <shard-count>
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = Path(__file__).resolve().parent / "ci_durations.json"


def shard_files(shard: int, count: int) -> list[str]:
    if not 1 <= shard <= count:
        raise SystemExit(f"shard {shard} is outside 1..{count}")
    files = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")
    )
    if not files:
        raise SystemExit("no test files found")
    known: dict[str, float] = json.loads(DURATIONS.read_text(encoding="utf-8"))
    default = statistics.median(known.values()) if known else 1.0
    weights = {name: known.get(name, default) for name in files}
    # Longest first, each file to the lightest shard: with the heaviest file far
    # below an even share, this keeps the slowest shard close to the average.
    buckets: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for name in sorted(files, key=lambda name: (-weights[name], name)):
        target = totals.index(min(totals))
        buckets[target].append(name)
        totals[target] += weights[name]
    return sorted(buckets[shard - 1])


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: ci_shard.py <shard-number> <shard-count>")
    print(" ".join(shard_files(int(sys.argv[1]), int(sys.argv[2]))))


if __name__ == "__main__":
    main()
