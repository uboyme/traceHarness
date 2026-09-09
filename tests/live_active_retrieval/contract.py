"""Read and freeze the synthetic AR evaluation before any scored Provider call."""

import hashlib
import json
from collections import Counter
from pathlib import Path


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != 1:
        raise ValueError("ar-manifest-format")
    cases, gates = data["cases"], data["acceptance"]
    if len(cases) != gates["case_count"] or len(data["repeat_seeds"]) != gates["repeats"]:
        raise ValueError("ar-manifest-count")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("ar-manifest-duplicate")
    counts = Counter(c["family"] for c in cases)
    if set(counts) != {"history", "skill", "memory", "output"} or any(
        value != gates["per_family_count"] for value in counts.values()
    ):
        raise ValueError("ar-manifest-family")
    for case in cases:
        if (
            not case["question"].strip()
            or not case["source_title"].strip()
            or case["language"] not in {"zh", "en"}
            or case["expected"] not in {"value", "no-evidence"}
            or case["value_type"] not in {"code", "number", "location", "none"}
            or not 0 <= case["domain"] < len(data["fixtures"]["domains"])
        ):
            raise ValueError("ar-manifest-case")
        if case["expected"] == "value" and "{value}" not in case["source_body"]:
            raise ValueError("ar-manifest-no-source-evidence")
        if "{value}" in case["question"] or any(
            word in case["question"]
            for word in (
                "search_history",
                "search_skill",
                "search_memory",
                "request_history_page",
                "request_skill_reference",
                "request_workspace_memory",
                "next_cursor",
            )
        ):
            raise ValueError("ar-manifest-question-leaks-instructions")
    if any(type(v) not in {int, float} or v < 0 for v in data["limits"].values()):
        raise ValueError("ar-manifest-limits")
    if not 0 < gates["minimum_joint_pass_per_model"] <= len(cases) * gates["repeats"]:
        raise ValueError("ar-manifest-gate")
    return data


def freeze_manifest(manifest, output):
    """Exclusive creation prevents silently replacing a scored contract."""
    data = load_manifest(manifest)
    files = (Path(manifest), Path(__file__))
    frozen = {
        "format": 1,
        "baseline_commit": data["baseline_commit"],
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "acceptance": data["acceptance"],
        "repeat_seeds": data["repeat_seeds"],
        "limits": data["limits"],
    }
    with Path(output).open("x", encoding="utf-8") as handle:
        json.dump(frozen, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return frozen


def verify_manifest(manifest, frozen):
    data = load_manifest(manifest)
    for path in (Path(manifest), Path(__file__)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != frozen["files"][path.name]:
            raise ValueError("ar-frozen-manifest-changed")
    if any(data[key] != frozen[key] for key in ("acceptance", "repeat_seeds", "limits")):
        raise ValueError("ar-frozen-gates-changed")
    return data
