"""Explicit bounded bilingual screen; no production semantic activation."""

import argparse
import hashlib
import json
import os
import time
from importlib.metadata import version
from pathlib import Path

from local_retrieval_screen.screen import (
    digest,
    lexical,
    observation_for_query,
    percentile,
    select,
    verify,
    write,
)

from traceh.evaluation.retrieval import score_blocks
from traceh.session.context_input import ContextInputPolicy
from traceh.session.retrieval import block_identity


def freeze(repository, assets_root, capture_path, criteria_path, output):
    criteria = json.loads(criteria_path.read_text(encoding="utf-8"))
    original = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
    for name in ("acceptance", "additional_queries"):
        if criteria[name] != original[name]:
            raise ValueError("retest-original-criteria-changed")
    for encoder in criteria["encoders"]:
        if any(
            encoder[name] != value
            for name, value in {
                "pooling": "cls",
                "normalization": "l2",
                "dtype": "float32",
                "device": "cpu",
            }.items()
        ):
            raise ValueError("retest-encoder-contract-unsupported")
    assets = {}
    metadata = json.loads((assets_root / "metadata.json").read_text(encoding="utf-8"))
    for encoder in criteria["encoders"]:
        item = next(m for m in metadata if m["id"] == encoder["model_id"])
        if item["revision"] != encoder["revision"]:
            raise ValueError("retest-revision-mismatch")
        folder = (assets_root / encoder["model_id"].split("/")[1]).resolve(strict=True)
        paths = [folder / file["rfilename"] for file in item["files"]]
        assets[encoder["id"]] = {
            "path": str(folder),
            "model_id": item["id"],
            "revision": item["revision"],
            "files": {p.name: digest(p) for p in paths},
            "bytes": sum(p.stat().st_size for p in paths),
        }
    if sum(a["bytes"] for a in assets.values()) > criteria["acceptance"]["model_assets_bytes"]:
        raise ValueError("retest-assets-over-limit")
    sources = [
        *repository.joinpath("src").rglob("*.py"),
        *repository.joinpath("tests").rglob("*.py"),
        *repository.joinpath("tests/local_retrieval_screen").glob("*.json"),
        *repository.joinpath("benchmarks/retrieval_v1").rglob("*.json"),
        *repository.joinpath("examples/plugins/traceh-reference-skills/src").rglob("*.py"),
        repository / "docs/plan/TRACEHARNESS_SEMANTIC_RETEST.md",
    ]
    write(
        output,
        {
            "format": 1,
            "created_unix": time.time(),
            "criteria": criteria,
            "assets": assets,
            "sources": {p.relative_to(repository).as_posix(): digest(p) for p in sorted(sources)},
            "capture_path": str(capture_path.resolve()),
            "capture_sha256": digest(capture_path),
            "versions": {
                name: version(name) for name in ("torch", "transformers", "numpy", "tokenizers")
            },
        },
    )


