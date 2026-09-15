"""Two-assistant acceptance through the real terminal and original product chain.

Reuses the record-070 TUI driver end to end. This module only freezes this
round's caps, raises the host's assistant-count authorization and per-role
allowances in the saved production configuration, and tightens the conversation
checkpoint to require two distinct patch_author identities, not one role label.
"""

import argparse
import asyncio
import json
from pathlib import Path

from live_dynamic_collaboration import tui_acceptance as driver
from traceh.evaluation.inputs import digest_bytes

MAX_REAL_CALLS = 60
TIMEOUT_SECONDS = 900
ASSISTANTS = 2


def _round_digest():
    return digest_bytes(Path(__file__).read_bytes())


async def prepare(options):
    await driver.prepare(options)
    root = options.output.resolve()
    settings = json.loads(
        (options.material.resolve() / "benchmark.json").read_text(encoding="utf-8")
    )["task_settings"]
    raw = json.loads((root / "product.json").read_text(encoding="utf-8"))
    # Keep the generated host's own presets and tool grants; only this round's
    # explicit authorization and allowances change. A generated host authorizes
    # one assistant, so the higher ceiling is stated here and nowhere else.
    raw["task_budget"] = settings["task_budget"]
    raw["retained_tokens"] = settings["retained_tokens"]
    raw["roles"]["coder"]["budget"] = settings["roles"]["coder"]["budget"]
    raw["roles"]["coder"]["max_turn_wall_milliseconds"] = settings["roles"]["coder"][
        "max_turn_wall_milliseconds"
    ]
    raw["roles"]["patch_author"]["budget"] = settings["roles"]["patch_author"]["budget"]
    driver.write(root / "product.json", raw)
    from traceh.product.config import load_product_host_file

    profile = load_product_host_file(root / "product.json").host_profile.profile
    assert profile.patch_author is not None
    assert profile.coder.budget.max_children == ASSISTANTS
    assert "apply_patch" in profile.patch_author.capability_grants
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    contract.update(
        kind="multi-child-real-tui-acceptance",
        assistants=ASSISTANTS,
        assistants_required_by_requirement=True,
        max_real_calls=MAX_REAL_CALLS,
        timeout_seconds=TIMEOUT_SECONDS,
        coder_max_children=profile.coder.budget.max_children,
        budgets={
            "task_tokens": profile.task_budget.max_tokens,
            "coder_tokens": profile.coder.budget.max_tokens,
            "assistant_tokens_each": profile.patch_author.budget.max_tokens,
            "retained_main_tokens": profile.retained_tokens,
        },
        round_driver_digest=await asyncio.to_thread(_round_digest),
        files={
            name: digest_bytes((root / name).read_bytes())
            for name in ("product.json", "sandbox.json", "case.json")
        },
        interpretation=(
            "The requirement names the assistant count; this measures the mechanism and "
            "delivery through the real terminal, not a spontaneous choice or a speedup."
        ),
    )
    driver.write(root / "contract.json", contract)
    print("Prepared two-assistant TUI acceptance; zero model calls.")


async def run(options):
    root = options.output.resolve()
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    if contract["round_driver_digest"] != await asyncio.to_thread(_round_digest):
        raise ValueError("multi-child-tui-driver-drift")
    observed = {}
    from traceh.tui import task_conversation as conversation

    original_load = conversation.TaskConversationReader.load

    async def load(self, task_id):
        snapshot = await original_load(self, task_id)
        rows = [role for role in snapshot.roles if role.role == "patch_author"]
        observed.update(
            patch_author_agents=sorted({role.agent_id for role in rows}),
            roles=[role.role for role in snapshot.roles],
            agents=[role.agent_id for role in snapshot.roles],
            usage=[role.usage_tokens for role in snapshot.roles],
            tool_calls=[role.tool_calls for role in snapshot.roles],
        )
        driver.write(root / "conversation.json", observed)
        return snapshot

    conversation.TaskConversationReader.load = load
    try:
        await driver.run(options)
    finally:
        conversation.TaskConversationReader.load = original_load
        driver.write(root / "conversation.json", observed)
    agents = observed.get("patch_author_agents", [])
    if len(agents) != ASSISTANTS:
        raise SystemExit(
            f"expected {ASSISTANTS} distinct patch_author identities, observed {agents}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--material", type=Path)
    parser.add_argument("--sandbox", type=Path)
    options = parser.parse_args()
    asyncio.run(prepare(options) if options.action == "prepare" else run(options))
