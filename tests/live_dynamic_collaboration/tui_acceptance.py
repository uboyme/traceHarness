"""Opt-in real terminal acceptance; original CLI assembly and TUI lifecycle.

Explicit frozen material/profile/output arguments only. Never collected by pytest.
"""

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

from live_unified_evaluation.baseline import connection
from promotion_fixtures import git, make_bare_target
from test_tui_config_forms import click, edit, select
from textual.app import App
from textual.widgets import Button, Input, RichLog, Select, Static

from traceh.cli import main as cli
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.product.config import load_product_host_file
from traceh.tui.app import TracehTuiApp
from traceh.tui.config_forms import ConfigForm, product_preset


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def prepare(options):
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    material = options.material.resolve()
    shutil.copytree(material / "initial", root / "source")
    source = root / "source"
    git("init", "--quiet", "--initial-branch=main", cwd=source)
    git("config", "commit.gpgsign", "false", cwd=source)
    git("add", "-A", cwd=source)
    git("commit", "--quiet", "-m", "isolated TUI acceptance fixture", cwd=source)
    make_bare_target(source, root / "target.git")
    shutil.copyfile(options.sandbox, root / "sandbox.json")
    case = json.loads((material / "dataset.json").read_text(encoding="utf-8"))["cases"][0]
    settings = json.loads((material / "benchmark.json").read_text(encoding="utf-8"))[
        "task_settings"
    ]
    args, provider, model = connection(options.profile)
    raw = product_preset(str(source), str(root / "data"), provider.name, model)
    for key in (
        "task_budget",
        "retained_tokens",
        "investigator_initial_tokens",
        "max_report_chars",
    ):
        raw[key] = settings[key]
    for role in ("coder", "investigator"):
        raw["roles"][role] = settings["roles"][role]
    raw["verification"] = case["verification"]
    raw["task_budget"]["max_tokens"] = 480000
    raw["roles"]["coder"]["budget"]["max_tokens"] = 360000
    raw["roles"]["coder"]["max_turn_wall_milliseconds"] = 300000

    class FormApp(App):
        def on_mount(self):
            self.push_screen(ConfigForm("product", root / "product.json", raw))

    app = FormApp()
    async with app.run_test(size=(125, 45)) as pilot:
        await edit(app, pilot, ("approver_id",), "tui-acceptance-reviewer")
        await edit(app, pilot, ("promotion_target", "repository"), str(root / "target.git"))
        await edit(app, pilot, ("promotion_target", "ref"), "refs/heads/main")
        await select(app, pilot, ("default_mode",))
        app.screen.query_one("#config-choice", Select).value = "multi"
        await click(app, pilot, "#config-update")
        await select(app, pilot, ("roles", "patch_author"))
        await click(app, pilot, "#config-toggle")
        app.save_screenshot("01-configuration.svg", path=root)
        await click(app, pilot, "#config-save")
        assert not isinstance(app.screen, ConfigForm)
    parsed = load_product_host_file(root / "product.json")
    assert parsed.host_profile.profile.patch_author is not None
    write(root / "case.json", case)
    write(
        root / "contract.json",
        dict(
            trials=1,
            max_real_calls=40,
            timeout_seconds=720,
            connection_timeout_seconds=60,
            retry_attempts=1,
            model=model,
            provider=provider.name,
            connection_digest=digest_bytes(args.base_url.encode()),
            source_digest=source_digest(source_files()[1]),
            driver_digest=digest_bytes(await asyncio.to_thread(Path(__file__).read_bytes)),
            files={
                name: digest_bytes((root / name).read_bytes())
                for name in ("product.json", "sandbox.json", "case.json")
            },
            source_revision=git("rev-parse", "HEAD", cwd=source),
            promotion="isolated target.git only",
            terminal="real PTY with Textual auto_pilot",
            configuration="programmatic Textual form; saved through production parser",
        ),
    )
    print("Prepared TUI acceptance; zero model calls.")


