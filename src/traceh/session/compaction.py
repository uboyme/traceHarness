"""Host-owned Surface compaction: manual replacement and automatic triggering.

Compaction never deletes history. It appends one ``surface/replace`` event that
hides an exact, closed prefix of model-visible conversation and contributes one
bounded summary in its logical place, so a long Session stops growing the model
request while every original event and every historical ``request/snapshot``
stay exactly as they were.

Three rules keep that honest.

**Only a closed prefix is compactable.** A cut boundary is always the sequence
of a ``turn/end`` that really closed an open Turn, so the current user message,
an open Turn, a Step and an assistant Tool call together with its result can
never be split. Automatic compaction additionally keeps the most recent Turns
its policy asks for.

**The host decides, the summarizer only writes prose.** Selection, bounding,
ordering and the durable write belong here. A `SessionSummarizer` receives a
`SummaryRequest` holding nothing but the exact messages being replaced and a
byte bound; it has no Store, no Session service, no Tools and no control
authority, so it cannot choose what is compacted or cause any side effect.

**Bytes are called bytes.** The trigger metric is canonical UTF-8 bytes of the
model-visible conversation. This runtime has no trusted general tokenizer, and
reporting bytes as tokens would be a fabricated number.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import ModelMessage
from traceh.cli.text_safety import is_unsafe_character
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.service import SessionService
from traceh.session.surface_replacement import (
    MAX_SURFACE_SUMMARY_UTF8_BYTES,
    SURFACE_REPLACE,
    SummarizerIdentity,
    SurfacePrefix,
    SurfaceReplacement,
    bounded_summary,
    closed_turn_ends,
    parse_surface_replacement,
    split_tool_calls,
    surface_prefix,
    surface_replacement_data,
    surface_tool_links,
)

#: A conflicting head is re-read rather than retried with a stale payload, so
#: the bound only limits how long one caller keeps losing that race.
MAX_COMPACTION_APPEND_ATTEMPTS = 3

#: Per-message excerpt width of the default deterministic summarizer. It is part
#: of that summarizer's configuration digest, so a changed width is visible in
#: every replacement it wrote.
DEFAULT_SUMMARY_EXCERPT_CHARS = 160


class CompactionError(RuntimeError):
    """One stable, short reason a compaction did not produce a replacement."""

    def __init__(self, code: str, *, committed: bool | None = False) -> None:
        super().__init__(code)
        self.code = code
        #: ``True`` committed, ``False`` provably not, ``None`` unknown. Unknown
        #: is never collapsed into "nothing happened".
        self.committed = committed


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Explicit host configuration for automatic compaction.

    Every field is required. There is no partially configured policy and no
    inferred threshold: a caller that cannot state all four values gets no
    automatic compaction rather than a guessed one.
    """

    enabled: bool
    trigger_utf8_bytes: int
    max_summary_utf8_bytes: int
    keep_recent_turns: int

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("compaction enabled must be a boolean")
        if type(self.trigger_utf8_bytes) is not int or self.trigger_utf8_bytes < 1:
            raise ValueError("compaction trigger must be a positive byte count")
        if (
            type(self.max_summary_utf8_bytes) is not int
            or not 1 <= self.max_summary_utf8_bytes <= MAX_SURFACE_SUMMARY_UTF8_BYTES
        ):
            raise ValueError(
                "compaction summary bound must be between 1 and "
                f"{MAX_SURFACE_SUMMARY_UTF8_BYTES} bytes"
            )
        if type(self.keep_recent_turns) is not int or self.keep_recent_turns < 0:
            raise ValueError("compaction kept turns cannot be negative")

    @property
    def digest(self) -> str:
        """Bind a replacement to the exact policy that authorized it."""

        return fingerprint(
            {
                "format_version": 2,
                "enabled": self.enabled,
                "trigger_utf8_bytes": self.trigger_utf8_bytes,
                "max_summary_utf8_bytes": self.max_summary_utf8_bytes,
                "keep_recent_turns": self.keep_recent_turns,
            }
        )


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """Everything a summarizer is given, and deliberately nothing else.

    There is no Store, Session service, Tool registry, Runtime or approval
    handle here. A summarizer writes prose about messages it is handed; it
    cannot read other history, cause a side effect, or decide what is compacted.
    """

    session_id: str
    messages: tuple[ModelMessage, ...]
    max_summary_utf8_bytes: int
    kept_recent_turns: int


@runtime_checkable
class SessionSummarizer(Protocol):
    """Turn an exact list of replaced messages into one bounded summary."""

    @property
    def identity(self) -> SummarizerIdentity:
        ...

    async def summarize(self, request: SummaryRequest) -> str:
        ...


