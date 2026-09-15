"""DA-12: paired field-description probes; no production Tool changes."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration import independent_decision, phase_diagnosis
from live_dynamic_collaboration.diagnosis_audit import events_at
from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelRequest
from traceh.evaluation.inputs import digest_bytes

CONDITIONS = ("original", "described-fields")
DESCRIPTIONS = {
    "main_goal": (
        "For local, the complete work you will do yourself. For separable, the concrete "
        "independently answerable outcome you retain and can advance from the original sources "
        "while the child works. Do not restate the entire task, duplicate the child's work, "
        "or retain only waiting, supervision, final formatting or merging."
    ),
    "child_goal": (
        "Empty for local. For separable, a bounded readonly independently answerable outcome "
        "complementary to main_goal. Neither branch's substantive investigation may require the "
        "other branch's future result to begin. Shared background is allowed; do not delegate "
        "all source gathering as a prerequisite for the parent's entire analysis."
    ),
    "child_deliverable": (
        "Empty for local. For separable, a verifiable answer to the child's own question with "
        "source evidence and any unresolved limits. Together the retained main result and this "
        "result must cover the requested outcomes; final integration happens after collection. "
        "A generic bundle of excerpts for the whole task is not an independent outcome."
    ),
}


def transform(request: ModelRequest, condition: str) -> ModelRequest:
    if condition not in CONDITIONS:
        raise ValueError("unknown-condition")
    if [t.name for t in request.tools] != [phase_diagnosis.DECISION_TOOL]:
        raise ValueError("exclusive-decision-required")
    schema = copy.deepcopy(request.tools[0].input_schema)
    for name in DESCRIPTIONS:
        if name not in schema["properties"] or "description" in schema["properties"][name]:
            raise ValueError("undescribed-original-fields-required")
    if condition == "original":
        return request
    for name, description in DESCRIPTIONS.items():
        schema["properties"][name]["description"] = description
    return replace(request, tools=(replace(request.tools[0], input_schema=schema),))


def code_digest() -> str:
    return fingerprint(
        {
            "driver": digest_bytes(Path(__file__).read_bytes()),
            "independent": digest_bytes(Path(independent_decision.__file__).read_bytes()),
            "shared_probe": digest_bytes(Path(phase_diagnosis.__file__).read_bytes()),
        }
    )


def prepare(prior_plan: Path, output: Path) -> None:
    prior = json.loads(prior_plan.read_text(encoding="utf-8"))
    phase_diagnosis.verify_sources(prior)
    rows = []
    for case in phase_diagnosis.CASES:

        def unique(condition, case):
            selected = [
                r for r in prior["rows"] if r["case"] == case and r["condition"] == condition
            ]
            if len(selected) != 1:
                raise ValueError("unique-source-condition-required")
            row = selected[0]
            if fingerprint(row["request"]) != row["request_fingerprint"]:
                raise ValueError("prior-request-changed")
            return row

        original = unique("original", case)
        selected = unique("evidence-focused-system", case)
        source = original["source"]
        packet = independent_decision.evidence_input(
            ModelRequest.from_dict(original["request"]),
            source,
            events_at(Path(source["database"])),
        )
        expected = independent_decision.transform(
            ModelRequest.from_dict(original["request"]),
            "evidence-focused-system",
            selected["request"]["system_prompt"],
            packet,
        )
        if expected.to_dict() != selected["request"] or source != selected["source"]:
            raise ValueError("independent-input-provenance-mismatch")
        for condition in CONDITIONS:
            request = transform(expected, condition)
            rows.append(
                {
                    "case": case,
                    "condition": condition,
                    "source": source,
                    "request": request.to_dict(),
                    "request_fingerprint": fingerprint(request.to_dict()),
                }
            )
    output.mkdir(parents=True, exist_ok=False)
    phase_diagnosis.write(
        output / "plan.json",
        {
            "kind": "complementary-work-probe",
            "product_evaluation": False,
            "code_digest": code_digest(),
            "prior_plan_sha256": digest_bytes(prior_plan.read_bytes()),
            "sources": prior["sources"],
            "max_calls": 6,
            "retry": False,
            "timeout_seconds": 60,
            "rows": rows,
        },
    )


async def run(output: Path, profile: Path) -> None:
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    if plan["code_digest"] != code_digest():
        raise ValueError("probe-code-changed")
    if plan["max_calls"] != 6 or [(r["case"], r["condition"]) for r in plan["rows"]] != [
        (case, condition) for case in phase_diagnosis.CASES for condition in CONDITIONS
    ]:
        raise ValueError("frozen-matrix-mismatch")
    for row in plan["rows"]:
        if fingerprint(row["request"]) != row["request_fingerprint"]:
            raise ValueError("frozen-request-changed")
    phase_diagnosis.verify_sources(plan)
    results_dir = output / "results"
    results_dir.mkdir(exist_ok=False)
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    _, provider, model = connection(profile)
    if any(
        r["request"]["provider"] != provider.name or r["request"]["model"] != model
        for r in plan["rows"]
    ):
        raise ValueError("authorized-model-mismatch")
    rows = []
    try:
        for i, row in enumerate(plan["rows"]):
            result = await phase_diagnosis.probe(
                provider, ModelRequest.from_dict(row["request"]), results_dir / f"{i:02}.json"
            )
            rows.append(
                {
                    "case": row["case"],
                    "condition": row["condition"],
                    **result["assessment"],
                    "usage": result["response"]["usage"],
                }
            )
            phase_diagnosis.write(
                output / "summary.json", {"complete": len(rows) == 6, "rows": rows}
            )
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    finally:
        phase_diagnosis.verify_sources(plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("prepare")
    pre.add_argument("--prior-plan", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    live = commands.add_parser("run")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.prior_plan, args.output)
    else:
        asyncio.run(run(args.output, args.profile))
