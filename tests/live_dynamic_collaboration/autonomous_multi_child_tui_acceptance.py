"""Autonomous multi-child acceptance through the real TUI and product chain.

The frozen requirement names no assistant count. The host authorizes up to four
direct writable assistants, while the model chooses the actual assignment count.
At least two distinct child identities must reach the TUI conversation view for
this run to count as a multi-child TUI acceptance.
"""

import argparse
import asyncio
import json
from pathlib import Path

from test_tui_config_forms import click, edit
from textual.app import App
from textual.widgets import Static

from live_dynamic_collaboration import tui_acceptance as driver
from live_dynamic_collaboration.autonomy_materials import REQUIREMENT
from traceh.evaluation.inputs import digest_bytes
from traceh.product.config import load_product_host_file
from traceh.tui.config_forms import ConfigForm

MAX_REAL_CALLS = 60
TIMEOUT_SECONDS = 900
MIN_ASSISTANTS = 2


def _round_digest():
    return digest_bytes(Path(__file__).read_bytes())


async def _save_autonomous_limits_through_tui(root, settings):
    raw = json.loads((root / "product.json").read_text(encoding="utf-8"))

    class FormApp(App):
        def on_mount(self):
            path = root / "product.json"
            self.push_screen(
                ConfigForm("product", path, raw, expected_bytes=path.read_bytes())
            )

    app = FormApp()
    async with app.run_test(size=(125, 45)) as pilot:
        for key, value in settings["task_budget"].items():
            await edit(app, pilot, ("task_budget", key), value)
        await edit(app, pilot, ("retained_tokens",), settings["retained_tokens"])
        for key, value in settings["roles"]["coder"]["budget"].items():
            await edit(app, pilot, ("roles", "coder", "budget", key), value)
        await edit(
            app,
            pilot,
            ("roles", "coder", "max_turn_wall_milliseconds"),
            settings["roles"]["coder"]["max_turn_wall_milliseconds"],
        )
        for key, value in settings["roles"]["patch_author"]["budget"].items():
            await edit(app, pilot, ("roles", "patch_author", "budget", key), value)
        for path, value in (
            (
                ("roles", "patch_author", "max_turn_wall_milliseconds"),
                settings["roles"]["patch_author"]["max_turn_wall_milliseconds"],
            ),
            (
                ("roles", "patch_author", "max_output_tokens"),
                settings["roles"]["patch_author"]["max_output_tokens"],
            ),
        ):
            await edit(app, pilot, path, value)
        app.save_screenshot("01b-autonomous-multi-config.svg", path=root)
        await click(app, pilot, "#config-save")
        if isinstance(app.screen, ConfigForm):
            status = str(app.screen.query_one("#config-form-status", Static).content)
            raise ValueError(f"autonomous-tui-config-not-saved: {status}")


async def prepare(options):
    await driver.prepare(options)
    root = options.output.resolve()
    material = options.material.resolve()
    case = json.loads((root / "case.json").read_text(encoding="utf-8"))
    if case["requirement"] != REQUIREMENT:
        raise ValueError("autonomous-tui-requirement-drift")
    settings = json.loads((material / "benchmark.json").read_text(encoding="utf-8"))[
        "task_settings"
    ]
    await _save_autonomous_limits_through_tui(root, settings)
    profile = load_product_host_file(root / "product.json").host_profile.profile
    if profile.patch_author is None:
        raise ValueError("autonomous-tui-patch-author-disabled")
    authorized = profile.coder.budget.max_children
    if authorized is None or authorized < MIN_ASSISTANTS:
        raise ValueError("autonomous-tui-multi-child-not-authorized")
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    contract.update(
        kind="autonomous-multi-child-real-tui-acceptance",
        assistant_count_named_in_requirement=False,
        authorized_assistants=authorized,
        minimum_observed_assistants=MIN_ASSISTANTS,
        max_real_calls=MAX_REAL_CALLS,
        timeout_seconds=TIMEOUT_SECONDS,
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
            "The requirement names no assistant count. Multi mode requires allocation, so "
            "this observes decomposition granularity and TUI delivery, not whether the model "
            "would choose collaboration in single mode or whether concurrency is faster."
        ),
    )
    driver.write(root / "contract.json", contract)
    print("Prepared autonomous multi-child TUI acceptance; zero model calls.")


async def run(options):
    root = options.output.resolve()
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    if contract["round_driver_digest"] != await asyncio.to_thread(_round_digest):
        raise ValueError("autonomous-multi-child-tui-driver-drift")
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
    if len(agents) < contract["minimum_observed_assistants"]:
        raise SystemExit(
            "expected autonomous multi-child decomposition in TUI, "
            f"observed child identities: {agents}"
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
