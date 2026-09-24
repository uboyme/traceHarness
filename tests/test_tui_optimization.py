"""User-visible settings and feedback use the same public host and Chat path."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("textual")
from textual.widgets import Button, Input, Static

from traceh.api.json_types import fingerprint
from traceh.chat.activity import default_clock
from traceh.chat.session import open_chat_session
from traceh.evolution.background import (
    BackgroundOptimizationHost,
    BackgroundPeriod,
    EpisodeReservation,
    EpisodeSettlement,
)
from traceh.tui.app import TracehTuiApp
from traceh.tui.optimization import OptimizationScreen


class _FailingReadProvider:
    """Each Turn first reads a missing file (a failed tool result), then answers."""

    name = "tui-provider"

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        from traceh.api.llm import CompletionCategory, ModelResponse, ToolCall

        self.requests.append(request)
        if len(self.requests) % 2:
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(f"c{len(self.requests)}", "read_file", {"path": "missing.txt"}),
                ),
                completion=CompletionCategory.TOOL_HANDOFF,
            )
        return ModelResponse(content="I could not find that file.")


async def test_two_real_chat_turns_with_failed_tools_produce_a_suggestion_after_them(tmp_path):
    from test_tui import _runtime

    provider = _FailingReadProvider()
    runtime, _ = _runtime(tmp_path, provider)
    opened = await open_chat_session(runtime, workspace=tmp_path, session_id=None)
    observed, done = [], asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        observed.extend(observations)
        done.set()
        return EpisodeSettlement("explicit-ui-test-evidence", None, True, True)

    host = BackgroundOptimizationHost(
        runtime.sessions,
        BackgroundPeriod(
            "ui-test-period",
            str(tmp_path.resolve()),
            fingerprint("source"),
            fingerprint("plan"),
            datetime.now(UTC) + timedelta(hours=1),
            1,
            20000,
            10,
            60,
        ),
        reservation=EpisodeReservation(20000),
        execute=execute,
    )
    await host.open()
    await host.set_enabled(True)
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=False,
        heartbeat_seconds=0,
        clock=default_clock(),
        background=host,
        product=None,
    )

    async def ask(pilot, text, ends):
        app.query_one("#chat-input", Input).value = text
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            events = await runtime.sessions.read_session(opened.session.session_id)
            if sum(e.type == "turn/end" for e in events) >= ends and not app._busy:
                return
        raise AssertionError("turn did not finish")

    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await ask(pilot, "Read the missing file", 1)
            # One Turn is one source: nothing is spent on a single occurrence.
            assert not done.is_set() and (await host.view())["episodes"] == 0
            await ask(pilot, "Try reading it again", 2)
            # Each Turn ran as a foreground operation; the idle pulse admits it after.
            await asyncio.wait_for(done.wait(), 10)
            assert {o.failure_class for o in observed} == {"tool-failed"}
            assert len({o.turn_id for o in observed}) == 2
            assert all(o.session_id == opened.session.session_id for o in observed)
            assert len(provider.requests) == 4  # Detection never asked the chat model.
            await pilot.press("f6")
            await pilot.pause()
            assert isinstance(app.screen, OptimizationScreen)
            await app.screen.refresh_state()
            state = str(app.screen.query_one("#optimization-state", Static).render())
            assert "explicit-ui-test-evidence" in state
    finally:
        await host.aclose()
        await runtime.dispose()


@pytest.mark.parametrize("product", [False, True])
async def test_user_case_selection_generates_valid_original_paired_plan_without_calls(
    tmp_path, product
):
    from pathlib import Path
    from types import SimpleNamespace

    from textual.app import App
    from textual.widgets import Select, SelectionList

    from traceh.chat.background import assemble_background, load_background_settings
    from traceh.evaluation.plan import load_run_options
    from traceh.session.event_store import InMemoryEventStore
    from traceh.session.service import SessionService
    from traceh.tui.optimization_plan import OptimizationPlanScreen

    benchmark = Path(__file__).parents[1] / "benchmarks/retrieval_episodes_v1"
    sandbox_config = ""
    if product:
        import json

        from evaluation_fixtures import write_dataset
        from sandbox_fixtures import real_sandbox_policy
        from test_adaptive_evaluation import _multi_material

        from traceh.api.json_types import to_json_value
        from traceh.evaluation.inputs import digest_bytes

        benchmark = tmp_path / "benchmark"
        _multi_material(benchmark)
        manifest = json.loads((benchmark / "benchmark.json").read_text())
        cases = json.loads((benchmark / "dataset.json").read_text())["cases"]
        rubric = benchmark / "rubric.json"
        rubric.write_text(
            json.dumps(
                {
                    "format": 1,
                    "criteria": {
                        c["case_id"]: ["Create the requested file without unsupported claims."]
                        for c in cases
                    },
                }
            )
        )
        manifest["assessment"] = {
            "scorer_id": "product-durable-semantic-v1",
            "version": 1,
            "rubric": {"file": rubric.name, "sha256": digest_bytes(rubric.read_bytes())},
            "requires_review": True,
        }
        write_dataset(benchmark, manifest, cases, format_version=3)
        sandbox = tmp_path / "sandbox.json"
        sandbox.write_text(
            json.dumps(
                {"format": 2, "policy": to_json_value(real_sandbox_policy()), "plugin_grants": []}
            )
        )
        sandbox_config = str(sandbox)
    model = {
        "provider": "openai-compatible",
        "model": "explicit-offline-fixture",
        "base_url": "https://provider.invalid/v1",
        "api_key_env": "EXPLICIT_TEST_KEY",
    }
    screen = OptimizationPlanScreen(
        config_path=tmp_path / "background.json",
        workspace=str(tmp_path.resolve()),
        data_dir=str(tmp_path / "data"),
        model_settings=model,
        sandbox_config=sandbox_config,
    )
    app = App()
    async with app.run_test(size=(120, 55)) as pilot:
        await app.push_screen(screen)
        screen.query_one("#optimization-benchmark", Input).value = str(benchmark)
        screen.query_one("#optimization-load-cases", Button).active_effect_duration = 0
        await pilot.click("#optimization-load-cases")
        await pilot.pause()
        choices = screen.query_one("#optimization-cases", SelectionList)
        assert choices.option_count > 0
        case = choices.get_option_at_index(0).value
        choices.select(case)
        # Select a material version actually supplied by the manifest, not a hidden default.
        from traceh.evaluation.evaluators.episode_manifest import load_episode_suite

        seed = (
            "source"
            if product
            else next(
                c.data["material_seed"]
                for c in load_episode_suite(screen.manifest).cases
                if c.data["case_id"] == case
            )
        )
        screen.query_one("#optimization-seed", Select).value = seed
        button = screen.query_one("#optimization-save-plan", Button)
        button.scroll_visible(animate=False)
        button.active_effect_duration = 0
        await pilot.pause()
        await pilot.click("#optimization-save-plan")
        await pilot.pause()
    settings = load_background_settings(tmp_path / "background.json")
    options = load_run_options(settings.run_plan)
    assert options.case_ids == (case,)
    assert options.material_seeds == (None if product else (seed,))
    assert [v.role for v in options.variants] == ["baseline", "candidate"]
    service = SessionService(InMemoryEventStore())
    host = assemble_background(
        SimpleNamespace(sessions=service),
        settings,
        provider=SimpleNamespace(name=model["provider"]),
        model=model["model"],
        base_url=model["base_url"],
        api_key="FAKE-NOT-A-REAL-KEY",
    )
    state = await host.open()
    assert not state["enabled"] and state["episodes"] == 0
    assert "FAKE-NOT-A-REAL-KEY" not in settings.run_plan.read_text(encoding="utf-8")
    await host.aclose()