def summarize(results, criteria, corpus, cost):
    summaries, accepted = [], []
    gates = criteria["acceptance"]
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
        semantic_ok = all(
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
        p95 = percentile(cost["warm_samples_ms"][candidate["id"]], 0.95)
        checks = {
            "original_nonsemantic": original_ok,
            "original_semantic": semantic_ok,
            "languages": all(
                values[metric] >= gates["added_semantic_" + metric + "_per_language"]
                for values in language.values()
                for metric in ("recall", "precision")
            ),
            "scope": all(m["scope_violations"] == gates["scope_violations"] for _, m in rows),
            "gain": gain >= gates["minimum_gain_over_lexical_semantic_recall"],
            "cost": p95 <= gates["warm_query_p95_ms"]
            and cost["cold_load_both_ms"] <= gates["cold_load_ms"]
            and cost["rebuild_both_ms"] <= gates["rebuild_ms"]
            and cost["asset_bytes"] <= gates["model_assets_bytes"]
            and cost["vector_bytes_per_item"] <= gates["vector_bytes_per_item"],
        }
        summaries.append(
            {
                "id": candidate["id"],
                "checks": checks,
                "language": language,
                "semantic_recall_gain": gain,
                "warm_p95_ms": p95,
            }
        )
        if all(checks.values()):
            accepted.append(candidate["id"])
    return summaries, accepted


def run(repository, frozen_path, output):
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    verify(repository, frozen)
    capture_path = Path(frozen["capture_path"])
    if digest(capture_path) != frozen["capture_sha256"]:
        raise ValueError("retest-capture-changed")
    if any(version(name) != expected for name, expected in frozen["versions"].items()):
        raise ValueError("retest-runtime-version-changed")
    criteria = frozen["criteria"]
    corpus = json.loads(
        (repository / "benchmarks/retrieval_v1/corpus.json").read_text(encoding="utf-8")
    )
    observations = json.loads(capture_path.read_text(encoding="utf-8"))
    pairs = [(j, observation_for_query(observations, j["query"])) for j in corpus["judgments"]]
    reference = pairs[0][1]
    texts = sorted(reference["texts"].values())
    if any(sorted(o["texts"].values()) != texts for _, o in pairs):
        raise ValueError("retest-eligible-corpora-differ")
    pairs.extend(
        (
            {
                **extra,
                "category": "added-semantic",
                "forbidden": corpus["judgments"][0]["forbidden"],
            },
            reference,
        )
        for extra in criteria["additional_queries"]
    )
    output.mkdir(parents=True, exist_ok=False)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(criteria["threads"])
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    tokenizers, models = {}, {}
    started = time.perf_counter()
    for encoder in criteria["encoders"]:
        identity = encoder["id"]
        folder = frozen["assets"][identity]["path"]
        tokenizers[identity] = AutoTokenizer.from_pretrained(
            folder, local_files_only=True, trust_remote_code=False
        )
        models[identity] = (
            AutoModel.from_pretrained(
                folder, local_files_only=True, trust_remote_code=False, use_safetensors=True
            )
            .to(device="cpu", dtype=torch.float32)
            .eval()
        )
    cold_ms = (time.perf_counter() - started) * 1000

    def encode(strings, encoder, *, query=False):
        identity = encoder["id"]
        values = [(encoder["query_prefix"] if query else "") + text for text in strings]
        inputs = tokenizers[identity](
            values,
            padding=True,
            truncation=True,
            max_length=encoder["max_length"],
            return_tensors="pt",
        )
        with torch.inference_mode():
            hidden = models[identity](**inputs).last_hidden_state[:, 0]
            return torch.nn.functional.normalize(hidden, p=2, dim=1).numpy()

    vectors, diagnostics = {}, []
    started = time.perf_counter()
    for encoder in criteria["encoders"]:
        identity = encoder["id"]
        vector = encode(texts, encoder)
        if vector.shape != (len(texts), encoder["dimension"]) or not np.isfinite(vector).all():
            raise ValueError("retest-vector-invalid")
        vectors[identity] = vector
        np.save(output / f"vectors-{identity}.npy", vector)
    rebuild_ms = (time.perf_counter() - started) * 1000
    for encoder in criteria["encoders"]:
        identity = encoder["id"]
        if not np.allclose(vectors[identity], encode(texts, encoder), atol=1e-6, rtol=0):
            raise ValueError("retest-rebuild-mismatch")
        for query, value in [(False, t) for t in texts] + [(True, j["query"]) for j, _ in pairs]:
            actual = (encoder["query_prefix"] if query else "") + value
            tokens = tokenizers[identity](actual, truncation=False)["input_ids"]
            diagnostics.append(
                {
                    "encoder": identity,
                    "query": query,
                    "text_sha256": hashlib.sha256(value.encode()).hexdigest(),
                    "tokens": len(tokens),
                    "unknown": tokens.count(tokenizers[identity].unk_token_id),
                    "truncated": len(tokens) > encoder["max_length"],
                }
            )
    results, timings = [], {c["id"]: [] for c in criteria["candidates"]}
    query_vectors = {e["id"]: [] for e in criteria["encoders"]}
    encoders = {e["id"]: e for e in criteria["encoders"]}
    k = corpus["evaluator"]["k"]
    for judgment, observation in pairs:
        query = judgment["query"]
        policy = ContextInputPolicy.from_dict(observation["context"]["policy"]["config"])
        base = lexical(observation, query, policy)
        if judgment["category"] != "added-semantic" and base != observation["context"]["blocks"]:
            raise ValueError("retest-lexical-reproduction-mismatch")
        by_text = {text: identity for identity, text in observation["texts"].items()}
        by_identity = {
            block_identity(b): b for lane in observation["lanes"] for b in lane["blocks"]
        }
        blocks = [by_identity[by_text[text]] for text in texts]
        scores = {}
        for name, encoder in encoders.items():
            query_vector = encode([query], encoder, query=True)
            query_vectors[name].append(query_vector[0])
            scores[name] = (query_vector @ vectors[name].T)[0]
        record = {
            "query": query,
            "category": judgment["category"],
            "language": judgment.get("language"),
            "lexical": score_blocks(base, judgment, k),
            "candidates": [],
            "scores": [
                {
                    "kind": b["kind"],
                    "id": b["id"],
                    **{name: float(values[i]) for name, values in scores.items()},
                }
                for i, b in enumerate(blocks)
            ],
        }
        for candidate in criteria["candidates"]:
            merged = np.maximum.reduce([scores[name] for name in candidate["encoders"]])
            selected = select(
                base, blocks, query, merged, None, {**candidate, "reranker_min": None}, k, policy
            )
            for _ in range(5):
                started = time.perf_counter()
                fresh = np.maximum.reduce(
                    [
                        (encode([query], encoders[name], query=True) @ vectors[name].T)[0]
                        for name in candidate["encoders"]
                    ]
                )
                select(
                    base, blocks, query, fresh, None, {**candidate, "reranker_min": None}, k, policy
                )
                timings[candidate["id"]].append((time.perf_counter() - started) * 1000)
            record["candidates"].append(
                {
                    "id": candidate["id"],
                    "metrics": score_blocks(selected, judgment, k),
                    "admitted": [{"kind": b["kind"], "id": b["id"]} for b in selected],
                }
            )
        results.append(record)
    for name, values in query_vectors.items():
        np.save(output / f"queries-{name}.npy", np.stack(values))
    cost = {
        "cold_load_both_ms": cold_ms,
        "rebuild_both_ms": rebuild_ms,
        "asset_bytes": sum(a["bytes"] for a in frozen["assets"].values()),
        "vector_bytes_per_item": sum(v.nbytes for v in vectors.values()) / len(texts),
        "eligible_items": len(texts),
        "warm_samples_ms": timings,
    }
    summaries, accepted = summarize(results, criteria, corpus, cost)
    verify(repository, frozen)
    write(
        output / "result.json",
        {
            "format": 1,
            "phase": "pre-integration-feasibility-only",
            "runtime_semantic_enabled": False,
            "freeze_sha256": digest(frozen_path),
            "capture_sha256": digest(capture_path),
            "results": results,
            "summaries": summaries,
            "accepted_for_integration": accepted,
            "cost": cost,
            "diagnostics": diagnostics,
            "versions": frozen["versions"],
        },
    )
    print(json.dumps({"accepted": accepted, "summaries": summaries}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("freeze")
    pre.add_argument("--assets", type=Path, required=True)
    pre.add_argument("--capture", type=Path, required=True)
    pre.add_argument("--criteria", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--freeze", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if args.command == "freeze":
        freeze(repo, args.assets, args.capture, args.criteria, args.output)
    else:
        run(repo, args.freeze, args.output)
