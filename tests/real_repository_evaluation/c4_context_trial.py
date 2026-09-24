"""C4: one real trial of the 080 C0-C3 changes, through the original CLI.

This drives `traceh eval` exactly as the pilot did - no parallel runner, no new
task state, no second ledger. What it adds is the derived read-out the trial is
for: where the output budget went, whether folding actually engaged, and what
the assistant really handed back.

The conditions, the disclosed deviations from v10 and the stop rules are frozen
in `c4-preregistration.json`, and this script refuses to run if that file is
missing. Every number it prints comes from the durable run evidence, never from
a model's own account of what it did.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.evaluators.product_review import trial_events
from traceh.evaluation.evidence import load_run

PREREGISTRATION = Path("docs/validation-data/real-repository-pilot-v1/c4-preregistration.json")


def _session_readout(events: list[dict]) -> dict:
    """Per-stream facts, taken from the events rather than from any summary."""

    attempts = [e for e in events if e["type"] == "model/attempt-end"]
    succeeded = [e for e in attempts if e["data"].get("status") == "succeeded"]
    usages = [e["data"].get("usage") or {} for e in succeeded]
    exact = [u for u in usages if u.get("quality") == "exact"]
    reasoning = [u["reasoning_tokens"] for u in exact if u.get("reasoning_tokens") is not None]
    folds = [
        e
        for e in events
        if e["type"] == "surface/replace" and e["data"].get("method") == "tool-fold"
    ]
    step_folds = [f for f in folds if f["data"].get("boundary", {}).get("unit") == "step"]
    measurements = [
        e["data"]["measurement"] for e in events if e["type"] == "request/token-measurement"
    ]
    read_backs = [
        e
        for e in events
        if e["type"] == "tool/result"
        and e["data"].get("tool_name")
        in ("read_tool_output", "list_tool_outputs", "search_tool_output")
    ]
    fold_failures = [
        e["data"]
        for e in events
        if e["type"] == "surface/compaction-failed" and e["data"].get("method") == "step-fold"
    ]
    return {
        "model_calls": len(attempts),
        "succeeded_calls": len(succeeded),
        "exact_usage_calls": len(exact),
        "unknown_usage_calls": len(usages) - len(exact),
        "input_tokens": sum(u.get("input_tokens", 0) for u in exact),
        "output_tokens": sum(u.get("output_tokens", 0) for u in exact),
        # The C0-2 question, now measured on a real task rather than one probe.
        "reasoning_tokens_reported_calls": len(reasoning),
        "reasoning_tokens": sum(reasoning),
        "completion_categories": sorted(
            {e["data"].get("completion") for e in attempts if e["data"].get("completion")}
        ),
        "largest_estimated_input": max(
            (m["input_tokens"] for m in measurements), default=None
        ),
        "estimated_inputs": [m["input_tokens"] for m in measurements],
        "trigger_tokens": next((m["trigger_tokens"] for m in measurements), None),
        "relief_tokens": next((m.get("relief_tokens") for m in measurements), None),
        "replacements": len(folds),
        "step_folds": len(step_folds),
        "read_back_calls": len(read_backs),
        "step_fold_failures": fold_failures,
        # Folding is only a saving if the model reopens evidence through the
        # read-back tools. If it re-runs the original tool instead, the Session
        # pays for the same bytes twice and the fold bought nothing.
        **_fold_induced_rereads(events),
    }


def _fold_induced_rereads(events: list[dict]) -> dict:
    """Count tool calls that re-run the original tool after its result was folded.

    This replaces the stop condition this trial was preregistered with. That one
    watched for the *same reference* being folded, read back and folded again -
    and measured zero, because that is not how the waste actually happens. What
    happens is that a folded result is re-obtained by **re-running the original
    tool**: the answer arrives as a brand new Effect with a new reference, so a
    detector looking at fold identity is structurally blind to it.

    The comparison is on tool name plus exact arguments, so paging through a
    large output is not counted - only asking for bytes the Session already has.
    """

    folded_at: dict[int, int] = {}
    for event in events:
        if event["type"] == "surface/replace" and event["data"].get("method") == "tool-fold":
            for source in event["data"]["source_seqs"]:
                folded_at[source] = event["seq"]
    result_seq = {
        event["data"]["tool_call_id"]: event["seq"]
        for event in events
        if event["type"] == "tool/result"
    }

    seen: dict[tuple, str] = {}
    after_fold = 0
    while_still_visible = 0
    for event in sorted(events, key=lambda item: item["seq"]):
        if event["type"] != "tool/call":
            continue
        data = event["data"]
        key = (data.get("tool_name"), json.dumps(data.get("arguments"), sort_keys=True))
        previous = seen.get(key)
        if previous is not None:
            folded = folded_at.get(result_seq.get(previous, -1))
            if folded is not None and folded < event["seq"]:
                after_fold += 1
            else:
                while_still_visible += 1
        seen[key] = data["tool_call_id"]
    return {
        "identical_recalls_after_their_result_was_folded": after_fold,
        "identical_recalls_while_result_still_visible": while_still_visible,
    }


def _no_fold_reason(readout: dict) -> str:
    """Say which of the four explanations applies, rather than implying success."""

    if readout["step_folds"]:
        return "folded"
    if readout["step_fold_failures"]:
        return "preparation-failed"
    if readout["relief_tokens"] is None:
        return "disabled-no-water-marks-configured"
    if readout["largest_estimated_input"] is None:
        return "no-measurement-recorded"
    if readout["largest_estimated_input"] < (readout["trigger_tokens"] or 0):
        return "never-reached-the-high-water-mark"
    return "reached-the-mark-but-found-no-safe-candidate"


def _frozen_conditions() -> dict:
    """Read the preregistration before anything is spent, never after."""

    if not PREREGISTRATION.is_file():
        raise SystemExit("refusing to run: the preregistration is missing")
    return json.loads(PREREGISTRATION.read_text(encoding="utf-8"))


async def main(args: argparse.Namespace, prereg: dict) -> int:
    print(
        json.dumps({"event": "trial-starting", "case": prereg["case"]["case_id"]}),
        flush=True,
    )

    cli = build_parser().parse_args(
        [
            "eval",
            str(args.material),
            "--output",
            str(args.output),
            "--env-file",
            str(args.env_file),
            "--provider",
            prereg["model"]["provider_id"],
            "--model",
            prereg["model"]["model_id"],
            "--sandbox-config",
            str(args.sandbox_config),
            "--eval-timeout-seconds",
            str(args.timeout_seconds),
            # The default retry budget is written for an interactive Session and
            # is wrong for a forty-minute trial. Several runs died to a single
            # network hiccup: this provider takes ten to sixty seconds to fail a
            # handshake, so an elapsed cap of thirty seconds delivered two of
            # its three attempts and a cap of 180 delivered three of five.
            # Retry earns its keep here - it recovers most failing requests, and
            # several needed a third or fourth attempt. Widened here rather than
            # in the shipped default because an interactive Session should not
            # stall ten minutes on a handshake; the tradeoff is the host's to
            # make per run.
            "--model-retry-max-attempts",
            str(prereg["model"]["retry"]["max_attempts"]),
            "--model-retry-max-elapsed-seconds",
            str(prereg["model"]["retry"]["max_elapsed_seconds"]),
            # The wrap-up Step produces the longest single output of the whole
            # Turn. One trial's report took 71 seconds while this provider was
            # fast; when it later slowed to 2.3x its median the same report ran
            # past the 120-second default, timed out, and the assistant
            # delivered nothing. Stated here rather than shipped as a new
            # default: an interactive Session should not hang this long.
            "--model-timeout-seconds",
            str(prereg["model"]["request_timeout_seconds"]),
        ]
    )
    _configure_from_environment(cli)
    code = await _eval(cli)
    print(json.dumps({"event": "cli-finished", "exit_code": code}), flush=True)

    # Read the durable evidence back, whatever the exit code was. A failed run
    # still has to say where its tokens went and whether folding engaged; that
    # is precisely the information a silent failure used to cost us.
    _, report, _ = load_run(args.output)
    readouts = {}
    for trial in report.get("trials", []):
        try:
            _, events = trial_events(args.output, trial)
        except Exception as error:  # evidence that will not open is itself a finding
            readouts[trial["identity"]["trial_id"]] = {"evidence_error": type(error).__name__}
            continue
        by_stream: dict[str, list[dict]] = {}
        for event in events:
            by_stream.setdefault(str(event.get("stream_id")), []).append(event)
        for stream, stream_events in by_stream.items():
            if not stream.startswith("session:"):
                continue
            readout = _session_readout(stream_events)
            if readout["model_calls"]:
                readout["folding_outcome"] = _no_fold_reason(readout)
                readouts[f"{trial['identity']['case_id']}/{stream}"] = readout

    evidence = {
        "kind": "c4-context-governance-trial",
        "exit_code": code,
        "complete": report.get("complete"),
        "errors": report.get("errors"),
        "trials": [
            {
                "case_id": t["identity"].get("case_id"),
                "execution": t.get("execution"),
                "assessment": t.get("assessment"),
                "convergence": t.get("convergence"),
                "invariants": t.get("invariants"),
                "reason": t.get("reason"),
            }
            for t in report.get("trials", [])
        ],
        "per_session": readouts,
        "interpretation_limits": prereg["interpretation_limits"],
        "disclosed_condition_changes_from_v10": [
            change["change"] for change in prereg["disclosed_condition_changes_from_v10"]
        ],
    }
    # One file per run directory. A fixed name silently overwrote the first
    # trial's evidence when a second one was launched, which is exactly the
    # kind of quiet loss this project does not accept.
    destination = Path(
        "docs/validation-data/real-repository-pilot-v1"
    ) / f"c4-context-governance-trial-{args.output.name}.json"
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    await asyncio.to_thread(destination.write_text, rendered, encoding="utf-8")
    print(rendered, flush=True)
    print(f"written: {destination}", flush=True)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--sandbox-config", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=5400.0)
    _arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(_arguments, _frozen_conditions())))
