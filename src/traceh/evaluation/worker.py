"""Trusted frozen worker entry. Control stdin cancels the existing evaluation owner."""

import asyncio
import os
import sys
import threading
import urllib.request
from pathlib import Path

from traceh.api.json_types import fingerprint, to_json_value
from traceh.concurrency import await_worker_convergence
from traceh.evaluation.errors import BenchmarkExecutionError
from traceh.evaluation.inputs import read_input
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import environment_identity, source_digest, source_files


async def execute(path):
    from traceh.cli.main import _configure_from_environment, _eval, build_parser

    document = read_input(path.parent, path.name)
    request = document.data
    root, arm = Path(request["root"]), request["arm"]
    frozen = read_input(root, "experiment.json").data
    actual_env = to_json_value(environment_identity())
    code_root, sources = source_files()
    if (
        fingerprint(frozen) != request["experiment_digest"]
        or arm not in frozen["arms"]
        or code_root != path.parent / "code/traceh"
        or source_digest(sources) != arm["source_digest"]
        or actual_env != request["environment"]
        or read_input(path.parent, "plan.json").sha256 != arm["plan_sha256"]
    ):
        raise BenchmarkExecutionError("evaluation-frozen-input-drift")
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    args = build_parser().parse_args(
        [
            "eval",
            str(root / "materials"),
            "--output",
            str(path.parent / "run"),
            "--run-plan",
            str(path.parent / "plan.json"),
            "--env-file",
            str(root / "inputs/empty.env"),
        ]
    )
    _configure_from_environment(args)
    args._evaluation_network_mode = "direct"
    args._evaluation_expected_implementation = frozen["provider_implementation"]
    task = asyncio.create_task(_eval(args))
    loop = asyncio.get_running_loop()

    def control():
        # One private parent pipe, not a protocol exposed to model/tool processes.
        line = os.read(sys.stdin.fileno(), 32)
        if line in (b"CANCEL\n", b""):
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    threading.Thread(target=control, daemon=True, name="evaluation-control").start()
    failure, code = None, 4
    try:
        code = await asyncio.shield(task)
    except BaseException as error:
        failure = error
        task.cancel()
        await await_worker_convergence(task)
    report = None
    if (path.parent / "run/report.json").is_file():
        report = read_input(path.parent / "run", "report.json").data
    converged = report is not None and all(
        t["convergence"] == "converged" for t in report["trials"]
    )
    write_json(
        path.parent / "worker-receipt.json",
        {
            "format": 2,
            "request_digest": fingerprint(request),
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "report_digest": None
            if report is None
            else read_input(path.parent / "run", "report.json").sha256,
            "source_root": str(code_root),
            "source_digest": source_digest(source_files()[1]),
            "environment": to_json_value(environment_identity()),
            "convergence": "converged" if converged else "unknown",
            "error": None if failure is None else getattr(failure, "code", type(failure).__name__),
        },
    )
    return code if failure is None else 130 if isinstance(failure, asyncio.CancelledError) else 4


def main(filename):
    try:
        code = asyncio.run(execute(Path(filename).resolve()))
    except BaseException:
        code = 4
    raise SystemExit(code)
