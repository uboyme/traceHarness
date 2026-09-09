"""Explicit offline model feasibility screen; never alters Runtime or Session evidence."""

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path

from traceh.api.retrieval import ReferenceRetrievalPolicy
from traceh.evaluation.retrieval import score_blocks
from traceh.session.context_input import (
    ContextInputPolicy,
    _admit_candidates,
    _fused_references,
)
from traceh.session.retrieval import (
    _literal_spans,
    block_identity,
    normalize,
    query_terms,
    rank,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def freeze(repository, output, embedding, reranker):
    sources = [*sorted((repository / "src").rglob("*.py"))]
    for folder in (
        "tests",
        "benchmarks/retrieval_v1",
        "examples/plugins/traceh-reference-skills/src",
    ):
        sources.extend(p for p in (repository / folder).rglob("*") if p.suffix in {".py", ".json"})
    assets = {}
    for name, path in (("embedding", embedding), ("reranker", reranker)):
        path = path.resolve(strict=True)
        files = sorted(
            p
            for p in path.rglob("*")
            if p.is_file() and p.suffix in {".json", ".txt", ".safetensors"}
        )
        if not (path / "model.safetensors").is_file():
            raise ValueError("c3-explicit-safe-weights-required")
        assets[name] = {
            "path": str(path),
            "files": {p.relative_to(path).as_posix(): digest(p) for p in files},
            "bytes": sum(p.stat().st_size for p in files),
        }
    write(
        output,
        {
            "format": 1,
            "created_unix": time.time(),
            "assets": assets,
            "sources": {
                p.relative_to(repository).as_posix(): digest(p) for p in sorted(set(sources))
            },
            "criteria": json.loads(
                (Path(__file__).with_name("cases.json")).read_text(encoding="utf-8")
            ),
        },
    )


def verify(repository, frozen):
    for relative, expected in frozen["sources"].items():
        if digest(repository / relative) != expected:
            raise ValueError("c3-frozen-source-changed")
    for asset in frozen["assets"].values():
        for relative, expected in asset["files"].items():
            if digest(Path(asset["path"]) / relative) != expected:
                raise ValueError("c3-frozen-asset-changed")


def lexical(observation, query, policy):
    """Reapply original pure rank/admission to already captured eligible source rows."""
    blocks, receipts = [], {"skill": None, "memory": None, "fusion": []}
    for lane in observation["lanes"]:
        owner_policy = ReferenceRetrievalPolicy.from_dict(lane["policy"])
        hits = (
            lane["hits"]
            if query == lane["query"]
            else [
                row["identity"] for row in lane["rows"] if set(row["tf"]) & set(query_terms(query))
            ]
        )
        candidates, unavailable, receipt = rank(
            lane["blocks"], lane["rows"], lane["values"], query, hits, owner_policy
        )
        if unavailable:
            raise ValueError("c3-baseline-lane-unavailable")
        blocks.extend(candidates)
        if lane["rows"]:
            receipts[lane["rows"][0]["kind"]] = receipt
    receipts["fusion"] = _fused_references(receipts)
    return _admit_candidates(blocks, [], policy, receipts, {})[0]


def screen_admission(blocks, policy):
    """Use original byte/block budget for hypothetical order; no persisted receipt."""
    receipts = {"skill": {"coverage": []}, "memory": {"coverage": []}, "fusion": []}
    for block in blocks:
        identity = block_identity(block)
        receipts[block["kind"]]["coverage"].append({"identity": identity, "terms": []})
        receipts["fusion"].append({"identity": identity})
    return _admit_candidates(blocks, [], policy, receipts, {})[0]


def select(base, blocks, query, scores, reranked, candidate, k, policy):
    if base or _literal_spans(normalize(query)):
        return base
    eligible = [
        i
        for i, score in enumerate(scores)
        if score >= candidate["cosine_min"]
        and (candidate["reranker_min"] is None or reranked[i] >= candidate["reranker_min"])
    ]
    ranking = sorted(
        eligible,
        key=lambda i: (
            -(scores[i] if candidate["reranker_min"] is None else reranked[i]),
            block_identity(blocks[i]),
        ),
    )
    return screen_admission([blocks[i] for i in ranking[:k]], policy)


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def observation_for_query(observations, query):
    # Runtime freezes NFKC/casefold queries; the original evaluator uses the same contract.
    matches = [
        item for item in observations if item["context"]["query"]["text"] == normalize(query)
    ]
    if len(matches) != 1:
        raise ValueError("c3-original-observation-not-unique")
    return matches[0]


def run(repository, frozen_path, capture_path, output):
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    verify(repository, frozen)
    criteria = frozen["criteria"]
    if (
        criteria["embedding"]["pooling"] != "attention-mask-mean"
        or criteria["embedding"]["normalization"] != "l2"
        or criteria["embedding"]["device"] != "cpu"
        or criteria["embedding"]["dtype"] != "float32"
        or criteria["reranker"]["activation"] != "identity"
        or criteria["reranker"]["device"] != "cpu"
        or criteria["reranker"]["dtype"] != "float32"
    ):
        raise ValueError("c3-model-contract-unsupported")
    corpus = json.loads(
        (repository / "benchmarks/retrieval_v1/corpus.json").read_text(encoding="utf-8")
    )
    observations = json.loads(capture_path.read_text(encoding="utf-8"))
    pairs = []
    for judgment in corpus["judgments"]:
        pairs.append((judgment, observation_for_query(observations, judgment["query"])))
    reference = pairs[0][1]
    # Every synthetic attempt has the same eligible canonical texts; proof IDs differ by Session.
    texts = sorted(reference["texts"].values())
    if any(sorted(o["texts"].values()) != texts for _, o in pairs):
        raise ValueError("c3-original-corpora-differ")
    for extra in criteria["additional_queries"]:
        pairs.append(
            (
                {
                    **extra,
                    "category": "added-semantic",
                    "forbidden": corpus["judgments"][0]["forbidden"],
                },
                reference,
            )
        )
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import numpy as np
    import torch
    import transformers
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(criteria["embedding"]["threads"])
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    load_start = time.perf_counter()
    embedding_path = frozen["assets"]["embedding"]["path"]
    reranker_path = frozen["assets"]["reranker"]["path"]
    tokenizer = AutoTokenizer.from_pretrained(
        embedding_path, local_files_only=True, trust_remote_code=False
    )
    model = (
        AutoModel.from_pretrained(
            embedding_path, local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
        .float()
        .eval()
    )
    cross_tokenizer = AutoTokenizer.from_pretrained(
        reranker_path, local_files_only=True, trust_remote_code=False
    )
    cross = (
        AutoModelForSequenceClassification.from_pretrained(
            reranker_path, local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
        .float()
        .eval()
    )
    cold_ms = (time.perf_counter() - load_start) * 1000
    truncations = []

    def encode(strings):
        encoded = tokenizer(
            strings,
            padding=True,
            truncation=True,
            max_length=criteria["embedding"]["max_length"],
            return_tensors="pt",
        )
        with torch.inference_mode():
            hidden = model(**encoded).last_hidden_state
            mask = encoded["attention_mask"].unsqueeze(-1)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
            return torch.nn.functional.normalize(pooled, p=2, dim=1).numpy()

    def cross_scores(query, documents):
        if not documents:
            return np.array([], dtype=np.float32)
        encoded = cross_tokenizer(
            [query] * len(documents),
            documents,
            padding=True,
            truncation=True,
            max_length=criteria["reranker"]["max_length"],
            return_tensors="pt",
        )
        with torch.inference_mode():
            return cross(**encoded).logits.squeeze(-1).numpy()

    for value in [*texts, *(j["query"] for j, _ in pairs)]:
        length = len(tokenizer(value, truncation=False)["input_ids"])
        if length > criteria["embedding"]["max_length"]:
            truncations.append(
                {"text_sha256": hashlib.sha256(value.encode()).hexdigest(), "tokens": length}
            )
    rebuild_start = time.perf_counter()
    vectors = encode(texts)
    rebuild_ms = (time.perf_counter() - rebuild_start) * 1000
    repeated = encode(texts)
    if (
        vectors.shape != (len(texts), criteria["embedding"]["dimension"])
        or not np.isfinite(vectors).all()
    ):
        raise ValueError("c3-embedding-shape-invalid")
    if not np.allclose(vectors, repeated, atol=1e-6, rtol=0):
        raise ValueError("c3-rebuild-not-reproducible")
    output.mkdir(parents=True, exist_ok=False)
    np.save(output / "vectors.npy", vectors)
    results, timings = [], {c["id"]: [] for c in criteria["candidates"]}
    k = corpus["evaluator"]["k"]
    for judgment, observation in pairs:
        query = judgment["query"]
        policy = ContextInputPolicy.from_dict(observation["context"]["policy"]["config"])
        base = lexical(observation, query, policy)
        if judgment["category"] != "added-semantic" and base != observation["context"]["blocks"]:
            raise ValueError("c3-baseline-context-reproduction-failed")
        by_text = {text: identity for identity, text in observation["texts"].items()}
        by_identity = {
            block_identity(b): b for lane in observation["lanes"] for b in lane["blocks"]
        }
        blocks = [by_identity[by_text[text]] for text in texts]
        scores = (encode([query]) @ vectors.T)[0]
        reranked = cross_scores(query, texts)
        if (
            scores.shape != (len(texts),)
            or reranked.shape != (len(texts),)
            or not np.isfinite(scores).all()
            or not np.isfinite(reranked).all()
        ):
            raise ValueError("c3-model-scores-invalid")
        record = {
            "query": query,
            "category": judgment["category"],
            "language": judgment.get("language"),
            "lexical": score_blocks(base, judgment, k),
            "scores": [
                {"kind": b["kind"], "id": b["id"], "cosine": float(s), "reranker": float(r)}
                for b, s, r in zip(blocks, scores, reranked, strict=True)
            ],
            "candidates": [],
        }
        for candidate in criteria["candidates"]:
            selected = select(base, blocks, query, scores, reranked, candidate, k, policy)
            for _ in range(5):
                started = time.perf_counter()
                fresh = (encode([query]) @ vectors.T)[0]
                applicable = [
                    i for i, value in enumerate(fresh) if value >= candidate["cosine_min"]
                ]
                if candidate["reranker_min"] is not None:
                    cross_scores(query, [texts[i] for i in applicable])
                timings[candidate["id"]].append((time.perf_counter() - started) * 1000)
            record["candidates"].append(
                {
                    "id": candidate["id"],
                    "metrics": score_blocks(selected, judgment, k),
                    "admitted": [{"kind": b["kind"], "id": b["id"]} for b in selected],
                }
            )
        results.append(record)
    accepted = []
    gates = criteria["acceptance"]
    summaries = []
    for candidate in criteria["candidates"]:
        rows = [
            (r, next(c for c in r["candidates"] if c["id"] == candidate["id"])["metrics"])
            for r in results
        ]
        original_ok = all(
            all(
                m[name] is None or m[name] >= threshold
                for name, threshold in corpus["evaluator"]["thresholds"][r["category"]].items()
            )
            for r, m in rows
            if r["category"] not in {"semantic", "added-semantic"}
        )
        original_semantic_ok = all(
            all(
                m[name] >= gates["original_semantic_" + name]
                for name in ("recall", "precision", "mrr")
            )
            for r, m in rows
            if r["category"] == "semantic"
        )
        language = {}
        for name in sorted({r["language"] for r in results if r["language"]}):
            group = [m for r, m in rows if r["language"] == name]
            language[name] = {
                metric: sum(m[metric] for m in group) / len(group)
                for metric in ("recall", "precision")
            }
        semantic_rows = [(r, m) for r, m in rows if r["category"] in {"semantic", "added-semantic"}]
        gain = sum(m["recall"] - r["lexical"]["recall"] for r, m in semantic_rows) / len(
            semantic_rows
        )
        size = frozen["assets"]["embedding"]["bytes"] + (
            frozen["assets"]["reranker"]["bytes"] if candidate["reranker_min"] is not None else 0
        )
        p95 = percentile(timings[candidate["id"]], 0.95)
        checks = {
            "original_nonsemantic": original_ok,
            "original_semantic": original_semantic_ok,
            "languages": all(
                values[metric] >= gates["added_semantic_" + metric + "_per_language"]
                for values in language.values()
                for metric in ("recall", "precision")
            ),
            "scope": all(m["scope_violations"] == gates["scope_violations"] for _, m in rows),
            "gain": gain >= gates["minimum_gain_over_lexical_semantic_recall"],
            "cost": p95 <= gates["warm_query_p95_ms"]
            and cold_ms <= gates["cold_load_ms"]
            and rebuild_ms <= gates["rebuild_ms"]
            and size <= gates["model_assets_bytes"]
            and vectors.nbytes / len(texts) <= gates["vector_bytes_per_item"],
        }
        if all(checks.values()):
            accepted.append(candidate["id"])
        summaries.append(
            {
                "id": candidate["id"],
                "checks": checks,
                "language": language,
                "semantic_recall_gain": gain,
                "warm_p95_ms": p95,
                "asset_bytes": size,
            }
        )
    write(
        output / "result.json",
        {
            "format": 1,
            "freeze_sha256": digest(frozen_path),
            "capture_sha256": digest(capture_path),
            "phase": "pre-integration-feasibility-only",
            "runtime_semantic_enabled": False,
            "accepted_for_integration": accepted,
            "summaries": summaries,
            "results": results,
            "cost": {
                "cold_load_both_models_ms": cold_ms,
                "rebuild_ms": rebuild_ms,
                "eligible_items": len(texts),
                "vector_bytes": vectors.nbytes,
                "vector_file_bytes": (output / "vectors.npy").stat().st_size,
                "truncations": truncations,
                "warm_samples_ms": timings,
            },
            "versions": {
                "torch": torch.__version__,
                "transformers": transformers.__version__,
                "numpy": np.__version__,
            },
        },
    )
    print(
        json.dumps(
            {"accepted_for_integration": accepted, "summaries": summaries},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("freeze")
    pre.add_argument("--embedding", type=Path, required=True)
    pre.add_argument("--reranker", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--freeze", type=Path, required=True)
    execute.add_argument("--capture", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    if args.command == "freeze":
        freeze(repository, args.output, args.embedding, args.reranker)
    else:
        run(repository, args.freeze, args.capture, args.output)
