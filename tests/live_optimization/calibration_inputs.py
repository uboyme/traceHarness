"""Explicit AO-2+ development fixtures; never a production scoring rule."""

import copy

from traceh.evaluation.model_review_protocol import REVIEW_SYSTEM, review_input
from traceh.evaluation.review import _episode_events, _run


def build_inputs(before):
    # This historical experiment's candidate was rejected. Reproducing it requires
    # its archived source, never silently evaluating the restored baseline twice.
    if REVIEW_SYSTEM == before["system"]:
        raise ValueError("calibration-requires-frozen-candidate-source")
    rows = before["rows"]
    selected = [
        ("development", "01", "s-absent", "failed", "catalog absence overstated"),
        ("development", "02", "s-absent", "failed", "catalog absence overstated"),
        ("heldout", "01", "stamp-catalog", "failed", "catalog absence overstated"),
        ("heldout", "02", "stamp-catalog", "failed", "catalog absence overstated"),
        ("development", "02", "s-english", "passed", "source section supports answer"),
        ("development", "02", "s-resource", "passed", "source resource supports answer"),
        ("heldout", "01", "printshop-limit", "passed", "approved memory supports answer"),
        ("heldout", "01", "geology-transfer", "passed", "complete history search excerpt"),
    ]
    samples, audit = [], []

    def add(row, expected, basis, *, answer=None):
        from pathlib import Path

        run = Path(row["run"])
        report, binding, rubric = _run(run)
        trial = next(t for t in report["trials"] if t["identity"] == row["trial"]["identity"])
        packet = next(
            p
            for p in report["task_report"]["episodes"]
            if p["trial_id"] == row["packet"]["trial_id"]
        )
        if binding != row["binding"] or packet != row["packet"] or rubric != row["rubric"]:
            raise ValueError("calibration-original-evidence-drift")
        _, events = _episode_events(run, trial, packet)
        current_packet = copy.deepcopy(packet)
        import json

        baseline = json.loads(row["baseline_input"])
        if answer is not None:
            current_packet["answer"] = baseline["answer"] = answer
        from traceh.api.json_types import canonical_json

        revised_input = review_input(rubric, current_packet, events)
        revised_data = json.loads(revised_input.splitlines()[1])
        del revised_data["evidence_scope"]
        if revised_data != baseline:
            raise ValueError("calibration-conditions-changed-evidence")
        samples.append(
            {
                "sample_id": f"case-{len(samples) + 1:02d}",
                "original_run": row["run"],
                "original_binding": binding,
                "original_trial_id": packet["trial_id"],
                "case_id": packet["case_id"],
                "expected": expected,
                "basis": basis,
                "answer_origin": "synthetic-contrast"
                if answer is not None
                else "original-agent-answer",
                "baseline": {"system": before["system"], "input": canonical_json(baseline)},
                "revised": {
                    "system": REVIEW_SYSTEM,
                    "input": revised_input,
                },
            }
        )

    for suite, arm, case, expected, basis in selected:
        row = next(
            r for r in rows if (r["suite"], r["arm"], r["packet"]["case_id"]) == (suite, arm, case)
        )
        add(row, expected, basis)
    for suite, case, answer in (
        (
            "development",
            "s-absent",
            "在当前可见的目录元数据和搜索结果中，我没有找到车辆加油折扣码的规定。未读取完整正文，不能据此断言整本手册不存在这项规定。",
        ),
        (
            "heldout",
            "stamp-catalog",
            "当前展示的目录摘要未给出停车券价格；仅凭这些资料无法确认规范全文是否写了价格，也无法给出金额。",
        ),
    ):
        row = next(
            r for r in rows if (r["suite"], r["arm"], r["packet"]["case_id"]) == (suite, "01", case)
        )
        add(
            row,
            "passed",
            "appropriately scoped negative; no exhaustive search required",
            answer=answer,
        )
    row = next(r for r in rows if r["arm"] == "02" and r["packet"]["case_id"] == "restoration-room")
    add(
        row,
        "failed",
        "correct code but wrong requested action",
        answer="应报口令 RILL-682，并按下绿色复位钮。",
    )
    add(row, "failed", "correct code but omitted requested action", answer="应报口令 RILL-682。")
    for row in rows:
        negative = row["packet"]["expectation"]["kind"] == "no-evidence"
        audit.append(
            {
                "trial_id": row["packet"]["trial_id"],
                "suite": row["suite"],
                "arm": row["arm"],
                "question": row["packet"]["question"],
                "answer": row["packet"]["answer"],
                "expected": "failed" if negative or row["hard_rejection"] else "passed",
                "basis": row["hard_rejection"]
                or (
                    "navigation-only categorical denial"
                    if negative
                    else "requested facts supported by dispatched source"
                ),
                "origin": "agent-contract-audit-not-human-approval",
            }
        )
    return samples, audit