class BoundedHistorySummarizer:
    """The default host summarizer: a deterministic, bounded transcript digest.

    It is not a model. No summarizer in this runtime may call a provider,
    because the only auditable, budgeted and cancellable model-call mainline is
    the Session dispatch permit, and every ``request/snapshot`` it writes must
    reconstruct as the Surface projection - which a summarization request is
    not. Rather than add a second, unaudited provider path, the default keeps a
    deterministic digest and a host may inject a different summarizer.
    """

    __slots__ = ("_excerpt_chars",)

    name = "bounded-history"
    version = "1"

    def __init__(self, *, excerpt_chars: int = DEFAULT_SUMMARY_EXCERPT_CHARS) -> None:
        if type(excerpt_chars) is not int or excerpt_chars < 1:
            raise ValueError("summary excerpt width must be a positive integer")
        self._excerpt_chars = excerpt_chars

    @property
    def identity(self) -> SummarizerIdentity:
        return SummarizerIdentity(
            name=self.name,
            version=self.version,
            config_digest=fingerprint(
                {
                    "name": self.name,
                    "version": self.version,
                    "excerpt_chars": self._excerpt_chars,
                }
            ),
        )

    async def summarize(self, request: SummaryRequest) -> str:
        counts: dict[str, int] = {}
        for message in request.messages:
            counts[message.role] = counts.get(message.role, 0) + 1
        roles = ", ".join(f"{role}={counts[role]}" for role in sorted(counts))
        lines = [
            f"Compacted {len(request.messages)} earlier messages from this "
            f"Session ({roles})."
        ]
        for message in request.messages:
            lines.append(f"{_excerpt(message.role, 32)}: {self._body(message)}")
        return "\n".join(lines)

    def _body(self, message: ModelMessage) -> str:
        excerpt = _excerpt(message.content, self._excerpt_chars)
        if message.tool_calls:
            names = ", ".join(
                _excerpt(call.name, 32) for call in message.tool_calls
            )
            requested = f"(requested tools: {names})"
            return f"{excerpt} {requested}" if excerpt else requested
        return excerpt or "(empty)"


@dataclass(frozen=True, slots=True)
class CompactionReport:
    """What one durable replacement actually did."""

    session_id: str
    replacement_seq: int
    source_seqs: tuple[int, ...]
    summary: str
    method: str
    cut_seq: int
    kept_recent_turns: int
    history_utf8_bytes: int
    source_utf8_bytes: int
    summary_truncated: bool


