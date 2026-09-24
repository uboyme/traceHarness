"""Replay one failed attempt's frozen request and keep the provider's raw response.

Diagnostic only (collection plan, experiment A stop on 2026-09-23). The adapter
deliberately never logs response text, so a ``provider-tool-arguments-*``
failure cannot be diagnosed from the event log. This re-sends the exact frozen
``dispatch_request`` through the adapter's own ``_complete_sync`` (same payload
construction), captures the raw HTTP body beside it, and records the adapter's
own verdict. No tool is executed, nothing is written to any Session, and the
connection is direct: proxies are cleared and ``ProxyHandler({})`` installed.
The API key is loaded by the project's own ``.env`` loader and never printed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

import traceh.llm.openai_compatible as adapter
from traceh.api.llm import ModelRequest
from traceh.cli.env_file import load_env_file
from traceh.llm.failures import ProviderFailure


def failed_request(database):
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT stream_id, seq, envelope_json FROM events ORDER BY stream_id, seq"
        ).fetchall()
    finally:
        connection.close()
    events = {}
    for stream, seq, raw in rows:
        events.setdefault(stream, {})[seq] = json.loads(raw)
    for stream, by_seq in events.items():
        for event in by_seq.values():
            data = event["data"]
            if event["type"] == "model/attempt-end" and data.get("status") == "failed":
                snapshot = by_seq[data["request_snapshot_seq"]]["data"]
                return stream, data["failure_code"], ModelRequest.from_dict(
                    snapshot["dispatch_request"]
                )
    raise SystemExit("no failed attempt in " + str(database))


def replay(request, repeats, output, base_url, api_key_env):
    real = urllib.request.urlopen
    captured = []

    def capturing(http_request, timeout=None):
        with real(http_request, timeout=timeout) as response:
            body = response.read()
        captured.append(body)
        return io.BytesIO(body)

    adapter.urllib.request.urlopen = capturing
    provider = adapter.OpenAICompatibleProvider(
        base_url=base_url, api_key_env=api_key_env, timeout_seconds=300.0
    )
    results = []
    try:
        for index in range(1, repeats + 1):
            captured.clear()
            try:
                response = provider._complete_sync(request)
                verdict = {
                    "adapter": "ok",
                    "tools": [call.name for call in response.tool_calls],
                    "completion": response.completion.value,
                }
            except ProviderFailure as failure:
                verdict = {"adapter": failure.code, "category": failure.category.value}
            raw = captured[0] if captured else None
            if raw is not None:
                (output / f"response-{index}.json").write_bytes(raw)
            parsed = json.loads(raw) if raw else None
            calls = []
            if parsed:
                message = parsed["choices"][0]["message"]
                for call in message.get("tool_calls") or []:
                    arguments = call["function"].get("arguments")
                    calls.append(
                        {
                            "name": call["function"].get("name"),
                            "arguments_type": type(arguments).__name__,
                            "arguments_prefix": repr(arguments)[:240],
                            "arguments_length": len(arguments)
                            if isinstance(arguments, str)
                            else None,
                        }
                    )
                verdict["finish_reason"] = parsed["choices"][0].get("finish_reason")
                verdict["usage"] = parsed.get("usage")
            verdict["calls"] = calls
            results.append(verdict)
            print(index, json.dumps(verdict, ensure_ascii=False)[:900], flush=True)
    finally:
        adapter.urllib.request.urlopen = real
    return results


def main_work_first(request):
    """Only move ``main_work`` to the front of the plan tool's properties.

    Everything else - messages, other tools, limits, values - stays byte-identical;
    JSON object key order does not change what the schema accepts.
    """
    raw = request.to_dict()
    for tool in raw["tools"]:
        if tool["name"] == "submit_collaboration_plan":
            properties = tool["input_schema"]["properties"]
            tool["input_schema"]["properties"] = {
                "main_work": properties["main_work"],
                **{k: v for k, v in properties.items() if k != "main_work"},
            }
    return ModelRequest.from_dict(raw)


def flatten_main_work(request):
    """Replace the nested ``main_work`` object with three top-level strings.

    Diagnostic variant only: tests whether the nested-object parameter shape is
    what the provider fails to serialise. Limits and wording are unchanged.
    """
    raw = request.to_dict()
    for tool in raw["tools"]:
        if tool["name"] == "submit_collaboration_plan":
            schema = tool["input_schema"]
            nested = schema["properties"].pop("main_work")
            for name, spec in nested["properties"].items():
                schema["properties"]["main_" + name] = spec
            schema["required"] = [
                *("main_" + name for name in nested["required"]),
                *(r for r in schema["required"] if r != "main_work"),
            ]
    return ModelRequest.from_dict(raw)


def main(database, repeats, output, reorder=False, flatten=False):
    for key in tuple(os.environ):
        if key.upper().endswith("_PROXY"):
            os.environ.pop(key)
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    load_env_file(Path(".env"))
    stream, code, request = failed_request(database)
    if reorder:
        request = main_work_first(request)
    if flatten:
        request = flatten_main_work(request)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "request.json").write_text(
        json.dumps(request.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("stream", stream, "original failure", code, "tools", len(request.tools), flush=True)
    results = replay(
        request,
        repeats,
        output,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    )
    (output / "results.json").write_text(
        json.dumps({"stream": stream, "original_failure": code, "results": results}, indent=1),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--main-work-first", action="store_true")
    parser.add_argument("--flatten-main-work", action="store_true")
    options = parser.parse_args()
    main(
        options.database,
        options.repeats,
        options.output,
        options.main_work_first,
        options.flatten_main_work,
    )
