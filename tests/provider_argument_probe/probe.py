"""Explicit C4 diagnostic: preserve HTTP bodies without changing the original Provider parser."""

import argparse
import asyncio
import hashlib
import json
import os
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelRequest
from traceh.cli.env_file import load_env_file, validate_env_var_name
from traceh.llm.failures import ProviderFailure
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.tools.schema import ToolArgumentError, validate_arguments


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    with path.open("x", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)


@contextmanager
def record_http():
    """Observe bytes already consumed by the original Provider; never read headers or keys."""
    original = urllib.request.urlopen
    capture = {"request_payload_sha256": None, "body": None}

    class Response:
        def __init__(self, response):
            self.response = response
            self.entered = None

        def __enter__(self):
            self.entered = self.response.__enter__()
            return self

        def read(self, *args, **kwargs):
            body = self.entered.read(*args, **kwargs)
            if capture["body"] is not None:
                raise RuntimeError("probe-multiple-body-reads")
            capture["body"] = body
            return body

        def __exit__(self, *args):
            return self.response.__exit__(*args)

    def open_recorded(request, *args, **kwargs):
        if capture["request_payload_sha256"] is not None:
            raise RuntimeError("probe-multiple-http-requests")
        capture["request_payload_sha256"] = hashlib.sha256(request.data).hexdigest()
        return Response(original(request, *args, **kwargs))

    with patch.object(urllib.request, "urlopen", open_recorded):
        yield capture


def strict_json(value):
    def reject(_):
        raise ValueError("non-json-number")

    return json.loads(value, parse_constant=reject)


