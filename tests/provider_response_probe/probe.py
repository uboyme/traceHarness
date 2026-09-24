"""C0-2: one frozen real call that records a response's *shape*, never its text.

Why this exists. The v10 pilot ended with ``finish_reason=length``, zero tool
calls and empty content, and the host recorded it as completed. One explanation
is that the model spent its whole output budget on a reasoning channel this
adapter does not read: ``openai_compatible`` takes ``message.content`` and
nothing else. That is a hypothesis, and the audit cannot settle it, because the
original run did not keep the HTTP body.

What this probe can and cannot show. It observes the shape of *this* call, on
the same provider, model and non-streaming mode. If a separate reasoning field
exists here, the hypothesis is live and the adapter has a fidelity gap worth
fixing. If it does not exist, the hypothesis is refuted for this configuration.
Either way it does **not** retro-diagnose the v10 response: a new call is a new
response, and key names alone would not prove what the old one contained.

What it records: field names, types, lengths, counts, the raw finish reason,
which usage sub-fields are present, and both output ceilings. What it never
records: reasoning text, answer text, authentication, headers, or a full body.
It executes no tool the model returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from traceh.api.llm import ModelMessage, ModelRequest
from traceh.cli.env_file import load_env_file
from traceh.llm import openai_compatible
from traceh.llm.openai_compatible import OpenAICompatibleProvider

#: The v10 model, not the `.env` default: the point is this exact model's shape.
MODEL = "deepseek-v4.1-flash"

#: Deliberately small, to make a truncated ending likely rather than rare. This
#: is a disclosed deviation from the pilot's ceiling (8192) and is the reason
#: the probe is cheap; a truncated shape here is evidence about the channel,
#: not a measurement of how often the pilot's configuration truncates.
MAX_OUTPUT_TOKENS = 64

PROMPT = (
    "A freight train leaves at 06:00 averaging 45 km/h. A second train leaves "
    "the same station at 07:30 averaging 72 km/h on a parallel track. Work "
    "through the algebra step by step, then state the catch-up time."
)


def _shape(value: object, *, depth: int = 0) -> object:
    """Describe a JSON value without reproducing any of its text."""

    if isinstance(value, dict):
        if depth >= 3:
            return {"type": "object", "keys": sorted(map(str, value))}
        return {
            "type": "object",
            "fields": {str(k): _shape(v, depth=depth + 1) for k, v in sorted(value.items())},
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "items": [_shape(v, depth=depth + 1) for v in value[:2]],
        }
    if isinstance(value, str):
        # Length and emptiness are the facts we need; the characters are not.
        return {"type": "string", "chars": len(value), "blank": not value.strip()}
    if value is None:
        # Explicit null and an absent key are different facts about the wire.
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, (int, float)):
        return {"type": "number", "value": value}
    return {"type": type(value).__name__}


def main() -> int:
    load_env_file(Path(".env"))
    key_name = os.environ.get("TRACEH_API_KEY_ENV", "DASHSCOPE_API_KEY")
    base_url = os.environ["TRACEH_BASE_URL"]
    if not os.environ.get(key_name):
        print(f"no credential in {key_name}; probe not run", file=sys.stderr)
        return 2

    # One HTTP call, made by the production adapter. The probe only tees the
    # bytes on their way in, so "what the wire said" and "what the host kept"
    # describe the same response rather than two different ones.
    seen: list[bytes] = []
    original = openai_compatible.urllib.request.urlopen

    def tee(*args, **kwargs):
        handle = original(*args, **kwargs)

        class Recording:
            def __enter__(self):
                self._inner = handle.__enter__()
                return self

            def __exit__(self, *exc):
                return handle.__exit__(*exc)

            def read(self, *a):
                payload = self._inner.read(*a)
                seen.append(payload)
                return payload

            def __getattr__(self, name):
                return getattr(handle, name)

        return Recording()

    provider = OpenAICompatibleProvider(
        base_url, api_key_env=key_name, timeout_seconds=120.0
    )
    request = ModelRequest(
        provider=provider.name,
        model=MODEL,
        messages=(ModelMessage("user", PROMPT),),
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    openai_compatible.urllib.request.urlopen = tee
    try:
        parsed = asyncio.run(provider.complete(request))
    finally:
        openai_compatible.urllib.request.urlopen = original

    raw = json.loads(b"".join(seen).decode("utf-8"))
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = raw.get("usage") or {}

    record = {
        "kind": "provider-response-shape-probe",
        "purpose": "C0-2: does this model return a reasoning channel the adapter drops",
        "recorded_at": datetime.now(UTC).isoformat(),
        "binding": {
            "base_url": base_url,
            "model": MODEL,
            "stream": False,
            "requested_max_tokens": MAX_OUTPUT_TOKENS,
            "pilot_max_tokens": 8192,
            "deviation_disclosed": (
                "The pilot allowed 8192 output tokens; this probe allows 64 to make a "
                "truncated ending cheap to observe. Frequency of truncation under the "
                "pilot's own ceiling is therefore NOT measured here."
            ),
        },
        "wire_shape": {
            "top_level_keys": sorted(map(str, raw)),
            "choice_keys": sorted(map(str, choice)),
            "message_keys": sorted(map(str, message)),
            "message_fields": {str(k): _shape(v) for k, v in sorted(message.items())},
            "finish_reason_present": "finish_reason" in choice,
            "finish_reason_raw": choice.get("finish_reason"),
            "tool_call_count": len(message.get("tool_calls") or []),
            "usage_keys": sorted(map(str, usage)),
            "usage_shape": {str(k): _shape(v) for k, v in sorted(usage.items())},
        },
        "adapter_read": {
            "completion_category": parsed.completion.value,
            "provider_finish_reason": parsed.provider_finish_reason,
            "content_chars": len(parsed.content),
            "content_blank": not parsed.content.strip(),
            "tool_calls": len(parsed.tool_calls),
            "usage_quality": parsed.usage.quality.value,
            "input_tokens": parsed.usage.input_tokens,
            "output_tokens": parsed.usage.output_tokens,
        },
        "interpretation_limits": [
            "One call describes this call. It does not establish what the v10 "
            "response contained, and key names alone never would.",
            "A reasoning channel being present does not mean the pilot lost its "
            "answer there; a reasoning channel being absent does not mean the "
            "pilot's empty answer had another cause that this probe identified.",
        ],
    }
    destination = Path("docs/validation-data/real-repository-pilot-v1/c0-response-shape-probe.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(record["wire_shape"], ensure_ascii=False, indent=2, sort_keys=True))
    print(json.dumps(record["adapter_read"], ensure_ascii=False, indent=2, sort_keys=True))
    print(f"written: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
