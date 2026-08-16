"""Append-only manual Surface replacement.

Automatic summarization belongs in a future context plugin. v0.3 provides the durable
operation and CLI so later compactors do not need a new Session protocol.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.session.service import SessionService


@dataclass(frozen=True, slots=True)
class CompactionReport:
    session_id: str
    replacement_seq: int
    source_seqs: tuple[int, ...]
    summary: str


class CompactionService:
    _surface_types = {"user/message", "assistant/message", "tool/result", "surface/replace"}

    def __init__(self, sessions: SessionService) -> None:
        self.sessions = sessions

    async def replace_through(
        self,
        session_id: str,
        *,
        through_seq: int,
        summary: str,
    ) -> CompactionReport:
        await self.sessions.ensure_session(session_id)
        if not summary.strip():
            raise ValueError("compaction summary cannot be empty")
        events = await self.sessions.read_session(session_id)
        if not events or through_seq < 1 or through_seq > events[-1].seq:
            raise ValueError(f"through_seq must be between 1 and {events[-1].seq if events else 0}")
        source_seqs = tuple(
            event.seq
            for event in events
            if event.seq <= through_seq and event.type in self._surface_types
        )
        if not source_seqs:
            raise ValueError("the selected boundary contains no model-visible messages")
        event = await self.sessions.append_session(
            session_id,
            "surface/replace",
            {
                "source_seqs": list(source_seqs),
                "replacement": {
                    "role": "user",
                    "content": f"<compacted-summary>\n{summary.strip()}\n</compacted-summary>",
                },
                "method": "manual",
                "through_seq": through_seq,
            },
        )
        return CompactionReport(session_id, event.seq, source_seqs, summary.strip())
