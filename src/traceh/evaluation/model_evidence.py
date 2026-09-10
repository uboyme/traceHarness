"""Read original closed control-model evidence. No model execution or grading."""

import json
import sqlite3
from pathlib import Path

from traceh.agents.directory import AgentDirectory
from traceh.api.events import EventEnvelope
from traceh.api.json_types import fingerprint, to_json_value
from traceh.api.llm import ModelRequest
from traceh.budgets.events import BUDGET_LEDGER_STREAM
from traceh.budgets.projection import BudgetLedger
from traceh.evaluation.evaluators.episode_assessment import usage
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.runtime.request_builder import build_request_from_events
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.surface import SurfaceProjector


def require(condition):
    if not condition:
        raise ValueError("evaluation-model-evidence-mismatch")


def model_events(root):
    path = Path(root) / "data/events.sqlite3"
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT envelope_json FROM events ORDER BY stream_id, seq"
        ).fetchall()
        return tuple(EventEnvelope.from_dict(json.loads(r[0])) for r in rows)
    finally:
        connection.close()


def observe_model_call(root):
    """Derive response and costs from the original log, not the cached result."""
    root = Path(root)
    definition = read_input(root, "call.json").data
    require(definition["format"] == 1)
    events = model_events(root)
    session = tuple(e for e in events if e.stream_id == "session:" + definition["session_id"])
    effects = tuple(e for e in events if e.stream_id == "effect:" + definition["session_id"])
    require(not CoreInvariantChecker().check(session, effects))
    if session:
        require(session[0].data["metadata"]["control_call"] == fingerprint(definition))
    directory = AgentDirectory.rebuild(
        tuple(e for e in events if e.stream_id == "agents:directory")
    )
    ledger = BudgetLedger.rebuild(
        tuple(e for e in events if e.stream_id == BUDGET_LEDGER_STREAM),
        directory,
    )
    starts = [e for e in session if e.type == "turn/start"]
    require(len(starts) <= 1)
    messages = [e for e in session if e.type == "user/message"]
    if starts:
        require(len(messages) == 1 and messages[0].data["content"] == definition["input"])
    snapshots = [e for e in session if e.type == "request/snapshot"]
    require(len(snapshots) <= 1)
    for event in snapshots:
        data = event.data
        built = build_request_from_events(
            session,
            SurfaceProjector(),
            session_id=definition["session_id"],
            turn_id=data["turn_id"],
            step_id=data["step_id"],
            through_seq=data["source_seq"],
        )
        composed = ModelRequest.from_dict(data["composed_request"])
        dispatch = ModelRequest.from_dict(data["dispatch_request"])
        require(built.request == composed and built.fingerprint == data["composed_fingerprint"])
        require(fingerprint(dispatch.to_dict()) == data["dispatch_fingerprint"])
        # This service reserves the entire single-call grant: admission cannot rewrite it.
        require(dispatch == composed and not dispatch.tools)
        require(
            dispatch.provider == definition["config"]["provider"]
            and dispatch.model == definition["config"]["model"]
            and dispatch.system_prompt.endswith(
                "## traceh.evaluation.control-model\n" + definition["system"]
            )
            and dispatch.max_output_tokens == definition["config"]["output_tokens"]
            and dispatch.temperature == definition["config"]["temperature"]
        )
    ends = [e for e in session if e.type == "turn/end"]
    answers = [e for e in session if e.type == "assistant/message"]
    attempts = [e for e in session if e.type == "model/attempt-end"]
    require(len(attempts) <= 1)
    if attempts:
        agent = directory.get(definition["agent_id"])
        account = ledger.account(definition["agent_id"])
        require(agent is not None and agent.session_id == definition["session_id"])
        require(
            account is not None and account.limits.max_tokens == definition["config"]["token_limit"]
        )
        require(all(r.status in {"settled", "released"} for r in ledger.usage_reservations))
    completed = (
        len(ends) == len(answers) == len(attempts) == 1
        and ends[0].data["reason"] == "completed"
        and attempts[0].data["status"] == "succeeded"
        and not answers[0].data.get("tool_calls")
    )
    return {
        "format": 1,
        "definition_digest": fingerprint(definition),
        "database_sha256": digest_bytes((root / "data/events.sqlite3").read_bytes()),
        "session_id": definition["session_id"],
        "turn_id": None if not starts else starts[0].data["turn_id"],
        "completed": completed,
        "text": answers[-1].data["content"] if completed else None,
        "usage": usage(session),
        "budget_usage_exact": bool(attempts)
        and all(r.usage_quality == "exact" for r in ledger.usage_reservations if r.amounts.tokens),
        "budget": to_json_value(ledger.account(definition["agent_id"])),
        "requests": len(snapshots),
    }


def load_model_call(root, *, digest=None):
    root = Path(root)
    receipt = read_input(root, "result.json")
    if digest is not None:
        require(receipt.sha256 == digest)
    observed = observe_model_call(root)
    require(receipt.data["observation"] == observed)
    return read_input(root, "call.json").data, receipt.data, receipt.sha256
