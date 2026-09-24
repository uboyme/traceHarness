"""Independent retrieval journeys under the shared evaluation scheduler."""

import json
from dataclasses import dataclass

from traceh.api.json_types import canonical_json, fingerprint
from traceh.evaluation.contracts import (
    AssessmentStatus,
    CheckStatus,
    ConvergenceStatus,
    ExecutionStatus,
    TrialResult,
    TrialSpec,
)
from traceh.evaluation.evaluators.episode_assessment import (
    answer_matches,
    dispatched_evidence,
    usage,
)
from traceh.evaluation.evaluators.episode_diagnostics import (
    diagnostic_row,
    diagnostics_markdown,
    observe_episode,
)
from traceh.evaluation.evaluators.episode_manifest import load_episode_suite
from traceh.evaluation.evaluators.episode_setup import (
    SOURCE_TOOLS,
    episode_runtime,
    setup_episode,
)
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.tool_output import RETAINED, resolve_tool_output


@dataclass(frozen=True)
class EpisodeResult:
    encoded: str

    def to_dict(self):
        import json

        return json.loads(self.encoded)


@dataclass(frozen=True)
class EpisodeReport:
    trials: tuple

    @property
    def complete(self):
        return bool(self.trials) and all(t.measured for t in self.trials)

    def to_dict(self):
        return {
            "scope_profile": "source-isolated",
            "purpose": "development-regression",
            "planned": len(self.trials),
            "measured": sum(t.measured for t in self.trials),
            "assessment_counts": {
                s.value: sum(t.assessment is s for t in self.trials) for s in AssessmentStatus
            },
            "episodes": [
                {"trial_id": t.spec.trial_id, **t.task_result.to_dict()}
                for t in self.trials
                if t.task_result is not None
            ],
        }

    def markdown(self):
        data = self.to_dict()
        packets = {p["trial_id"]: p for p in data["episodes"]}
        rows = []
        for trial in self.trials:
            packet = packets.get(trial.spec.trial_id)
            rows.append(
                diagnostic_row(
                    packet,
                    packet["retrieval_diagnostics"] if packet else None,
                    {
                        "identity": {
                            "trial_id": trial.spec.trial_id,
                            "case_id": trial.spec.case_id,
                        },
                        "execution": {"status": trial.execution.value, "reason": trial.reason},
                        "assessment": {"status": trial.assessment.value},
                    },
                )
            )
        return (
            "## Retrieval episodes\n\n"
            f"Measured: {data['measured']}/{data['planned']}. "
            "Exact answer matches are provisional; final judgments require evidence review.\n"
            + "\n"
            + diagnostics_markdown(rows)
        )


