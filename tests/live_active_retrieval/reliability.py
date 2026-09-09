"""Explicit RE experiment entry point. No network during import, freeze or pytest."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from live_active_retrieval.grid import installed_source_root, run_case, source_files, write
from live_active_retrieval.history_smoke import load_provider
from live_active_retrieval.reliability_fixtures import frozen_materials
from traceh.api.llm import ModelMessage, ModelRequest, ToolSchema


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connection_identity(provider, model):
    # Endpoint fingerprints never include credentials, query strings or raw URLs.
    u = urllib.parse.urlsplit(provider.base_url)
    if u.username or u.password or u.query or u.fragment:
        raise ValueError("experiment-endpoint-must-not-contain-credentials-or-query")
    return {
        "provider": provider.name,
        "model": model,
        "endpoint_sha256": hashlib.sha256(provider.base_url.encode()).hexdigest(),
    }


def support_digests():
    tests = Path(__file__).resolve().parents[1]
    paths = sorted((tests / "live_active_retrieval").glob("*.py"))
    paths += [
        tests / name
        for name in [
            "memory_fixtures.py",
            "retrieval_fixtures.py",
            "skill_fixtures.py",
            "plugin_fixtures.py",
        ]
    ]
    return {str(p.relative_to(tests)).replace("\\", "/"): sha(p) for p in paths}


def freeze(args):
    root = args.root.resolve()
    previous = read(Path("docs/validation-data/active-retrieval/ar-d-grid-06/frozen.json"))
    provider, model = load_provider(args.profile)
    identity = connection_identity(provider, model)
    if (provider.name, model) != (previous["provider"], previous["model"]):
        raise ValueError("experiment-model-changed")
    materials = frozen_materials(previous["manifest"])
    write(root / "materials.json", materials, exclusive=True)
    write(
        root / "experiment.json",
        {
            "purpose": "RE controlled experiments; separate from the historical 72-question score",
            "manifest": previous["manifest"],
            "context_policies": previous["context_policies"],
            **identity,
            "materials_sha256": sha(root / "materials.json"),
            "support_files": support_digests(),
            "baseline_source": source_files(args.source.resolve()),
            "schedule": "Single request at a time; alternating paired arm order; retain failures.",
            "max_candidates_per_group": 2,
            "max_target_journeys": 352,
            "probe_configuration": {"calls": 2, "retry": 0, "timeout": 45},
            "evaluation": (
                "Manual answer/evidence/scope review; no model grader. Positive abstention fails."
            ),
            "control_exception": (
                "Current-message controls intentionally contain user-provided data; "
                "hidden source answers never enter questions."
            ),
        },
        exclusive=True,
    )
    print(json.dumps({"frozen": True, "development": 24, "controls": 4, "holdout": 24}))


class DirectTransport:
    """Only for this dedicated sequential experiment child process."""

    def __init__(self):
        self.connections = []
        self.active = False

    def _audit(self, event, args):
        if self.active and event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple) and len(address) >= 2:
                try:
                    loopback = ipaddress.ip_address(address[0]).is_loopback
                except ValueError:
                    loopback = None
                self.connections.append({"loopback": loopback, "port": address[1]})

    def __enter__(self):
        self.previous = urllib.request._opener
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        sys.addaudithook(self._audit)
        urllib.request.install_opener(self.opener)
        self.active = True
        return self

    def __exit__(self, *args):
        self.active = False
        urllib.request._opener = self.previous


def verified_connection(args, frozen):
    provider, model = load_provider(args.profile)
    if connection_identity(provider, model) != {
        k: frozen[k] for k in connection_identity(provider, model)
    }:
        raise ValueError("experiment-connection-changed")
    provider.timeout_seconds = frozen["manifest"]["limits"]["provider_timeout_seconds"]
    return provider, model


async def probe(args):
    root = args.root.resolve()
    frozen = read(root / "experiment.json")
    provider, model = verified_connection(args, frozen)
    provider.timeout_seconds = frozen["probe_configuration"]["timeout"]
    out = root / "transport-probes.json"
    if out.exists():
        raise ValueError("experiment-probes-already-recorded")
    reports = []
    for use_tool in [False, True]:
        marker = "RE_CONNECTION_OK"
        request = ModelRequest(
            provider=provider.name,
            model=model,
            temperature=0,
            max_output_tokens=96,
            messages=(
                ModelMessage(
                    "user",
                    "Call connectivity_echo with text RE_CONNECTION_OK."
                    if use_tool
                    else "Reply exactly RE_CONNECTION_OK.",
                ),
            ),
            tools=(
                ToolSchema(
                    "connectivity_echo",
                    "Synthetic connection probe; not executed.",
                    {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                ),
            )
            if use_tool
            else (),
        )
        record = {"request": request.to_dict(), "response": None, "error": None}
        start = time.monotonic()
        with DirectTransport() as transport:
            try:
                response = await provider.complete(request)
                record["response"] = response.to_dict()
                record["expected"] = (
                    (
                        len(response.tool_calls) == 1
                        and response.tool_calls[0].name == "connectivity_echo"
                        and response.tool_calls[0].arguments == {"text": marker}
                    )
                    if use_tool
                    else response.content.strip() == marker
                )
            except Exception as error:
                record["error"] = {
                    "type": type(error).__name__,
                    "code": getattr(error, "code", None),
                }
                record["expected"] = False
        record.update(seconds=time.monotonic() - start, connections=transport.connections)
        reports.append(record)
        write(out, {"calls": reports, "complete": len(reports) == 2, "tool_executions": 0})
        print(
            json.dumps(
                {"probe": len(reports), "expected": record["expected"], "error": record["error"]}
            ),
            flush=True,
        )
        if not record["expected"]:
            break


async def case(args):
    root = args.root.resolve()
    frozen = read(root / "frozen.json")
    if source_files(installed_source_root()) != frozen["source_files"][args.arm]:
        raise ValueError("experiment-source-changed")
    if support_digests() != frozen["support_files"]:
        raise ValueError("experiment-support-changed")
    fixture = next(f for f in frozen["fixtures"] if f["identity"] == args.identity)
    provider, model = verified_connection(args, frozen)
    with DirectTransport() as transport:
        report = await run_case(
            root / args.arm / fixture["identity"], fixture, frozen, provider, model
        )
    write(
        root / args.arm / fixture["identity"] / "transport.json",
        {
            "mode": "direct",
            "connections": transport.connections,
            "opener_restored": urllib.request._opener is transport.previous,
        },
    )
    print(
        json.dumps(
            {
                "arm": args.arm,
                "identity": fixture["identity"],
                "answer": report.get("answer"),
                "error": report.get("error"),
                "audit_error": report.get("audit_error"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def batch(args):
    root = args.root.resolve()
    frozen = read(root / "frozen.json")
    progress = root / "execution.json"
    if progress.exists():
        raise ValueError("experiment-batch-already-started")
    tasks = [
        (i, arm, f)
        for i, f in enumerate(frozen["fixtures"])
        for arm in (["baseline", "candidate"] if i % 2 == 0 else ["candidate", "baseline"])
    ]
    state = {"complete": False, "planned": len(tasks), "runs": [], "unrun": [], "stop_reason": None}
    consecutive = 0
    for _index, arm, fixture in tasks:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(Path(frozen["source_roots"][arm]).resolve()),
                str(Path(__file__).resolve().parents[1]),
            ]
        )
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "live_active_retrieval.reliability",
                "case",
                "--root",
                str(root),
                "--profile",
                str(args.profile),
                "--arm",
                arm,
                "--identity",
                fixture["identity"],
            ],
            env=env,
            check=False,
        )
        report_path = root / arm / fixture["identity"] / "report.json"
        report = read(report_path) if report_path.exists() else {}
        state["runs"].append(
            {"arm": arm, "identity": fixture["identity"], "exit_code": result.returncode}
        )
        code = (report.get("error") or {}).get("code")
        consecutive = (
            consecutive + 1
            if code
            in {
                "provider-tls-eof",
                "provider-timeout",
                "provider-disconnected",
                "provider-transport-unknown",
                "provider-dns-temporary",
            }
            else 0
        )
        boundary = (
            report.get("audit_error")
            or report.get("replay_errors")
            or report.get("invariant_errors")
        )
        if (
            fixture["family"] == "output"
            and report.get("target_start_seq") is not None
            and report.get("output_executions") != 1
        ):
            boundary = "unexpected-output-execution-count"
        if result.returncode or boundary or consecutive >= 2:
            state["stop_reason"] = (
                "runner-or-boundary-failure"
                if result.returncode or boundary
                else "two-consecutive-transport-failures"
            )
            break
        write(progress, state)
    done = {(r["arm"], r["identity"]) for r in state["runs"]}
    state["unrun"] = [
        {"arm": arm, "identity": f["identity"]}
        for _, arm, f in tasks
        if (arm, f["identity"]) not in done
    ]
    state["complete"] = not state["unrun"]
    write(progress, state)
    print(
        json.dumps(
            {"complete": state["complete"], "runs": len(done), "stop_reason": state["stop_reason"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["freeze", "probe", "case", "batch"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--arm", choices=["baseline", "candidate"])
    parser.add_argument("--identity")
    args = parser.parse_args()
    if args.command in {"freeze", "batch"}:
        globals()[args.command](args)
    else:
        asyncio.run(globals()[args.command](args))