async def run(options):
    root = options.output.resolve()
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    assert contract["source_digest"] == source_digest(source_files()[1])
    assert contract["driver_digest"] == digest_bytes(
        await asyncio.to_thread(Path(__file__).read_bytes)
    )
    for name, digest in contract["files"].items():
        assert digest_bytes((root / name).read_bytes()) == digest
    args, inner, model = connection(options.profile)
    assert (inner.name, model) == (contract["provider"], contract["model"])
    assert digest_bytes(args.base_url.encode()) == contract["connection_digest"]
    assert git("rev-parse", "HEAD", cwd=root / "source") == contract["source_revision"]
    assert git("status", "--porcelain", cwd=root / "source") == ""
    (root / "started.json").open("x").close()
    started = time.monotonic()
    report = dict(calls=0, completed=False, checkpoints=[], failure=None)

    class Bounded:
        name = inner.name

        async def complete(self, request):
            if report.get("provider_stopped") or report["calls"] >= contract["max_real_calls"]:
                raise RuntimeError("tui-acceptance-call-limit")
            report["calls"] += 1
            write(root / "progress.json", report)
            try:
                return await inner.complete(request)
            except BaseException:
                report["provider_stopped"] = True
                raise

    args.workspace, args.session_id = root / "source", None
    args.data_dir, args.product_config = root / "data", root / "product.json"
    args.sandbox_config = root / "sandbox.json"
    args.context_config = args.background_config = None
    args.plugins, args.max_steps = [], 4
    args.compaction = args.token_budget = None
    args.verify_command = args.verifier_name = None
    cli._provider_and_model = lambda _args: (Bounded(), model)
    cli._model_retry_policy = lambda _args: NO_MODEL_RETRY
    original = TracehTuiApp.run_async

    async def pilot_flow(pilot):
        app = pilot.app

        def checkpoint(name):
            app.save_screenshot(name + ".svg", path=root)
            report["checkpoints"].append(name)
            write(root / "progress.json", report)

        async def settle():
            await pilot.pause()
            operation = app._operation_task
            if operation:
                await asyncio.wait_for(asyncio.shield(operation), 600)
            await pilot.pause()

        async def send(text):
            app.query_one("#chat-input", Input).value = text
            app.query_one("#chat-input", Input).focus()
            await pilot.press("enter")
            await settle()

        async def gate(token):
            await pilot.click("#gate-primary")
            await pilot.pause()
            field = app.query_one("#confirmation-input", Input)
            field.value = token
            field.focus()
            await pilot.press("enter")
            await settle()

        try:
            async with asyncio.timeout(contract["timeout_seconds"]):
                checkpoint("02-chat")
                case = json.loads((root / "case.json").read_text(encoding="utf-8"))
                await send(
                    "请调用 propose_product_task 提议以下 multi 任务，等待我确认：\n"
                    + case["requirement"]
                )
                checkpoint("03-proposal")
                await send("确认以上任务，请调用 confirm_product_task 让我在界面输入 START。")
                assert str(app.query_one("#gate-primary", Button).label) == "START"
                checkpoint("04-start-gate")
                await gate("START")
                checkpoint("05-result")
                panel = str(app.query_one("#product-state", Static).content)
                report["result_panel"] = panel
                assert str(app.query_one("#gate-primary", Button).label) == "批准", panel
                for key, name in (
                    ("ctrl+t", "06-conversation"),
                    ("ctrl+d", "07-patch"),
                    ("ctrl+p", "08-identities"),
                ):
                    await pilot.press(key)
                    await pilot.pause()
                    if key == "ctrl+t":
                        async with asyncio.timeout(30):
                            log = app.screen.query_one("#task-conversation-log", RichLog)
                            while not log.lines:
                                await pilot.pause()
                        snapshot = app.screen._snapshot
                        assert snapshot is not None
                        assert {role.role for role in snapshot.roles} == {"coder", "patch_author"}
                    checkpoint(name)
                    await pilot.press("escape")
                # The isolated target must remain untouched before explicit approval.
                assert (
                    git("rev-parse", "main", cwd=root / "target.git") == contract["source_revision"]
                )
                await gate("APPROVE")
                checkpoint("09-approved")
                report["final_panel"] = str(app.query_one("#product-state", Static).content)
                assert (
                    git("rev-parse", "main", cwd=root / "target.git") != contract["source_revision"]
                )
                assert git("status", "--porcelain", cwd=root / "source") == ""
                report["completed"] = True
        except BaseException as error:
            report["failure"] = type(error).__name__
            checkpoint("failure")
        finally:
            await pilot.press("ctrl+q")

    async def real_terminal(app, **kwargs):
        return await original(app, size=(125, 45), auto_pilot=pilot_flow, **kwargs)

    TracehTuiApp.run_async = real_terminal
    try:
        report["exit_code"] = await cli._chat(args)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write(root / "result.json", report)
    print(json.dumps(report, ensure_ascii=True))
    if not report["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--material", type=Path)
    parser.add_argument("--sandbox", type=Path)
    options = parser.parse_args()
    asyncio.run(prepare(options) if options.action == "prepare" else run(options))
