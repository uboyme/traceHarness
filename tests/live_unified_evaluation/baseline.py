"""Opt-in UE-4 driver: explicit inputs, original Runner, no alternate scorer."""

import argparse
import asyncio
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from traceh.cli.credentials import load_key
from traceh.cli.main import _configure_from_environment, _provider_and_model, build_parser
from traceh.cli.tui_entry import initial_settings
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.llm.retry import ModelRetryPolicy
from traceh.sandbox.config import load_sandbox_file


def connection(profile):
    args = build_parser().parse_args(
        [
            "chat",
            "--tui",
            "--tui-profile",
            str(profile),
            "--auto-compact",
            "off",
            "--auto-compact-method",
            "extractive",
        ]
    )
    args.tui_explicit = {"auto_compact", "auto_compact_method"}
    args, error = initial_settings(args)
    if error:
        raise ValueError("authorized-profile-unavailable")
    environment = dict(os.environ)
    _configure_from_environment(args, environment=environment)
    args.tui_api_key = environment.get(args.api_key_env) or load_key(args)
    endpoint = urlsplit(args.base_url)
    if endpoint.username or endpoint.password or endpoint.query:
        raise ValueError("endpoint-not-safe-to-freeze")
    provider, model = _provider_and_model(args)
    provider.timeout_seconds = 60
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    return args, provider, model


async def execute(options):
    driver_bytes = await asyncio.to_thread(Path(__file__).read_bytes)
    args, provider, model = connection(options.profile)
    suite, output = options.benchmark.resolve(), options.output.resolve()
    if output.is_relative_to(suite):
        raise ValueError("output-overlaps-benchmark")
    plan = json.loads((suite / "run-plan.example.json").read_text(encoding="utf-8"))
    plan["variants"] = [{"variant_id": "current-ue4", "role": "current", "source": "current"}]
    plan["trials"] = {"repetitions": 1}
    plan["comparison"] = None
    plan["model"].update(
        provider=provider.name,
        model=model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        script=None,
        timeout_seconds=provider.timeout_seconds,
    )
    plan["execution"].update(
        sandbox_config=str(options.sandbox.resolve()), max_trials=72, timeout_seconds=7200
    )
    sandbox = load_sandbox_file(options.sandbox.resolve())
    output.mkdir(parents=True, exist_ok=False)
    (output / "driver.py").write_bytes(driver_bytes)
    path = output / "plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    runner = EvaluationRunner(
        suite,
        output / "run",
        provider=provider,
        model_id=model,
        retry_policy=ModelRetryPolicy(**plan["model"]["retry_policy"]),
        sandbox=sandbox.policy,
        options=load_run_options(path),
        provider_binding={
            "network_mode": "direct-urllib-proxy-handler-empty",
            "connection_digest": digest_bytes(args.base_url.encode()),
            "driver_digest": digest_bytes(driver_bytes),
            "timeout_seconds": 60,
        },
    )
    if len(runner.trials) != 72:
        raise ValueError("ue4-frozen-case-count-mismatch")
    print(
        json.dumps(
            {
                "provider": provider.name,
                "model": model,
                "trials": len(runner.trials),
                "output": str(output),
                "network": "direct",
            }
        ),
        flush=True,
    )
    original = runner.evaluator.execute

    async def observe(context):
        identity = {"case": context.spec.case_id, "seed": context.spec.material_seed}
        print(json.dumps({**identity, "event": "started"}), flush=True)
        result = await original(context)
        print(
            json.dumps({**identity, "event": "finished", "execution": result.execution.value}),
            flush=True,
        )
        return result

    runner.evaluator.execute = observe
    report = await runner.run()
    print(
        json.dumps(
            {
                "complete": report.complete,
                "assessment_complete": report.assessment_complete,
                "run_id": report.run_id,
                "output": str(output),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "sandbox", "benchmark", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