class RetrievalEpisodeEvaluator:
    def __init__(self, manifest, *, provider, model_id, retry_policy, sandbox, monotonic):
        self.manifest = manifest
        self.suite = load_episode_suite(manifest)
        self.provider, self.model_id = provider, model_id
        self.retry_policy, self.sandbox = retry_policy, sandbox
        self.monotonic = monotonic

    def trials(self, variant_id, repetitions):
        return tuple(
            TrialSpec(
                fingerprint(
                    {
                        "variant": variant_id,
                        "case": case.data["case_id"],
                        "material": case.digest,
                        "replicate": replicate,
                    }
                ),
                case.data["case_id"],
                case.data["group_id"],
                case.digest,
                case.data["material_seed"],
                replicate,
                None,
                variant_id,
            )
            for case in self.suite.cases
            for replicate in range(1, repetitions + 1)
        )

    def verify_inputs(self):
        self.manifest.verify()
        for file in self.suite.files:
            file.verify()

    def frozen_settings(self):
        return {
            "task_settings": self.manifest.task_settings,
            "assessment": self.manifest.assessment,
        }

    def frozen_materials(self):
        files = (self.manifest.document, self.manifest.dataset, *self.suite.files)
        return tuple(sorted({f.relative: f.content for f in files}.items()))

    async def execute(self, context):
        case = next(c for c in self.suite.cases if c.digest == context.spec.material_digest)
        data = case.data
        result, phase, failure = None, "setup", None
        before, events, effects, all_events = 0, (), (), ()
        replay, invariants = [], []
        started = self.monotonic()
        target_started = None
        async with episode_runtime(
            context,
            case,
            self.manifest,
            provider=self.provider,
            model_id=self.model_id,
            retry_policy=self.retry_policy,
            sandbox=self.sandbox,
        ) as prepared:
            try:
                await setup_episode(prepared, case, self.manifest)
                events = await prepared.runtime.sessions.read_session(prepared.session_id)
                before = events[-1].seq
                phase = "target"
                target_started = self.monotonic()
                result = await prepared.runtime.run_existing(prepared.session_id, data["question"])
            except Exception as error:
                failure = {
                    "phase": phase,
                    "type": type(error).__name__,
                    "code": getattr(error, "code", None),
                }
            session_id = prepared.session_id
            for session in dict.fromkeys((prepared.setup_session_id, session_id)):
                current = await prepared.runtime.sessions.read_session(session)
                all_events += current
                replay.extend(
                    await verify_request_snapshots(
                        prepared.runtime.sessions, prepared.runtime.surface, session
                    )
                )
                invariants.extend(str(v) for v in await prepared.runtime.check_invariants(session))
                if session == session_id:
                    events = current
            effects = await prepared.runtime.sessions.read_effects(session_id)
            counter = None
            if data["family"] == "output":
                path = prepared.workspace / data["setup"]["counter_path"]
                counter = path.read_text() if path.is_file() else None
        # No production resources are live while assessing captured, validated facts.
        target = tuple(e for e in events if before and e.seq > before)
        completed = result is not None and result.reason == "completed" and failure is None
        receipts = (
            dispatched_evidence(
                events,
                effects,
                data,
                target_start=before,
                target_turn=result.turn_id,
                max_chars=self.manifest.task_settings["runtime"]["max_tool_output_chars"],
            )
            if completed
            else []
        )
        calls = [
            {
                "seq": e.seq,
                "name": e.data["tool_name"],
                "arguments": e.data["arguments"],
                "tool_call_id": e.data["tool_call_id"],
            }
            for e in target
            if e.type == "tool/call"
        ]
        illegal = [
            e.seq
            for e in target
            if e.type == "tool/result"
            and e.data["tool_name"] not in SOURCE_TOOLS[data["family"]]
            and e.data["status"] == "succeeded"
        ]
        output_sources = []
        if data["family"] == "output":
            for e in events:
                # This experiment measures retrieval of output the model was
                # *not* shown, so its sources are the results the host actually
                # withheld. Every complete result is addressable now, so the
                # presence of a reference no longer means "retained"; the
                # disclosure the host recorded does. Widening this set would
                # silently rescore the frozen episodes.
                if (
                    e.type == "tool/result"
                    and e.data["tool_name"] == "shell"
                    and e.data.get("output_ref", {}).get("disclosure") == RETAINED
                ):
                    ref = e.data["output_ref"]
                    payload = resolve_tool_output(
                        events,
                        effects,
                        session_id=session_id,
                        effect_id=ref["effect_id"],
                        digest=ref["digest"],
                    )
                    output_sources.append(
                        {
                            "seq": e.seq,
                            "reference": ref,
                            "source_digest": fingerprint(payload),
                            "exit_code": payload["data"].get("exit_code"),
                        }
                    )
            if completed and (counter != "1" or len(output_sources) != 1):
                illegal.append("output-execution-count")
        answer = result.final_text if result is not None else ""
        matching = answer_matches(answer, data["expectation"]) if completed else None
        packet = {
            "case_id": data["case_id"],
            "family": data["family"],
            "material_seed": data["material_seed"],
            "question": data["question"],
            "expectation": data["expectation"],
            "answer": answer,
            "session_id": session_id,
            "setup_session_id": prepared.setup_session_id,
            "target_start_seq": before,
            "target_turn_id": result.turn_id if result else None,
            "reason": result.reason if result else None,
            "failure": failure,
            "dispatched_evidence": receipts,
            "provisional_answer_match": matching,
            "provisional_joint_pass": bool(
                completed
                and matching
                and receipts
                and not replay
                and not invariants
                and not illegal
            ),
            "preparation_text_digest": fingerprint(
                [
                    {
                        "content": e.data["content"],
                        "tool_calls": [
                            {"name": c["name"], "arguments": c["arguments"]}
                            for c in e.data["tool_calls"]
                        ],
                    }
                    for e in all_events
                    if e not in target and e.type == "assistant/message"
                ]
            ),
            "calls": calls,
            "search_read_count": sum(
                c["name"].startswith(("search_", "request_", "read_tool_")) for c in calls
            ),
            "searches": [
                {"seq": e.seq, "kind": b["kind"], "page": json.loads(b["body"])}
                for e in target
                if e.type == "context/input"
                for b in e.data["blocks"]
                if b["tier"] == "search"
            ],
            "output_executions": len(output_sources),
            "output_sources": output_sources,
            "replay_errors": replay,
            "invariant_errors": invariants,
            "scope_violations": illegal,
            "elapsed_seconds": self.monotonic() - started,
            "target_seconds": None if target_started is None else self.monotonic() - target_started,
            "usage": {
                "all": usage(all_events),
                "target": usage(target),
                "preparation": usage(tuple(e for e in all_events if e not in target)),
            },
        }
        packet["retrieval_diagnostics"] = observe_episode(packet, events)
        status = CheckStatus.VIOLATED if replay or invariants or illegal else CheckStatus.PASSED
        return TrialResult(
            context.spec,
            ExecutionStatus.COMPLETED if completed else ExecutionStatus.FAILED,
            failure["code"] or failure["type"]
            if failure
            else (None if completed else result.reason),
            AssessmentStatus.PENDING_REVIEW if completed else AssessmentStatus.UNASSESSABLE,
            status,
            ConvergenceStatus.CONVERGED,
            True,
            EpisodeResult(canonical_json(packet)),
            usage=tuple(packet["usage"].items()),
        )

    def summarize(self, results):
        return EpisodeReport(results)
