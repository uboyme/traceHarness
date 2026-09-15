"""Frozen retrieval evaluator for the existing ProductTask attempt runner.

Judgments stay here, outside repositories and model-visible canonical facts.
Every observation is one real Step, regardless of Provider retry count.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.memory import MemoryPolicy, ProjectScopeLimits
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.session.context_input import ContextInputPolicy, parse_context_input
from traceh.session.retrieval import normalize

CATEGORIES = frozenset(
    {
        "exact",
        "lexical",
        "semantic",
        "zero-hit",
        "cross-workspace",
        "revoked",
        "superseded",
        "retired-generation",
        "hard-negative",
    }
)
ROLES = frozenset({"requester", "coder", "investigator", "patch_author"})
EVALUATOR_VERSION = "context-injected-unique-step-v1"


def environment():
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
        "unicode": unicodedata.unidata_version,
    }


def unavailable_retrieval(spec, task_id):
    judgments = [j for j in spec.data["judgments"] if j["task_id"] == task_id]
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_digest": spec.digest,
        "input_digest": spec.file_digest,
        "environment": environment(),
        "seed": None,
        "observations": [],
        "expected_judgments": len(judgments),
        "unproven": [
            {"role": j["role"], "query": j["query"], "category": j["category"]} for j in judgments
        ],
        "unavailable_steps": None,
        "quality_passed": False,
        "k": spec.data["evaluator"]["k"],
        "note": "Attempt preparation failed; no successful-only denominator.",
    }


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("retrieval-manifest-shape-invalid")
    return value


def text(value):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("retrieval-manifest-value-invalid")
    return value


def identity(value):
    value = exact(value, {"kind", "id", "tiers"})
    text(value["id"])
    if value["kind"] not in {"skill", "memory"} or type(value["tiers"]) is not list:
        raise ValueError("retrieval-judgment-invalid")
    if not value["tiers"] or not set(value["tiers"]) <= {
        "directory",
        "summary",
        "section",
        "chunk",
    }:
        raise ValueError("retrieval-judgment-invalid")
    return value


@dataclass(frozen=True)
class FrozenRetrieval:
    path: Path
    file_digest: str
    canonical: str

    @property
    def data(self):
        return json.loads(self.canonical)

    @property
    def digest(self):
        return fingerprint({"evaluator": EVALUATOR_VERSION, "input": self.data})

    @property
    def context(self):
        return ContextInputPolicy.from_dict(self.data["context"])

    @property
    def memory_policy(self):
        raw = self.data["memory_policy"]
        return MemoryPolicy(**{**raw, "denied_patterns": tuple(raw["denied_patterns"])})

    def verify(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.file_digest:
            raise BenchmarkManifestError("retrieval-frozen-input-changed", "retrieval")


def load_retrieval(value, root, tasks):
    if value is None:
        return None
    try:
        value = exact(value, {"file", "sha256"})
        relative = Path(text(value["file"]))
        root = Path(root).resolve(strict=True)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("retrieval-path-invalid")
        path = (root / relative).resolve(strict=True)
        if root not in path.parents or not path.is_file():
            raise ValueError("retrieval-path-invalid")
        # Keep evaluator/corpus files out of every model-writable initial tree.
        if any(path == task.initial_dir or task.initial_dir in path.parents for task in tasks):
            raise ValueError("retrieval-input-in-workspace")
        if path.stat().st_size > 2_000_000:
            raise ValueError("retrieval-input-too-large")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != value["sha256"]:
            raise ValueError("retrieval-digest-mismatch")
        raw = exact(
            json.loads(content),
            {
                "format",
                "context",
                "project_limits",
                "memory_policy",
                "memories",
                "plugins",
                "judgments",
                "evaluator",
            },
        )
        if type(raw["format"]) is not int or raw["format"] != 1:
            raise ValueError("retrieval-version-unsupported")
        policy = ContextInputPolicy.from_dict(raw["context"])
        if policy.memory is None:
            raise ValueError("retrieval-memory-policy-required")
        ProjectScopeLimits(**exact(raw["project_limits"], ProjectScopeLimits.__dataclass_fields__))
        memory = exact(raw["memory_policy"], MemoryPolicy.__dataclass_fields__)
        if type(memory["denied_patterns"]) is not list:
            raise ValueError("retrieval-memory-policy-invalid")
        MemoryPolicy(**{**memory, "denied_patterns": tuple(memory["denied_patterns"])})
        if type(raw["memories"]) is not list or len(raw["memories"]) > 256:
            raise ValueError("retrieval-corpus-invalid")
        ids = set()
        for fact in raw["memories"]:
            exact(fact, {"id", "scope", "slot", "body", "status", "successor"})
            for key in ("id", "slot", "body"):
                text(fact[key])
            if fact["id"] in ids or fact["scope"] not in {"current", "foreign"}:
                raise ValueError("retrieval-corpus-identity-invalid")
            ids.add(fact["id"])
            if fact["status"] not in {"active", "proposed", "revoked", "superseded"}:
                raise ValueError("retrieval-corpus-state-invalid")
            successor = fact["successor"]
            if fact["status"] == "superseded":
                exact(successor, {"id", "body"})
                if text(successor["id"]) in ids:
                    raise ValueError("retrieval-corpus-identity-invalid")
                ids.add(successor["id"])
                text(successor["body"])
            elif successor is not None:
                raise ValueError("retrieval-corpus-state-invalid")
        plugins = exact(
            raw["plugins"], {"enabled", "retired", "catalog_digest", "selected", "limits"}
        )
        for key in ("enabled", "retired", "selected"):
            if type(plugins[key]) is not list or len(set(plugins[key])) != len(plugins[key]):
                raise ValueError("retrieval-plugin-selection-invalid")
            for item in plugins[key]:
                text(item)
        if set(plugins["enabled"]) & set(plugins["retired"]):
            raise ValueError("retrieval-plugin-selection-invalid")
        if plugins["enabled"] or plugins["retired"]:
            from traceh.api.skills import SkillLimits

            SkillLimits(**exact(plugins["limits"], SkillLimits.__dataclass_fields__))
            if policy.skills is None:
                raise ValueError("retrieval-skill-policy-required")
        elif plugins["limits"] is not None or plugins["selected"]:
            raise ValueError("retrieval-plugin-selection-invalid")
        judgments = raw["judgments"]
        if type(judgments) is not list or not 1 <= len(judgments) <= 512:
            raise ValueError("retrieval-judgments-invalid")
        seen = set()
        for judge in judgments:
            exact(judge, {"task_id", "role", "query", "category", "relevant", "forbidden"})
            if (
                judge["role"] not in ROLES
                or judge["category"] not in CATEGORIES
                or judge["task_id"] not in {task.task_id for task in tasks}
            ):
                raise ValueError("retrieval-judgment-invalid")
            key = (judge["task_id"], judge["role"], normalize(text(judge["query"])))
            if key in seen:
                raise ValueError("retrieval-judgment-duplicate")
            seen.add(key)
            for name in ("relevant", "forbidden"):
                if type(judge[name]) is not list:
                    raise ValueError("retrieval-judgment-invalid")
                keys = [(identity(i)["kind"], i["id"]) for i in judge[name]]
                if len(set(keys)) != len(keys):
                    raise ValueError("retrieval-judgment-duplicate")
            if {(i["kind"], i["id"]) for i in judge["relevant"]} & {
                (i["kind"], i["id"]) for i in judge["forbidden"]
            }:
                raise ValueError("retrieval-judgment-conflict")
        evaluator = exact(
            raw["evaluator"], {"k", "thresholds", "max_context_bytes", "max_context_prepare_ms"}
        )
        if type(evaluator["k"]) is not int or evaluator["k"] < 1:
            raise ValueError("retrieval-k-invalid")
        for key in ("max_context_bytes", "max_context_prepare_ms"):
            if type(evaluator[key]) is not int or evaluator[key] < 1:
                raise ValueError("retrieval-resource-threshold-invalid")
        thresholds = evaluator["thresholds"]
        if thresholds is not None:
            exact(thresholds, {j["category"] for j in judgments})
            for threshold in thresholds.values():
                exact(threshold, {"recall", "mrr", "precision", "zero_hit"})
                if any(type(v) not in {int, float} or not 0 <= v <= 1 for v in threshold.values()):
                    raise ValueError("retrieval-threshold-invalid")
        return FrozenRetrieval(path, value["sha256"], canonical_json(raw))
    except (ValueError, TypeError, KeyError, OSError):
        raise BenchmarkManifestError("benchmark-retrieval-input-invalid", "retrieval") from None


def score_blocks(blocks, judgment, k):
    """Actual injection order; one identity counts once at its first eligible tier."""
    relevant = {(i["kind"], i["id"]): i["tiers"] for i in judgment["relevant"]}
    unique, seen = [], {}
    for block in blocks:
        key = (block["kind"], block["id"])
        if block["kind"] not in {"memory", "skill"}:
            continue
        if key in seen:
            if block["tier"] in relevant.get(key, ()):
                unique[seen[key]] = block
            continue
        seen[key] = len(unique)
        unique.append(block)
    selected = unique[:k]
    forbidden = {(i["kind"], i["id"]) for i in judgment["forbidden"]}
    hits = [
        index
        for index, block in enumerate(selected, 1)
        if block["tier"] in relevant.get((block["kind"], block["id"]), ())
    ]
    return {
        "recall": len(hits) / len(relevant) if relevant else None,
        "mrr": 1 / hits[0] if hits else (0.0 if relevant else None),
        "precision": len(hits) / len(selected) if selected else (0.0 if relevant else None),
        "context_precision": (
            sum(b["tier"] in relevant.get((b["kind"], b["id"]), ()) for b in unique) / len(unique)
            if unique
            else (0.0 if relevant else None)
        ),
        "zero_hit": float(not unique) if not relevant else None,
        "scope_violations": sum((b["kind"], b["id"]) in forbidden for b in unique),
    }


async def collect_retrieval(sessions, spec, *, session_roles, task_id, seed_receipt):
    raw = spec.data
    judgments = [j for j in raw["judgments"] if j["task_id"] == task_id]
    observed, matched = [], set()
    for session_id, role in session_roles.items():
        events = await sessions.read_session(session_id)
        for step in (e for e in events if e.type == "step/start"):
            step_id = step.data["step_id"]
            contexts = [
                e for e in events if e.type == "context/input" and e.data["step_id"] == step_id
            ]
            row = {
                "session_id": session_id,
                "role": role,
                "step_id": step_id,
                "status": "unavailable",
                "query": None,
                "metrics": None,
            }
            if contexts:
                if len(contexts) != 1:
                    raise ValueError("retrieval-context-duplicate")
                context = parse_context_input(contexts[0].data).to_dict()
                query = context["query"]["text"]
                row.update(
                    query=query,
                    query_digest=context["query"]["digest"],
                    context_digest=context["context_digest"],
                    injected=[
                        {key: b[key] for key in ("kind", "id", "tier", "content_bytes")}
                        for b in context["blocks"]
                    ],
                    context_bytes=context["budget"]["rendered_bytes"],
                    candidate_ranking=context["retrieval"],
                    context_prepare_ms=max(
                        0, int((contexts[0].occurred_at - step.occurred_at).total_seconds() * 1000)
                    ),
                )
                dispatched = any(
                    e.type == "request/snapshot" and e.data["step_id"] == step_id for e in events
                )
                matches = [
                    (i, j)
                    for i, j in enumerate(judgments)
                    if j["role"] == role and normalize(j["query"]) == query
                ]
                row["status"] = "unjudged" if dispatched else "unavailable"
                forbidden = {(item["kind"], item["id"]) for item in seed_receipt["forbidden"]}
                if matches:
                    forbidden.update((i["kind"], i["id"]) for i in matches[0][1]["forbidden"])
                if dispatched:
                    row["scope_violations"] = len(
                        {
                            (b["kind"], b["id"])
                            for b in context["blocks"]
                            if (b["kind"], b["id"]) in forbidden
                            or (
                                b["kind"] == "memory"
                                and b["provenance"]["project_id"]
                                != seed_receipt["bindings"]["current"]["project_id"]
                            )
                        }
                    )
                if matches and dispatched:
                    index, judgment = matches[0]
                    matched.add(index)
                    row.update(
                        status="measured",
                        category=judgment["category"],
                        metrics=score_blocks(context["blocks"], judgment, raw["evaluator"]["k"]),
                    )
                    row["metrics"]["scope_violations"] = row["scope_violations"]
            observed.append(row)
    missing = [
        {"role": j["role"], "query": j["query"], "category": j["category"]}
        for i, j in enumerate(judgments)
        if i not in matched
    ]
    thresholds = raw["evaluator"]["thresholds"]
    passed = None if thresholds is None or not judgments else not missing
    if passed is not None:
        for row in observed:
            if row["status"] != "unavailable":
                passed &= row["scope_violations"] == 0
                passed &= row["context_bytes"] <= raw["evaluator"]["max_context_bytes"]
                passed &= row["context_prepare_ms"] <= raw["evaluator"]["max_context_prepare_ms"]
            if row["status"] != "measured":
                continue
            metrics = row["metrics"]
            bound = thresholds[row["category"]]
            passed &= metrics["scope_violations"] == 0 and all(
                metrics[key] is None or metrics[key] >= bound[key] for key in bound
            )
            passed &= row["context_bytes"] <= raw["evaluator"]["max_context_bytes"]
            passed &= row["context_prepare_ms"] <= raw["evaluator"]["max_context_prepare_ms"]
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_digest": spec.digest,
        "input_digest": spec.file_digest,
        "environment": environment(),
        "seed": seed_receipt,
        "observations": observed,
        "query_summaries": [
            {
                "role": j["role"],
                "query": j["query"],
                "category": j["category"],
                "observations": len(rows),
                "unproven": not rows,
                "means": {
                    key: (sum(values) / len(values) if values else None)
                    for key in ("recall", "mrr", "precision", "zero_hit")
                    for values in [
                        [r["metrics"][key] for r in rows if r["metrics"][key] is not None]
                    ]
                },
            }
            for j in judgments
            for rows in [
                [
                    r
                    for r in observed
                    if r["status"] == "measured"
                    and r["role"] == j["role"]
                    and r["query"] == normalize(j["query"])
                ]
            ]
        ],
        "expected_judgments": len(judgments),
        "unproven": missing,
        "unavailable_steps": sum(r["status"] == "unavailable" for r in observed),
        "scope_violations": sum(r.get("scope_violations", 0) for r in observed),
        "quality_passed": passed,
        "k": raw["evaluator"]["k"],
        "note": "Per unique Step; candidate ranks differ from injection. No significance claim. "
        "Context preparation includes source validation; isolated ranking latency unavailable.",
    }
