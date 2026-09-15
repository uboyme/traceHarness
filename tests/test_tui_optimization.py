"""User-visible settings and feedback use the same public host and Chat path."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("textual")
from test_tui import _Provider, _runtime
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


async def test_tui_chat_then_feedback_panel_starts_background_and_retains_evidence(tmp_path):
    provider = _Provider()
    runtime, _ = _runtime(tmp_path, provider)
    opened = await open_chat_session(runtime, workspace=tmp_path, session_id=None)
    observed, done = [], asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        observed.extend(observations)
        done.set()
        return EpisodeSettlement("explicit-ui-test-evidence", None, False, True, True)

    host = BackgroundOptimizationHost(
        runtime.sessions,
        BackgroundPeriod(
            "ui-test-period",
            str(tmp_path.resolve()),
            fingerprint("source"),
            fingerprint("plan"),
            datetime.now(UTC) + timedelta(hours=1),
            1,
            2,
            20000,
            10,
            60,
        ),
        reservation=EpisodeReservation(2, 20000),
        execute=execute,
    )
    await host.open()
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=False,
        heartbeat_seconds=0,
        clock=default_clock(),
        background=host,
        product=None,
    )
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#chat-input", Input).value = "A short question"
            await pilot.press("enter")
            await provider.started.wait()
            for _ in range(20):
                await pilot.pause()
                events = await runtime.sessions.read_session(opened.session.session_id)
                if any(e.type == "turn/end" for e in events):
                    break
            await pilot.press("f6")
            await pilot.pause()
            assert isinstance(app.screen, OptimizationScreen)
            for name in ("optimization-enable", "optimization-submit"):
                app.screen.query_one("#" + name, Button).active_effect_duration = 0
            await pilot.click("#optimization-enable")
            await pilot.pause()
            app.screen.query_one("#optimization-feedback", Input).value = "The evidence is unclear."
            await pilot.click("#optimization-submit")
            await asyncio.wait_for(done.wait(), 10)
            await pilot.pause()
            await app.screen.refresh_state()
            assert "explicit-ui-test-evidence" in str(
                app.screen.query_one("#optimization-state", Static).render()
            )
            assert observed[0].session_id == opened.session.session_id
            assert len(provider.requests) == 1  # Feedback wasn't sent as another chat question.
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
        write_dataset(benchmark, manifest, cases, format_version=2)
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
