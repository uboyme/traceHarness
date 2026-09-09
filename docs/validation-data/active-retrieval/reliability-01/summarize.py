"""Offline evidence rollup; semantic decisions come from separate human reviews."""

import hashlib
import json
import statistics
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(root):
    stages = []
    for execution_path in sorted(root.glob("*/execution.json")):
        folder = execution_path.parent
        execution = read(execution_path)
        frozen = read(folder / "frozen.json")
        review = read(folder / "manual-review.json")
        cases = []
        for run in execution["runs"]:
            case = folder / run["arm"] / run["identity"]
            report = read(case / "report.json")
            fixture = read(case / "fixture.json")
            assert fixture == next(
                f for f in frozen["fixtures"] if f["identity"] == run["identity"]
            )
            events = read(case / "source-events.json")
            effects = read(case / "effect-events.json")
            transport = read(case / "transport.json")
            target = [e for e in events if e["seq"] > report["target_start_seq"]]
            attempts = [e for e in events if e["type"] == "model/attempt-end"]
            tools = [e for e in target if e["type"] == "tool/result"]
            calls = report.get("tool_calls", [])
            source_outcomes = [
                e["data"]
                for e in effects
                if e["type"] == "effect/outcome" and "retained_output" in e["data"]
            ]
            assert not report.get("replay_errors") and not report.get("invariant_errors")
            assert not report.get("audit_error")
            assert transport["opener_restored"]
            assert all(
                c["loopback"] is False and c["port"] == 443 for c in transport["connections"]
            )
            assert not fixture["control"] or not calls
            if fixture["family"] == "output":
                assert report["output_executions"] == 1 and len(source_outcomes) == 1
            cases.append(
                {
                    "arm": run["arm"],
                    "identity": run["identity"],
                    "closed": report["closed"],
                    "error": report.get("error"),
                    "steps": report["steps"],
                    "tool_calls": len(calls),
                    "search_read_count": report["search_read_count"],
                    "query_terms": sum(
                        len(c["arguments"].get("queries", [])) or int("query" in c["arguments"])
                        for c in calls
                    ),
                    "target_tool_result_utf8_bytes": sum(
                        len(e["data"]["content"].encode("utf-8")) for e in tools
                    ),
                    "target_seconds": report["target_seconds"],
                    "all_usage": report["all_usage"],
                    "target_usage": report["target_usage"],
                    "failed_attempts": sum(e["data"]["status"] != "succeeded" for e in attempts),
                    "request_snapshots": sum(e["type"] == "request/snapshot" for e in events),
                    "source_output_digest": source_outcomes[0]["output_ref"]["digest"]
                    if fixture["family"] == "output"
                    else None,
                    "source_events_sha256": sha(case / "source-events.json"),
                    "effect_events_sha256": sha(case / "effect-events.json"),
                }
            )
        paired = []
        for fixture in frozen["fixtures"]:
            matches = [c for c in cases if c["identity"] == fixture["identity"]]
            if len(matches) == 2:
                assert matches[0]["source_output_digest"] == matches[1]["source_output_digest"]
                paired.append(fixture["identity"])
        arms = {}
        for arm in ("baseline", "candidate"):
            selected = [c for c in cases if c["arm"] == arm]
            reopen = read(folder / (arm + "-reopen.json"))
            assert reopen["cases"] == len(selected)
            assert all(
                not c["replay_errors"]
                and not c["invariant_errors"]
                and c["attempts_started"] == c["attempts_ended"]
                for c in reopen["results"]
            )
            arms[arm] = {
                "runs": len(selected),
                "joint_pass": review["scores"][arm],
                "target_tokens": sum(c["target_usage"]["total_tokens"] for c in selected),
                "all_tokens": sum(c["all_usage"]["total_tokens"] for c in selected),
                "unknown_usage_attempts": sum(c["all_usage"]["unknown_usage"] for c in selected),
                "estimated_usage_attempts": sum(
                    c["all_usage"]["estimated_usage"] for c in selected
                ),
                "all_attempts": sum(c["all_usage"]["attempts"] for c in selected),
                "failed_attempts": sum(c["failed_attempts"] for c in selected),
                "request_snapshots": sum(c["request_snapshots"] for c in selected),
                "tool_calls": sum(c["tool_calls"] for c in selected),
                "median_target_seconds": statistics.median(c["target_seconds"] for c in selected),
            }
        stages.append(
            {
                "stage": folder.name,
                "arms": arms,
                "paired": len(paired),
                "planned": execution["planned"],
                "unrun": execution["unrun"],
                "complete": execution["complete"],
                "decision": review["decision"],
                "cases": cases,
            }
        )
    output = {
        "method": "Read-only rollup; human scores are not recomputed by answer matching.",
        "tool_result_bytes_note": "Returned Tool receipts/content only; not total request tokens."
        " Context-delivered reference bodies are measured by Provider usage instead.",
        "stages": stages,
    }
    (root / "summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps([{"stage": s["stage"], "arms": s["arms"]} for s in stages], indent=2))


if __name__ == "__main__":
    summarize(Path(__file__).resolve().parent)
