"""Isolated diagnostic: bounded draft review through the existing continuation hook.

The in-memory flag belongs only to this experiment, not a proposed production owner.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from live_active_retrieval import grid
from live_active_retrieval.history_smoke import load_provider
from traceh.runtime.continuation import Continue, DefaultContinuationRuntime, Finish

FEEDBACK = (
    "Your previous response is a draft. Review its evidence before answering the original "
    "user request. A claim based only on a preview, directory, or literal search non-match "
    "does not establish what the entire source contains. There may or may not be an answer. "
    "If useful, call the permitted source search/read tools to check the missing evidence. "
    "If you cannot check enough, state the unresolved scope rather than infer absence. "
    "Do not merely promise to search; use an actual tool call when evidence is needed."
)


class ReviewOnce:
    def __init__(self, scope):
        self.scope = scope
        self.reviewed = False
        self.default = DefaultContinuationRuntime()

    async def decide(self, **kwargs):
        if kwargs["step_number"] == 1:
            self.reviewed = False
        decision = await self.default.decide(**kwargs)
        if (not self.scope.setup and not self.reviewed
                and isinstance(decision, Finish) and decision.reason == "completed"):
            self.reviewed = True
            return Continue((FEEDBACK,))
        return decision


async def main(args):
    root = args.output.resolve()
    frozen = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    assert grid.source_files(grid.installed_source_root()) == frozen["source_files"][args.arm]
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == frozen["runner_sha256"]
    if args.arm == "candidate":
        original = grid.build_default_runtime_async

        async def build(*pos, **kwargs):
            kwargs["continuation"] = ReviewOnce(kwargs["policies"][0])
            return await original(*pos, **kwargs)

        grid.build_default_runtime_async = build
    provider, model = load_provider(args.profile)
    assert (provider.name, model) == (frozen["provider"], frozen["model"])
    provider.timeout_seconds = frozen["manifest"]["limits"]["provider_timeout_seconds"]
    arm = root / args.arm
    arm.mkdir(exist_ok=False)
    for fixture in frozen["fixtures"]:
        report = await grid.run_case(arm / fixture["identity"], fixture, frozen, provider, model)
        print(json.dumps({"arm": args.arm, "case": fixture["identity"],
                          "answer": report.get("answer"), "error": report.get("error"),
                          "provisional": report["provisional_joint_pass"]},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    asyncio.run(main(parser.parse_args()))