class CompactionService:
    """The single owner of every Surface replacement in this runtime."""

    def __init__(
        self,
        sessions: SessionService,
        *,
        policy: CompactionPolicy | None = None,
        summarizer: SessionSummarizer | None = None,
    ) -> None:
        if type(sessions) is not SessionService:
            raise TypeError("sessions must be a SessionService")
        if policy is not None and type(policy) is not CompactionPolicy:
            raise TypeError("policy must be a CompactionPolicy")
        if policy is not None and policy.enabled:
            if summarizer is None:
                raise ValueError(
                    "automatic compaction requires an explicit summarizer"
                )
            if type(summarizer.identity) is not SummarizerIdentity:
                raise TypeError("summarizer must expose a SummarizerIdentity")
        self.sessions = sessions
        self._policy = policy
        self._summarizer = summarizer

    @property
    def policy(self) -> CompactionPolicy | None:
        return self._policy

    @property
    def automatic(self) -> bool:
        return self._policy is not None and self._policy.enabled

    async def replace_through(
        self,
        session_id: str,
        *,
        through_seq: int,
        summary: str,
    ) -> CompactionReport:
        """Append one human-authored replacement through a closed Turn.

        ``through_seq`` must be **exactly** the sequence of a ``turn/end`` that
        closed an open Turn. A sequence inside a Turn, or past the end of the
        log, is refused with `compaction-boundary-not-closed-turn` rather than
        slid back to the nearest earlier Turn: cutting inside a Turn would
        strand a Tool result or an open Step on the model Surface, and quietly
        compacting a different range than the caller named is worse than
        refusing. A human may compact any closed history, including an earlier
        summary.
        """

        await self.sessions.ensure_session(session_id)
        if type(through_seq) is not int or through_seq < 1:
            raise CompactionError("compaction-boundary-invalid")
        if type(summary) is not str or not summary.strip():
            raise CompactionError("compaction-summary-empty")
        return await self._append(
            session_id,
            method="manual",
            select=lambda events: self._plan(
                events,
                through_seq=through_seq,
                keep_recent_turns=0,
            ),
            summarize=lambda plan: _ready(summary),
            kept_recent_turns=0,
            max_summary_utf8_bytes=MAX_SURFACE_SUMMARY_UTF8_BYTES,
            policy_digest=None,
            summarizer=None,
        )

    async def compact_before_turn(
        self, session_id: str
    ) -> CompactionReport | None:
        """Compact closed history when the configured byte trigger is reached.

        Returns ``None`` when compaction is disabled, when the trigger has not
        been reached, or when there is no closed history left to compact -
        none of which is a failure. Every real failure raises
        `CompactionError` with a stable code and leaves history unchanged.
        """

        policy = self._policy
        summarizer = self._summarizer
        if policy is None or not policy.enabled or summarizer is None:
            return None
        await self.sessions.ensure_session(session_id)

        def select(events: tuple[EventEnvelope, ...]) -> SurfacePrefix | None:
            plan = self._plan(
                events,
                through_seq=None,
                keep_recent_turns=policy.keep_recent_turns,
            )
            if plan is None or plan.history_utf8_bytes < policy.trigger_utf8_bytes:
                return None
            # Re-running with nothing new would rewrite one summary into
            # another summary of itself. Repeated triggering is a no-op.
            if plan.new_history_sources == 0:
                return None
            return plan

        return await self._append(
            session_id,
            method="automatic",
            select=select,
            summarize=lambda plan: summarizer.summarize(
                SummaryRequest(
                    session_id=session_id,
                    messages=plan.messages,
                    max_summary_utf8_bytes=policy.max_summary_utf8_bytes,
                    kept_recent_turns=policy.keep_recent_turns,
                )
            ),
            kept_recent_turns=policy.keep_recent_turns,
            max_summary_utf8_bytes=policy.max_summary_utf8_bytes,
            policy_digest=policy.digest,
            summarizer=summarizer.identity,
        )

    # -- selection --------------------------------------------------------

    def _plan(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        through_seq: int | None,
        keep_recent_turns: int,
    ) -> SurfacePrefix | None:
        cut_seq = _cut_boundary(
            events, through_seq=through_seq, keep_recent_turns=keep_recent_turns
        )
        if cut_seq is None:
            return None
        prefix = surface_prefix(events, cut_seq=cut_seq)
        if prefix is None:
            return None
        split = split_tool_calls(
            surface_tool_links(events),
            prefix.source_seqs,
            hidden=_hidden_sources(events),
        )
        if split:
            raise CompactionError("compaction-tool-call-split")
        return prefix

    # -- durable write ----------------------------------------------------

    async def _append(
        self,
        session_id: str,
        *,
        method: str,
        select: Callable[[tuple[EventEnvelope, ...]], SurfacePrefix | None],
        summarize: Callable[[SurfacePrefix], Awaitable[str]],
        kept_recent_turns: int,
        max_summary_utf8_bytes: int,
        policy_digest: str | None,
        summarizer: SummarizerIdentity | None,
    ) -> CompactionReport | None:
        """Select, summarize and commit against one unchanged Session head.

        Summarizing may await, and the Session may move meanwhile. Binding a
        summary to history it no longer describes would be worse than not
        compacting at all, so the head observed during selection is carried into
        the append as ``expected_seq``: the Store's compare-and-swap is the
        linearization point, and a Session that moved rejects the write. The
        retry then re-reads and re-selects from scratch rather than resubmitting
        a payload that already lost its race.
        """

        for attempt in range(1, MAX_COMPACTION_APPEND_ATTEMPTS + 1):
            try:
                events = await self.sessions.read_session(session_id)
            except CompactionError:
                raise
            except Exception:
                raise CompactionError("compaction-read-failed") from None
            try:
                plan = select(events)
            except CompactionError:
                raise
            except ValueError:
                raise CompactionError("compaction-history-invalid") from None
            if plan is None:
                if method == "manual":
                    raise CompactionError("compaction-no-closed-history")
                return None
            expected_seq = events[-1].seq if events else 0

            try:
                summary = await summarize(plan)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A host may inject any summarizer. Its failure is a compaction
                # failure with a stable code, never an opaque exception escaping
                # into the Turn owner.
                raise CompactionError("compaction-summarizer-failed") from None
            try:
                bounded, truncated = bounded_summary(summary, max_summary_utf8_bytes)
            except ValueError:
                raise CompactionError("compaction-summary-invalid") from None

            try:
                data = surface_replacement_data(
                    method=method,
                    cut_seq=plan.cut_seq,
                    source_seqs=plan.source_seqs,
                    source_digest=plan.source_digest,
                    source_utf8_bytes=plan.source_utf8_bytes,
                    history_utf8_bytes=plan.history_utf8_bytes,
                    kept_recent_turns=kept_recent_turns,
                    policy_digest=policy_digest,
                    summarizer=summarizer,
                    summary=bounded,
                    summary_truncated=truncated,
                )
            except ValueError:
                raise CompactionError("compaction-payload-invalid") from None

            append = asyncio.create_task(
                self.sessions.append_session(
                    session_id,
                    SURFACE_REPLACE,
                    data,
                    expected_seq=expected_seq,
                ),
                name="traceh-surface-compaction",
            )
            try:
                event = await asyncio.shield(append)
            except asyncio.CancelledError as cancellation:
                await await_worker_convergence(append)
                # The reread still runs even though cancellation wins: a
                # may-have-committed append must converge before the caller
                # may retry or close the Store.
                await self._committed(session_id, data)
                if not append.cancelled() and append.exception() is not None:
                    raise cancellation from append.exception()
                raise cancellation
            except Exception as error:
                committed = await self._committed(session_id, data)
                if committed is True:
                    return await self._read_exact(session_id, data)
                if (
                    isinstance(error, ConcurrencyConflict)
                    and committed is False
                    and attempt < MAX_COMPACTION_APPEND_ATTEMPTS
                ):
                    continue
                code = (
                    "compaction-write-unknown"
                    if committed is None
                    else "compaction-write-failed"
                )
                raise CompactionError(code, committed=committed) from None
            try:
                replacement = parse_surface_replacement(event)
            except ValueError:
                raise CompactionError(
                    "compaction-write-invalid", committed=True
                ) from None
            return _report(session_id, event.seq, replacement)
        raise CompactionError("compaction-session-changed")

    async def _committed(
        self, session_id: str, data: dict[str, JsonValue]
    ) -> bool | None:
        expected_payload = canonical_json(data)

        def matches(event: EventEnvelope) -> bool:
            return (
                event.type == SURFACE_REPLACE
                # JSON identity is type-sensitive on purpose: Python would
                # otherwise treat ``True`` and ``1`` as the same reconciled
                # payload.
                and canonical_json(event.data) == expected_payload
            )

        return await committed_after_failure(
            lambda: self.sessions.read_session(session_id), matches
        )

    async def _read_exact(
        self, session_id: str, data: dict[str, JsonValue]
    ) -> CompactionReport:
        expected_payload = canonical_json(data)
        try:
            events = await self.sessions.read_session(session_id)
        except Exception:
            raise CompactionError(
                "compaction-write-unreadable", committed=True
            ) from None
        for event in reversed(events):
            if event.type != SURFACE_REPLACE:
                continue
            if canonical_json(event.data) != expected_payload:
                continue
            try:
                return _report(session_id, event.seq, parse_surface_replacement(event))
            except ValueError:
                break
        raise CompactionError("compaction-write-unreadable", committed=True)