def analyze_body(body, request, parsed):
    """Separate wire syntax from the existing Tool schema; no repair or source permissions."""
    if body is None:
        return {"body_available": False, "calls": [], "wire_usage": None}
    result = {
        "body_available": True,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_bytes": len(body),
        "calls": [],
        "wire_usage": None,
    }
    try:
        raw = strict_json(body.decode("utf-8"))
        message = raw["choices"][0]["message"]
    except (UnicodeError, ValueError, KeyError, IndexError, TypeError):
        return {**result, "envelope": "invalid-or-unsupported"}
    result["envelope"] = "decoded"
    usage = raw.get("usage", {})
    if isinstance(usage, dict) and all(
        type(usage.get(key)) is int and usage[key] >= 0
        for key in ("prompt_tokens", "completion_tokens")
    ):
        result["wire_usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens")}
    schemas = {schema.name: schema.input_schema for schema in request.tools}
    calls = message.get("tool_calls", []) if isinstance(message, dict) else None
    if not isinstance(calls, list):
        return {**result, "tool_calls": "invalid-envelope"}
    for index, call in enumerate(calls):
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            result["calls"].append({"index": index, "syntax": "invalid-call-envelope"})
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            result["calls"].append({"index": index, "syntax": "invalid-call-name"})
            continue
        raw_arguments = function.get("arguments", "{}")
        row = {"index": index, "tool_name": name, "wire_type": type(raw_arguments).__name__}
        try:
            decoded = (
                strict_json(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
            row["syntax"] = "object" if isinstance(decoded, dict) else "non-object"
        except (ValueError, TypeError):
            decoded = None
            row["syntax"] = "invalid-json"
        if row["syntax"] == "object":
            schema = schemas.get(name)
            if schema is None:
                row["tool_schema"] = "tool-not-in-request"
            else:
                try:
                    validate_arguments(decoded, schema)
                except ToolArgumentError:
                    row["tool_schema"] = "invalid"
                else:
                    row["tool_schema"] = "valid"
            row["arguments_sha256"] = fingerprint(decoded)
        if parsed is not None and index < len(parsed.tool_calls):
            actual = parsed.tool_calls[index]
            row["adapter_name_matches"] = actual.name == name
            row["adapter_arguments_match"] = actual.arguments == decoded
            if row["syntax"] == "invalid-json":
                row["adapter_dialect_accepted"] = True
        result["calls"].append(row)
    return result


def prepare(args):
    if args.repeats < 1 or args.timeout_seconds <= 0:
        raise ValueError("probe-explicit-positive-bounds-required")
    events = json.loads(args.events.read_text(encoding="utf-8"))
    failures = [
        event
        for event in events
        if event["type"] == "model/attempt-end"
        and event["data"].get("failure_code") == "provider-tool-arguments-invalid"
    ]
    if len(failures) != 1:
        raise ValueError("probe-original-failure-not-unique")
    failure = failures[0]
    snapshots = [
        event for event in events if event["seq"] == failure["data"]["request_snapshot_seq"]
    ]
    if len(snapshots) != 1 or snapshots[0]["type"] != "request/snapshot":
        raise ValueError("probe-original-snapshot-missing")
    snapshot = snapshots[0]["data"]
    if (
        failure["stream_id"] != snapshots[0]["stream_id"]
        or any(
            failure["data"][name] != snapshot[name]
            for name in ("dispatch_fingerprint", "step_id", "turn_id")
        )
    ):
        raise ValueError("probe-original-attempt-binding-mismatch")
    request = ModelRequest.from_dict(snapshot["dispatch_request"])
    if (
        request.to_dict() != snapshot["dispatch_request"]
        or fingerprint(request.to_dict()) != snapshot["dispatch_fingerprint"]
    ):
        raise ValueError("probe-frozen-request-mismatch")
    repository = Path(__file__).resolve().parents[2]
    files = [
        *sorted((repository / "src").rglob("*.py")),
        *sorted(Path(__file__).parent.glob("*.py")),
    ]
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": 1,
        "purpose": "C4 exact historical dispatch diagnostic, not Session replay or Tool execution",
        "original_events_sha256": sha(args.events),
        "original_failure_seq": failure["seq"],
        "original_request_fingerprint": snapshot["dispatch_fingerprint"],
        "model": request.model,
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "repeats": args.repeats,
        "timeout_seconds": args.timeout_seconds,
        "retry": "none; every attempt counted",
        "source_sha256": {p.relative_to(repository).as_posix(): sha(p) for p in files},
        "created_unix": time.time(),
        "rules": [
            "Keep the original frozen dispatch exactly; do not execute returned Tools.",
            "Capture only the original Provider's consumed response body and request payload hash; "
            "never read or export auth headers or environment values.",
            "Compare independent strict JSON decoding, original Provider output "
            "and existing Tool schema separately.",
            "No parser repairs, thresholds, model fallbacks, hidden retries, replacement of failed "
            "cases or retrospective claims about unavailable historical HTTP bytes.",
        ],
    }
    write(args.output / "manifest.json", manifest)
    write(args.output / "request.json", request.to_dict())
    # The original loader is the only reader of credentials. No report exports its values.
    load_env_file(args.env_file)
    key_env = validate_env_var_name(
        os.environ.get("TRACEH_API_KEY_ENV", "OPENAI_API_KEY"), setting="TRACEH_API_KEY_ENV"
    )
    if any(not os.environ.get(name) for name in ("TRACEH_BASE_URL", key_env)):
        raise ValueError("explicit-real-provider-configuration-required")
    provider = OpenAICompatibleProvider(
        os.environ["TRACEH_BASE_URL"], api_key_env=key_env, timeout_seconds=args.timeout_seconds
    )
    return request, provider, manifest


async def run(args):
    request, provider, manifest = prepare(args)
    rows = []
    for ordinal in range(1, args.repeats + 1):
        folder = args.output / f"{ordinal:03}"
        folder.mkdir()
        started = time.perf_counter()
        parsed = None
        row = {"ordinal": ordinal}
        with record_http() as capture:
            try:
                parsed = await provider.complete(request)
            except ProviderFailure as error:
                row.update(
                    status="provider-failure", code=error.code, category=error.category.value
                )
            except Exception as error:
                row.update(status="diagnostic-error", error_type=type(error).__name__)
            else:
                row.update(status="parsed", tool_calls=len(parsed.tool_calls))
        row["elapsed_ms"] = (time.perf_counter() - started) * 1000
        row["request_payload_sha256"] = capture["request_payload_sha256"]
        if capture["body"] is not None:
            (folder / "body.bin").write_bytes(capture["body"])
        row["wire"] = analyze_body(capture["body"], request, parsed)
        write(folder / "result.json", row)
        rows.append(row)
        print(json.dumps({key: row[key] for key in ("ordinal", "status")}), flush=True)
    write(args.output / "report.json", {"manifest": manifest, "results": rows})
    return 1 if any(row["status"] == "diagnostic-error" for row in rows) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(run(arguments)))
