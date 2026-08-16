"""Projection from durable events to model-visible messages."""

from __future__ import annotations

from traceh.api.events import EventEnvelope
from traceh.api.llm import ModelMessage, ToolCall


class SurfaceProjector:
    def project(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        through_seq: int | None = None,
    ) -> tuple[ModelMessage, ...]:
        selected = tuple(event for event in events if through_seq is None or event.seq <= through_seq)
        hidden: set[int] = set()
        replacements: list[tuple[int, ModelMessage]] = []

        for event in selected:
            if event.type != "surface/replace":
                continue
            source_seqs = event.data.get("source_seqs", [])
            if isinstance(source_seqs, list):
                hidden.update(int(seq) for seq in source_seqs)
            replacement = event.data.get("replacement")
            if isinstance(replacement, dict):
                replacements.append((event.seq, ModelMessage.from_dict(replacement)))

        output: list[tuple[int, ModelMessage]] = []
        for event in selected:
            if event.seq in hidden:
                continue
            if event.type == "user/message":
                output.append(
                    (event.seq, ModelMessage(role="user", content=str(event.data.get("content", ""))))
                )
            elif event.type == "assistant/message":
                raw_calls = event.data.get("tool_calls", [])
                calls = tuple(
                    ToolCall.from_dict(item)
                    for item in raw_calls
                    if isinstance(raw_calls, list) and isinstance(item, dict)
                )
                output.append(
                    (
                        event.seq,
                        ModelMessage(
                            role="assistant",
                            content=str(event.data.get("content", "")),
                            tool_calls=calls,
                        ),
                    )
                )
            elif event.type == "tool/result":
                output.append(
                    (
                        event.seq,
                        ModelMessage(
                            role="tool",
                            content=str(event.data.get("content", "")),
                            tool_call_id=str(event.data.get("tool_call_id", "")),
                            name=str(event.data.get("tool_name", "")) or None,
                        ),
                    )
                )

        output.extend(pair for pair in replacements if pair[0] not in hidden)
        output.sort(key=lambda pair: pair[0])
        return tuple(message for _, message in output)