def _report(
    session_id: str, seq: int, replacement: SurfaceReplacement
) -> CompactionReport:
    return CompactionReport(
        session_id=session_id,
        replacement_seq=seq,
        source_seqs=replacement.source_seqs,
        summary=replacement.summary,
        method=replacement.method,
        cut_seq=replacement.cut_seq,
        kept_recent_turns=replacement.kept_recent_turns,
        history_utf8_bytes=replacement.history_utf8_bytes,
        source_utf8_bytes=replacement.source_utf8_bytes,
        summary_truncated=replacement.summary_truncated,
    )


def _cut_boundary(
    events: tuple[EventEnvelope, ...],
    *,
    through_seq: int | None,
    keep_recent_turns: int,
) -> int | None:
    """Choose the exact `turn/end` a replacement may cut at.

    Automatic compaction takes the latest closed Turn its policy still allows.
    A manual boundary must name a closed Turn **exactly**: silently sliding a
    caller's sequence back to an earlier Turn would compact a different range
    than the one they asked for, and a command that quietly does something else
    is worse than one that refuses.
    """

    ends = closed_turn_ends(events)
    if through_seq is None:
        candidates = ends[: max(0, len(ends) - keep_recent_turns)]
        return candidates[-1] if candidates else None
    if not ends:
        return None
    if through_seq not in ends:
        raise CompactionError("compaction-boundary-not-closed-turn")
    return through_seq


def _hidden_sources(events: tuple[EventEnvelope, ...]) -> set[int]:
    hidden: set[int] = set()
    for event in events:
        if event.type == SURFACE_REPLACE:
            hidden.update(parse_surface_replacement(event).source_seqs)
    return hidden


async def _ready(value: str) -> str:
    return value


def _excerpt(value: str, limit: int) -> str:
    """One safe, bounded, single-line fragment of untrusted message text."""

    scrubbed = "".join(
        " " if is_unsafe_character(character) else character for character in value
    )
    flat = " ".join(scrubbed.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


__all__ = [
    "DEFAULT_SUMMARY_EXCERPT_CHARS",
    "MAX_COMPACTION_APPEND_ATTEMPTS",
    "BoundedHistorySummarizer",
    "CompactionError",
    "CompactionPolicy",
    "CompactionReport",
    "CompactionService",
    "SessionSummarizer",
    "SummaryRequest",
]
