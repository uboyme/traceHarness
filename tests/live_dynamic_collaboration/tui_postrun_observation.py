"""Render a completed ProductTask conversation from an immutable replay copy."""

import argparse
import asyncio
import json
from pathlib import Path

from textual.app import App
from textual.widgets import RichLog

from traceh.artifacts.cas import LocalArtifactCas
from traceh.product.config import load_product_host_file
from traceh.product.host import build_product_read_models
from traceh.session.sqlite import SqliteEventStore
from traceh.tui.screens import TaskConversationScreen
from traceh.tui.task_conversation import TaskConversationReader


async def observe(root: Path, minimum_patch_authors: int) -> None:
    store = SqliteEventStore(root / "replay-copy")
    config = load_product_host_file(root / "product.json")
    models = build_product_read_models(
        store=store,
        host_profile=config.host_profile,
        artifact_cas=LocalArtifactCas(config.cas_root),
        max_report_chars=config.max_report_chars,
    )
    try:
        streams = await store.list_streams(prefix="product-task:")
        if len(streams) != 1:
            raise ValueError("postrun-product-task-ambiguous")
        task_id = streams[0].removeprefix("product-task:")
        observation = await models.observation.load(task_id)
        reader = TaskConversationReader(store)
        snapshot = await reader.load(observation)
        patch_authors = [role for role in snapshot.roles if role.role == "patch_author"]
        result = {
            "task_id": task_id,
            "product_status": observation.summary.status.value,
            "roles": [
                {
                    "role": role.role,
                    "agent_id": role.agent_id,
                    "session_id": role.session_id,
                    "turns_started": role.turns_started,
                    "turns_completed": role.turns_completed,
                    "tool_calls": role.tool_calls,
                    "usage_tokens": role.usage_tokens,
                    "usage_quality": role.usage_quality,
                }
                for role in snapshot.roles
            ],
            "patch_author_agents": sorted({role.agent_id for role in patch_authors}),
            "model_calls": 0,
        }
        (root / "conversation.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        class Viewer(App):
            def on_mount(self):
                self.push_screen(
                    TaskConversationScreen(reader, models.observation, task_id)
                )

        app = Viewer()

        async def pilot_flow(pilot):
            try:
                async with asyncio.timeout(30):
                    log = app.screen.query_one("#task-conversation-log", RichLog)
                    while not log.lines:
                        await pilot.pause()
                    await pilot.pause()
                    app.save_screenshot("06-conversation-postrun.svg", path=root)
            finally:
                app.exit()

        await app.run_async(size=(125, 45), auto_pilot=pilot_flow)
        if len(result["patch_author_agents"]) < minimum_patch_authors:
            raise SystemExit(
                f"expected at least {minimum_patch_authors} patch authors, "
                f"observed {result['patch_author_agents']}"
            )
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await store.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--minimum-patch-authors", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(observe(args.root.resolve(), args.minimum_patch_authors))
