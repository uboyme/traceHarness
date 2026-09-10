"""Derived journey observations, not a scorer or an Agent retrieval policy."""

import json

from traceh.api.history import HistoryReadPolicy
from traceh.api.json_types import fingerprint
from traceh.evaluation.evaluators.episode_assessment import answer_dispatches, dispatched_context
from traceh.session.history import read_history

READ_TOOLS = frozenset(
    {
        "request_history_page",
        "request_skill_reference",
        "request_workspace_memory",
        "read_tool_output",
    }
)


def _candidate(block, context, events, expected):
    """Locate the expected source container; never equate navigation with its body."""
    family = block["kind"]
    references = (
        [h["reference"] for h in json.loads(block["body"])["hits"]]
        if block["tier"] == "search"
        else [{"id": block["id"], "skill_id": block["id"], "block_id": block["id"]}]
    )
    if family in {"memory", "skill"}:
        key = "skill_id" if family == "skill" else "id"
        return any(r.get(key) == expected["reference_id"] for r in references)
    history = read_history(
        events,
        session_id=context["session_id"],
        through_seq=context["observed_session_seq"],
        policy=HistoryReadPolicy.from_dict(context["policy"]["config"]["history"]),
    )
    by_seq = {e.seq: e for e in events}
    return any(
        expected["source_text"] in (by_seq[leaf["seq"]].data.get("content") or "")
        for ref in references
        for leaf in history.leaf_refs(ref["block_id"])
    )


def observe_episode(packet, events):
    """Use verified native events and existing scorer receipts; unknown is not a miss."""
    expected, family = packet["expectation"], packet["family"]
    positive = expected["kind"] == "value"
    by_seq = {e.seq: e for e in events}
    before, turn = packet["target_start_seq"], packet["target_turn_id"]
    target = [e for e in events if before and e.seq > before]
    results = {e.data["tool_call_id"]: e for e in target if e.type == "tool/result"}
    reads = []
    for call in target:
        if call.type != "tool/call" or call.data["tool_name"] not in READ_TOOLS:
            continue
        result = results.get(call.data["tool_call_id"])
        reads.append(
            {
                "call_seq": call.seq,
                "tool_call_id": call.data["tool_call_id"],
                "tool_name": call.data["tool_name"],
                "arguments": call.data["arguments"],
                "requested_tier": call.data["arguments"].get("requested_tier"),
                "result_seq": result.seq if result else None,
                "status": result.data["status"] if result else "no_result",
                "error_type": result.data["error_type"] if result else None,
            }
        )
    usable = not any(packet[k] for k in ("replay_errors", "invariant_errors", "scope_violations"))
    frames = (
        list(answer_dispatches(events, target_start=before, target_turn=turn)) if usable else []
    )
    candidates, evidence, views, coverage = [], [], [], []
    contexts_seen, results_seen, searches_seen = set(), set(), set()
    output_refs = {
        (p["reference"]["effect_id"], p["reference"]["digest"])
        for p in packet["output_sources"]
        if p["seq"] <= before
    }
    for attempt, snapshot in frames:
        proof = {
            "request_snapshot_seq": snapshot.seq,
            "attempt_id": attempt.data["attempt_id"],
            "dispatch_fingerprint": snapshot.data["dispatch_fingerprint"],
        }
        visible = dispatched_context(snapshot, by_seq)
        visible_context_seq = visible[0].seq if visible else None
        if visible is not None and visible[0].seq not in contexts_seen:
            context_event, context = visible
            contexts_seen.add(context_event.seq)
            for block in context["blocks"]:
                if block["kind"] != family:
                    continue
                observation = {
                    **proof,
                    "context_seq": context_event.seq,
                    "id": block["id"],
                    "version": block["version"],
                    "tier": block["tier"],
                    "provenance": block["provenance"],
                }
                views.append(observation)
                if positive and _candidate(block, context, events, expected):
                    candidates.append(observation)
                if block["tier"] == "search":
                    page = json.loads(block["body"])
                    identity = fingerprint([block["provenance"], block["content_digest"]])
                    if identity not in searches_seen:
                        searches_seen.add(identity)
                        coverage.append(
                            {
                                **observation,
                                "domain": "skill-navigation" if family == "skill" else family,
                                **{
                                    k: page[k]
                                    for k in (
                                        "query",
                                        "fields",
                                        "status",
                                        "scanned",
                                        "total",
                                        "next_cursor",
                                    )
                                },
                                "hit_count": len(page["hits"]),
                                "proves_global_absence": False,
                            }
                        )
        visible_results = set()
        for result in results.values():
            if result.seq >= snapshot.seq or result.data["status"] != "succeeded":
                continue
            if not any(
                m["role"] == "tool"
                and m.get("tool_call_id") == result.data["tool_call_id"]
                and m.get("content") == result.data["content"]
                for m in snapshot.data["dispatch_request"]["messages"]
            ):
                continue
            visible_results.add(result.seq)
            if family != "output" or result.seq in results_seen:
                continue
            results_seen.add(result.seq)
            name = result.data["tool_name"]
            if name not in {"list_tool_outputs", "read_tool_output", "search_tool_output"}:
                continue
            page = json.loads(result.data["content"])
            refs = (
                [p["output_ref"] for p in page["outputs"]]
                if name == "list_tool_outputs"
                else [page]
            )
            observation = {**proof, "tool_result_seq": result.seq, "tool_name": name}
            if name == "read_tool_output":
                observation["page"] = {
                    k: page[k]
                    for k in ("effect_id", "digest", "part", "offset", "next_offset", "total_chars")
                }
            views.append(observation)
            if positive and any((r["effect_id"], r["digest"]) in output_refs for r in refs):
                candidates.append(observation)
            if name == "search_tool_output":
                coverage.append(
                    {
                        **observation,
                        "domain": "retained-tool-output",
                        **{
                            k: page[k]
                            for k in (
                                "effect_id",
                                "digest",
                                "part",
                                "query",
                                "offset",
                                "next_offset",
                                "total_chars",
                            )
                        },
                        "hit_count": len(page["matches"]),
                        "proves_global_absence": False,
                    }
                )
        for receipt in packet["dispatched_evidence"]:
            if (
                receipt["request_snapshot_seq"] == snapshot.seq
                and receipt["attempt_id"] == attempt.data["attempt_id"]
                and receipt["dispatch_fingerprint"] == proof["dispatch_fingerprint"]
                and (
                    receipt.get("context_seq") == visible_context_seq
                    if family != "output"
                    else receipt.get("tool_result_seq") in visible_results
                )
            ):
                evidence.append(
                    {
                        **proof,
                        "context_seq": receipt.get("context_seq"),
                        "tool_result_seq": receipt.get("tool_result_seq"),
                        "reference": receipt["reference"],
                    }
                )
    complete = bool(frames) and packet["failure"] is None and packet["reason"] == "completed"
    mapped = bool(
        expected["reference_id"]
        if family in {"skill", "memory"}
        else expected["source_text"]
        if family == "history"
        else output_refs
    )

    def state(found, known=True):
        if not positive:
            return "not_applicable"
        return "observed" if found else "not_observed" if complete and known else "unknown"

    candidate_status = state(candidates or evidence, mapped)
    evidence_status = state(evidence)
    gap = (
        "negative-scope-review"
        if not positive
        else "evidence-dispatched"
        if evidence
        else "candidate-without-evidence"
        if candidate_status == "observed"
        else "candidate-not-observed"
        if candidate_status == "not_observed"
        else "unresolved"
    )
    return {
        "format": 1,
        "candidate": {
            "status": candidate_status,
            "granularity": "source-container",
            "observations": candidates or evidence,
        },
        "evidence": {"status": evidence_status, "observations": evidence},
        "gap": gap,
        "source_views": views,
        "read_requests": reads,
        "search_coverage": coverage,
        "negative_scope_review_required": not positive,
        "successful_answer_dispatches": len(frames),
        "execution_failure": packet["failure"],
        "execution_reason": packet["reason"],
    }


