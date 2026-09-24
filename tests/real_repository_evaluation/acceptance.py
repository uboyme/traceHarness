"""Reference-driven plumbing acceptance through the original EvaluationRunner.

This provider is explicitly an answer injector, never an autonomous agent or an
experimental baseline. It owns no workspace, lifecycle, verification or score.
"""

import argparse
import asyncio
import json
from pathlib import Path

from traceh.api.llm import ModelResponse, ToolCall, Usage, UsageQuality
from traceh.evaluation.runner import EvaluationRunner
from traceh.sandbox.config import load_sandbox_file


class ReferenceProvider:
    name = "offline-reference-injector"

    def __init__(self, materials, *, variant):
        self.variant = variant
        self.cases = []
        for path in sorted((materials / "host").glob("*/instance.json")):
            row = json.loads(path.read_bytes())
            edits = []
            for line in row["patch"].splitlines():
                if not line.startswith("diff --git "):
                    continue
                name = line.split(" b/", 1)[1]
                original = materials / "material" / row["instance_id"] / "initial" / name
                final = path.parent / "reference" / name
                before, after = (
                    original.read_text(encoding="utf-8"),
                    final.read_text(encoding="utf-8"),
                )
                if variant == "unfixed":
                    after = (
                        before + "\n# Offline negative control: defect intentionally retained.\n"
                    )
                edits.append(dict(path=name, old_text=before, new_text=after))
            self.cases.append((row["problem_statement"].strip(), edits))

    async def complete(self, request):
        text = "\n".join(
            message.content for message in request.messages if isinstance(message.content, str)
        )
        candidates = [edits for requirement, edits in self.cases if requirement in text]
        if len(candidates) != 1:
            raise ValueError("offline reference provider requires one exact task requirement")
        tools = {tool.name for tool in request.tools}
        if "apply_patch" not in tools:
            raise ValueError("offline acceptance requires the explicit single coding arm")
        completed = sum(
            message.role == "tool" and message.name == "apply_patch" for message in request.messages
        )
        edits = candidates[0]
        if completed < len(edits):
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        id=f"reference-edit-{completed}",
                        name="apply_patch",
                        arguments=edits[completed],
                    ),
                ),
                usage=Usage(0, 0, UsageQuality.EXACT),
            )
        return ModelResponse(
            content="Offline answer-injection acceptance; not agent performance.",
            usage=Usage(0, 0, UsageQuality.EXACT),
        )


async def run(materials, sandbox, output, variant):
    runner = EvaluationRunner(
        materials / "material",
        output,
        provider=ReferenceProvider(materials, variant=variant),
        model_id="offline-reference-" + variant,
        sandbox=load_sandbox_file(sandbox).policy,
    )
    report = await runner.run()
    print(
        "Original EvaluationRunner returned; measurement complete:", report.complete,
        "inspect", output / "report.json", flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("materials", "sandbox", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("reference", "unfixed"))
    options = parser.parse_args()
    report = asyncio.run(
        run(
            options.materials.resolve(),
            options.sandbox.resolve(),
            options.output.resolve(),
            options.variant,
        )
    )
    expected = "passed" if options.variant == "reference" else "failed"
    raise SystemExit(
        0 if report.complete and all(t.assessment.value == expected for t in report.trials) else 1
    )
