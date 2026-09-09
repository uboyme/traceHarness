"""Deterministic answer and actually dispatched evidence checks; never LLM grading."""

import json
import re

from live_skill_navigation.run import reference_identity

from traceh.session.context_input import parse_context_input, render_context_message


def answer_object(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-answer-key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-json-answer-constant")

    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fenced:
        text = fenced[1]
    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, TypeError):
        return None
    return (
        value if isinstance(value, dict) and all(type(v) is str for v in value.values()) else None
    )


def _matches(block, kind, expected, case):
    if block["kind"] != kind:
        return False
    if kind == "memory":
        return (
            block["tier"] in {"summary", "section"}
            and block["id"] == expected["id"]
            and block["version"] == expected["version"]
            and block["source_refs"] == expected["source_refs"]
            and block["body"] == expected["body"]
        )
    if kind == "skill":
        return (
            reference_identity({**block["provenance"], "requested_tier": block["tier"]})
            == expected["identity"]
            and block["body"] == expected["body"]
        )
    if kind == "history":
        return (
            block["tier"] in {"section", "chunk"}
            and expected["body"] in block["body"]
            and block["provenance"]["freshness"] == case["freshness"]
        )
    return False


def _file_evidence(expected, events, messages):
    matches = []
    for event in events:
        data = event.data
        if (
            event.type != "tool/result"
            or data["tool_name"] != "read_file"
            or data["status"] != "succeeded"
            or data["data"].get("path") != expected["path"]
            or expected["body"].strip() not in data["content"]
        ):
            continue
        if all(
            any(
                m["role"] == "tool"
                and m.get("tool_call_id") == data["tool_call_id"]
                and expected["body"].strip() in m["content"]
                for m in frame
            )
            for frame in messages
        ):
            matches.append(event.seq)
    return matches


def assess(case, expected, events, final_text, reason, replay_errors, invariants, allowed_tools):
    target = [e for e in events if e.seq >= expected["target_start_seq"]]
    contexts = [e for e in target if e.type == "context/input"]
    snapshots = [e for e in target if e.type == "request/snapshot"]
    last = snapshots[-1] if snapshots else None
    last_context = next(
        (e for e in contexts if last and e.seq == last.data["context_input_seq"]),
        None,
    )
    messages = (
        [last.data[key]["messages"] for key in ("composed_request", "dispatch_request")]
        if last
        else []
    )
    current_context = (
        render_context_message(parse_context_input(last_context.data)).to_dict()
        if last_context
        else None
    )
    context_dispatched = bool(current_context) and all(
        frame and frame[-1] == current_context for frame in messages
    )
    final_blocks = last_context.data["blocks"] if context_dispatched else []
    scope = []
    for event in contexts:
        for block in event.data["blocks"]:
            kind, identity = block["kind"], block["id"]
            valid = block["scope"]["session_id"] == expected["session_id"]
            if kind == "memory":
                fact = expected["active_memory"].get(identity)
                valid = (
                    valid
                    and fact is not None
                    and block["version"] == fact["version"]
                    and block["source_refs"] == fact["source_refs"]
                    and block["provenance"]["project_id"] == expected["project_id"]
                )
            elif kind == "skill":
                valid = valid and identity in expected["selected_ids"]
            if not valid:
                scope.append({"seq": event.seq, "kind": kind, "id": identity})
            if any(body in block["body"] for body in expected["forbidden_bodies"]):
                scope.append(
                    {"seq": event.seq, "kind": kind, "id": identity, "code": "forbidden-body"}
                )
    evidence = {}
    for source, value in expected["sources"].items():
        kind = source.split(":", 1)[0]
        if kind == "file":
            found = _file_evidence(value, target, messages) if messages else []
            evidence[source] = {"last_request": bool(found), "tool_result_seqs": found}
        else:
            earlier = [
                e.seq
                for e in contexts
                if any(_matches(b, kind, value, case) for b in e.data["blocks"])
            ]
            evidence[source] = {
                "last_request": any(_matches(b, kind, value, case) for b in final_blocks),
                "context_seqs": earlier,
            }
    results = [e for e in target if e.type == "tool/result"]
    succeeded = {e.data["tool_name"] for e in results if e.data["status"] == "succeeded"}
    errors = [{"seq": e.seq, **e.data} for e in results if e.data["status"] != "succeeded"]
    other_tools = [
        e.data["tool_name"]
        for e in target
        if e.type == "tool/call" and e.data["tool_name"] not in allowed_tools
    ]
    missing_tools = sorted(set(case.get("required_tools", [])) - succeeded)
    answer = answer_object(final_text)
    answer_ok = answer == case["answers"]
    evidence_ok = (
        bool(last)
        and context_dispatched
        and all(item["last_request"] for item in evidence.values())
    )
    passed = bool(
        reason == "completed"
        and answer_ok
        and evidence_ok
        and not missing_tools
        and not errors
        and not other_tools
        and not scope
        and not replay_errors
        and not invariants
    )
    return {
        "passed": passed,
        "task_passed": passed,
        "answer_ok": answer_ok,
        "answer": answer,
        "evidence_ok": evidence_ok,
        "evidence": evidence,
        "last_snapshot_seq": last.seq if last else None,
        "last_context_seq": last_context.seq if last_context else None,
        "context_dispatched": context_dispatched,
        "missing_tools": missing_tools,
        "tool_errors": errors,
        "other_tool_calls": other_tools,
        "scope_violations": scope,
        "replay_errors": replay_errors,
        "invariants": invariants,
        "requests": len(snapshots),
    }


def summarize(corpus, results, *, complete):
    core = [r for r in results if r["group"] == "core"]
    semantic = [r for r in results if r["group"] == "semantic-diagnostic"]
    failures = {
        key: sum(len(r.get(key, [])) for r in results)
        for key in ("scope_violations", "replay_errors", "invariants")
    }
    core_passed = sum(bool(r.get("task_passed")) for r in core)
    evidence_errors = sum("evidence_error_type" in r for r in results)
    return {
        "complete_grid": complete,
        "core": {"total": len(core), "task_passed": core_passed},
        "semantic_diagnostic": {
            "total": len(semantic),
            "task_passed": sum(bool(r.get("task_passed")) for r in semantic),
        },
        **failures,
        "evidence_errors": evidence_errors,
        "acceptance_passed": bool(
            complete
            and len(core) == corpus["acceptance"]["core_cases_per_model"]
            and core_passed >= corpus["acceptance"]["minimum_core_task_passed_per_model"]
            and not any(failures.values())
            and not evidence_errors
        ),
    }
