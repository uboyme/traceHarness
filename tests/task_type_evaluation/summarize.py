"""Per-arm facts for experiments A/B, read only from each run's original report.

Nothing is re-scored: success is the Product report's own four-fact success,
tokens are provider-reported whole-tree totals (unknown stays unknown), and the
context columns are the S0-A diagnostics already in the report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def arm_rows(output):
    output = Path(output)
    plan = json.loads((output / "experiment.json").read_bytes())
    rows = []
    for arm in plan["arms"]:
        report_path = output / arm["directory"] / "run" / "report.json"
        row = {"plan": output.name, "variant": arm["variant_id"], "ran": report_path.is_file()}
        if not row["ran"]:
            rows.append(row)
            continue
        report = json.loads(report_path.read_bytes())
        for attempt in report["task_report"]["attempts"]:
            evidence = attempt["evidence"] or {}
            execution = evidence.get("execution") or {}
            sessions = execution.get("sessions") or []
            tokens = execution.get("tokens")
            rows.append(
                {
                    **row,
                    "case_id": attempt["benchmark_task_id"],
                    "mode": attempt["requested_mode"],
                    "resolved": evidence.get("resolved_mode"),
                    "success": attempt["success"],
                    "review_passed": evidence.get("review_passed"),
                    "failure_code": evidence.get("failure_code") or attempt.get("error_code"),
                    "wall_ms": (attempt.get("timing") or {}).get("wall_ms"),
                    "total_tokens": None if tokens is None else tokens["total_tokens"],
                    "token_quality": None if tokens is None else tokens["quality"],
                    "model_attempts": execution.get("model_attempts"),
                    "tool_calls": execution.get("tool_calls"),
                    "children": sum(1 for s in sessions if s.get("label", "coder") != "coder"),
                    "sessions": [
                        {
                            "label": s.get("label"),
                            "tokens": None
                            if s.get("tokens") is None
                            else s["tokens"]["total_tokens"],
                            "context": s.get("context"),
                        }
                        for s in sessions
                    ],
                }
            )
    return rows


def main(outputs, destination):
    rows = [row for output in outputs for row in arm_rows(output)]
    Path(destination).write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", "utf-8")
    for row in rows:
        if not row["ran"]:
            print(row["plan"], row["variant"], "not run")
            continue
        peaks = [
            (
                s["label"],
                (s["context"] or {}).get("input_peak"),
                (s["context"] or {}).get("tool_folds"),
            )
            for s in row["sessions"]
        ]
        print(
            row["case_id"],
            row["mode"],
            "success" if row["success"] else "fail",
            row["total_tokens"],
            row["wall_ms"],
            "children",
            row["children"],
            peaks,
        )
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outputs", nargs="+", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    options = parser.parse_args()
    main(options.outputs, options.destination)