def diagnostic_row(packet, observation, trial):
    return {
        "trial_id": trial["identity"]["trial_id"],
        "case_id": trial["identity"]["case_id"],
        "execution": trial["execution"],
        "assessment": trial["assessment"],
        "provisional_answer_match": packet["provisional_answer_match"] if packet else None,
        "question": packet["question"] if packet else None,
        "answer": packet["answer"] if packet else None,
        "observation": observation,
    }


def diagnostics_markdown(rows):
    def status(row, key):
        return row["observation"][key]["status"] if row["observation"] else "unknown"

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "## Retrieval diagnostics",
        "",
        "相关来源候选只定位来源容器；目录不等于正文。派发只证明内容进入请求，不证明模型已理解。",
        "读取回执不证明正文已送达；完整有效搜索片段不需要额外 read。",
        "负例按原查询、字段和分页范围审阅，不把查询无命中当成全库不存在。",
        "observed=已出现，not_observed=本条轨迹未出现，unknown=未知，not_applicable=不适用。",
        "pending_review=待人工审阅；provisional 匹配不是正式评分。",
        "",
        "| 题目 ID | 相关来源候选 | 足够证据派发 | 回答评估 | 观测缺口 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                map(
                    cell,
                    (
                        row["case_id"],
                        status(row, "candidate"),
                        status(row, "evidence"),
                        row["assessment"]["status"],
                        row["observation"]["gap"] if row["observation"] else "unresolved",
                    ),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"
