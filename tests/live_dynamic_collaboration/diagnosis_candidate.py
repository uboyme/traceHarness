"""One explicit, isolated text hypothesis; never install or adopt the candidate."""

import argparse
import ast
import json
import zipfile
from pathlib import Path

from live_dynamic_collaboration.materials import write
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.variants import (
    apply_candidate,
    environment_identity,
    source_digest,
    source_files,
    text_node,
)

TEXTS = {
    "DELEGATE_GUIDANCE": (
        "Ask a readonly investigator one independent, bounded question about the task's original "
        "source revision. After inspecting the task, consider this when separate source areas or "
        "competing explanations can be checked independently while you make progress elsewhere. "
        "Keep simple or tightly coupled work local; delegation is optional, not a completion gate. "
        "Do not forward the whole task. Name the specific uncertainty, relevant paths or symbols "
        "already found, and a short evidence deliverable. The child gets only your explicit work "
        "message and the original checkout, not your conversation or uncommitted edits. Ask it to "
        "search targeted symbols and read relevant ranges, then return the supported conclusion "
        "with file/line evidence and remaining uncertainty; avoid exhaustive file dumps. "
        "Work on a different part and collect the exact returned message before using its findings."
    ),
    "COLLECT_GUIDANCE": (
        "Read the report for one exact owned agent_id and message_id. Wait up to 30 seconds; "
        "zero only polls. Pending means the investigation is unfinished: continue independent "
        "work and collect again when its answer is needed, or stop it if no longer useful. "
        "Do not treat one pending poll as a completed handoff. Failed or cancelled reports are "
        "diagnostics, not a supported answer; investigate the remaining question yourself if "
        "needed. A completed report is a child claim, not verified truth or approval. Check its "
        "cited source evidence before relying on it, and finish the user's requested deliverable."
    ),
}


def candidate_patch(files):
    name = "supervision/delegation.py"
    tree = ast.parse(dict(files)[name])
    return {
        "format": 1,
        "base_source_digest": source_digest(files),
        "edits": [
            {
                "file": name,
                "selector": selector,
                "old_sha256": digest_bytes(text_node(tree, selector).value.encode("utf-8")),
                "new_text": text,
            }
            for selector, text in TEXTS.items()
        ],
    }


def prepare(baseline, output):
    contract = json.loads((baseline / "contract.json").read_text(encoding="utf-8"))
    _, files = source_files()
    if source_digest(files) != contract["source_digest"]:
        raise ValueError("diagnostic-baseline-source-mismatch")
    patch = candidate_patch(files)
    candidate = apply_candidate(files, patch)
    output.mkdir(parents=True, exist_ok=False)
    for label, contents in (("baseline", files), ("candidate", candidate)):
        with zipfile.ZipFile(output / (label + "-source.zip"), "x") as archive:
            for name, raw in contents:
                archive.writestr(name, raw)
                if label == "candidate":
                    target = output / "code/traceh" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
    drivers = {}
    for path in sorted(Path(__file__).parent.glob("*.py")):
        raw = path.read_bytes()
        drivers[path.name] = digest_bytes(raw)
        target = output / "drivers" / path.name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)
    write(output / "patch.json", patch)
    write(
        output / "contract.json",
        {
            "format": 1,
            "base_source_digest": source_digest(files),
            "candidate_source_digest": source_digest(candidate),
            "baseline_contract_sha256": digest_bytes((baseline / "contract.json").read_bytes()),
            "dataset_digest": contract["dataset_digest"],
            "manifest_digest": contract["manifest_digest"],
            "environment": environment_identity(),
            "drivers": drivers,
            "archive_timing": "after baseline completion, before candidate execution",
            "hypothesis": (
                "Bounded work definition and explicit report-state guidance may improve handoff."
            ),
            "comparison_limit": (
                "Six sequential trials per arm; not randomized or statistical proof."
            ),
            "new_runtime_logic": False,
            "adoption_authorized": False,
        },
    )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.baseline.resolve(), args.output.resolve()))
