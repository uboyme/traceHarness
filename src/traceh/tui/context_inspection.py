"""Fresh, read-only Context transparency projection for the TUI.

This module answers one question - "what does the model actually see, and how
close is it to being compacted" - from the single durable Session log. It adds
no fact, no cache, no index and no event: every value here is derived on demand
from events the Session owner already wrote, using the same parsers and
projectors the runtime itself uses.

Two distinctions carry the whole design and must not be blurred.

**Current projection is not the latest frozen request.** The Surface keeps
changing after a request is frozen - an assistant message, a Tool result, a new
Product context snapshot or a compaction can all land afterwards. Recomputing
today's Surface and calling it "what the model saw" would be a fabrication, so
the frozen request is read from ``request/snapshot`` instead, and the Product
context it contained is selected from events within its own ``source_seq``.

**Bytes are bytes.** There is no trusted general tokenizer here and no canonical
per-model context-window size, so nothing in this projection may be presented as
tokens or as a share of a model's context window. The only honest denominator is
the configured compaction trigger, and only when compaction is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import canonical_json
from traceh.api.llm import (
    REQUEST_SNAPSHOT_KEYS,
    ModelMessage,
    ModelRequest,
    dispatch_request_matches_composed,
)
from traceh.session.compaction import CompactionPolicy
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.product_context import latest_product_context
from traceh.session.service import SessionService
from traceh.session.surface_replacement import (
    SURFACE_COMPACTION_FAILED,
    SURFACE_REPLACE,
    parse_surface_replacement,
    surface_conversation,
    surface_utf8_bytes,
)


class ContextInspectionError(RuntimeError):
    """One stable, short reason the Context projection is unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ContextCompactionRecord:
    """One durable ``surface/replace`` as the user needs to read it."""

    seq: int
    method: str
    cut_seq: int
    source_count: int
    source_utf8_bytes: int
    history_utf8_bytes: int
    summary_utf8_bytes: int
    summary_truncated: bool
    kept_recent_turns: int
    policy_digest: str | None
    summarizer_name: str | None
    summarizer_version: str | None
    summarizer_config_digest: str | None
    summary: str
    #: ``True`` only when this record's own policy digest equals the digest of
    #: the policy this process is running. A replacement stores a digest, never
    #: the historical threshold values, so the current threshold must never be
    #: presented as the one that authorized an older record.
    matches_current_policy: bool


@dataclass(frozen=True, slots=True)
class ContextCompactionFailure:
    seq: int
    code: str
    #: ``True`` committed, ``False`` provably not, ``None`` unknown.
    committed: bool | None


@dataclass(frozen=True, slots=True)
class ContextTaskEntry:
    task_id: str
    status: str
    requested_mode: str
    resolved_mode: str | None


@dataclass(frozen=True, slots=True)
class ContextProductView:
    """The ProductTask memory a Surface actually carries."""

    snapshot_seq: int
    context_id: str
    focus_task_id: str
    focus_status: str
    shown: int
    total: int
    omitted: int
    tasks: tuple[ContextTaskEntry, ...]
    messages: int
    utf8_bytes: int


@dataclass(frozen=True, slots=True)
class ContextRequestView:
    """The latest legal ``request/snapshot``, read rather than recomputed."""

    seq: int
    source_seq: int
    composition_revision: str
    provider: str
    model: str
    composed_utf8_bytes: int
    dispatch_utf8_bytes: int
    composed_fingerprint: str
    dispatch_fingerprint: str
    system_prompt_utf8_bytes: int
    product_context_messages: int
    product_context_utf8_bytes: int
    conversation_messages: int
    conversation_utf8_bytes: int
    tool_schemas: int
    tool_utf8_bytes: int
    composed_max_output_tokens: int | None
    dispatch_max_output_tokens: int | None
    dispatch_matches_composed: bool


@dataclass(frozen=True, slots=True)
class ContextPolicyView:
    enabled: bool
    trigger_utf8_bytes: int
    max_summary_utf8_bytes: int
    keep_recent_turns: int
    digest: str


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """One discardable display snapshot. Never a fact source, never cached."""

    session_id: str
    head_seq: int
    conversation_messages: int
    #: The exact metric `CompactionService` triggers on: canonical UTF-8 bytes
    #: of model-visible conversation, excluding host Product context messages.
    conversation_utf8_bytes: int
    #: How many summaries the model can see right now. Different from
    #: ``len(compactions)``: widening compaction folds earlier summaries into a
    #: later one, so durable events accumulate while visible summaries do not.
    visible_summaries: int
    compactions: tuple[ContextCompactionRecord, ...]
    failures: tuple[ContextCompactionFailure, ...]
    product: ContextProductView | None
    request: ContextRequestView | None
    policy: ContextPolicyView | None

    @property
    def compaction_count(self) -> int:
        return len(self.compactions)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def latest_compaction(self) -> ContextCompactionRecord | None:
        return self.compactions[-1] if self.compactions else None

    @property
    def trigger_ratio(self) -> float | None:
        """Share of the configured compaction trigger, never of a model window."""

        policy = self.policy
        if policy is None or not policy.enabled or policy.trigger_utf8_bytes <= 0:
            return None
        return self.conversation_utf8_bytes / policy.trigger_utf8_bytes


class ContextInspectionReader:
    """Project one requester Session into a Context display snapshot."""

    __slots__ = ("_policy", "_sessions")

    def __init__(
        self,
        sessions: SessionService,
        *,
        policy: CompactionPolicy | None = None,
    ) -> None:
        if type(sessions) is not SessionService:
            raise ContextInspectionError("context-inspection-sessions-invalid")
        if policy is not None and type(policy) is not CompactionPolicy:
            raise ContextInspectionError("context-inspection-policy-invalid")
        self._sessions = sessions
        self._policy = policy

    @property
    def policy(self) -> CompactionPolicy | None:
        return self._policy

    async def load(self, session_id: str) -> ContextSnapshot:
        """Read the Session once and derive everything from that one read.

        A single stream read is already a consistent snapshot, so no second
        read and no head reconciliation is needed here; the caller compares
        ``head_seq`` when deciding whether a newer refresh has superseded this
        one.
        """

        try:
            events = await self._sessions.read_session(session_id)
        except Exception:
            raise ContextInspectionError("context-inspection-read-failed") from None
        try:
            effects = await self._sessions.read_effects(session_id)
        except Exception:
            raise ContextInspectionError("context-inspection-read-failed") from None
        # Same fail-closed rule the task-conversation projection already uses:
        # a Session whose protocol is broken must not be described with
        # half-true numbers.
        if CoreInvariantChecker().check(events, effects):
            raise ContextInspectionError("context-inspection-session-invalid")

        try:
            entries = surface_conversation(events)
        except (TypeError, ValueError):
            raise ContextInspectionError("context-inspection-surface-invalid") from None

        compactions = self._compactions(events)
        return ContextSnapshot(
            session_id=session_id,
            head_seq=events[-1].seq if events else 0,
            conversation_messages=len(entries),
            conversation_utf8_bytes=surface_utf8_bytes(
                entry.message for entry in entries
            ),
            visible_summaries=sum(
                1 for entry in entries if entry.replacement is not None
            ),
            compactions=compactions,
            failures=self._failures(events),
            product=self._product(events, through_seq=None),
            request=self._request(events),
            policy=self._policy_view(),
        )

    # -- compaction -------------------------------------------------------

    def _compactions(
        self, events: tuple[EventEnvelope, ...]
    ) -> tuple[ContextCompactionRecord, ...]:
        current = None if self._policy is None else self._policy.digest
        records: list[ContextCompactionRecord] = []
        for event in events:
            if event.type != SURFACE_REPLACE:
                continue
            try:
                replacement = parse_surface_replacement(event)
            except (TypeError, ValueError):
                raise ContextInspectionError(
                    "context-inspection-replacement-invalid"
                ) from None
            summarizer = replacement.summarizer
            records.append(
                ContextCompactionRecord(
                    seq=event.seq,
                    method=replacement.method,
                    cut_seq=replacement.cut_seq,
                    source_count=len(replacement.source_seqs),
                    source_utf8_bytes=replacement.source_utf8_bytes,
                    history_utf8_bytes=replacement.history_utf8_bytes,
                    summary_utf8_bytes=len(replacement.summary.encode("utf-8")),
                    summary_truncated=replacement.summary_truncated,
                    kept_recent_turns=replacement.kept_recent_turns,
                    policy_digest=replacement.policy_digest,
                    summarizer_name=None if summarizer is None else summarizer.name,
                    summarizer_version=(
                        None if summarizer is None else summarizer.version
                    ),
                    summarizer_config_digest=(
                        None if summarizer is None else summarizer.config_digest
                    ),
                    summary=replacement.summary,
                    matches_current_policy=(
                        current is not None
                        and replacement.policy_digest is not None
                        and replacement.policy_digest == current
                    ),
                )
            )
        return tuple(records)

    def _failures(
        self, events: tuple[EventEnvelope, ...]
    ) -> tuple[ContextCompactionFailure, ...]:
        failures: list[ContextCompactionFailure] = []
        for event in events:
            if event.type != SURFACE_COMPACTION_FAILED:
                continue
            data = event.data if isinstance(event.data, dict) else {}
            raw_code = data.get("code")
            committed = data.get("committed")
            failures.append(
                ContextCompactionFailure(
                    seq=event.seq,
                    code=raw_code if isinstance(raw_code, str) and raw_code else "unknown",
                    # Only an exact boolean answers the question; anything else
                    # - absent, null, or the wrong type - stays unknown rather
                    # than being reported as "nothing was written".
                    committed=(
                        committed if committed is True or committed is False else None
                    ),
                )
            )
        return tuple(failures)

    # -- product context --------------------------------------------------

    def _product(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        through_seq: int | None,
    ) -> ContextProductView | None:
        selected = (
            events
            if through_seq is None
            else tuple(event for event in events if event.seq <= through_seq)
        )
        try:
            latest = latest_product_context(selected)
        except (TypeError, ValueError):
            raise ContextInspectionError(
                "context-inspection-product-context-invalid"
            ) from None
        if latest is None:
            return None
        seq, snapshot = latest
        return ContextProductView(
            snapshot_seq=seq,
            context_id=snapshot.context_id,
            focus_task_id=snapshot.focus.task_id,
            focus_status=snapshot.focus.status.value,
            shown=len(snapshot.tasks),
            total=snapshot.total_tasks,
            omitted=snapshot.omitted_tasks,
            tasks=tuple(
                ContextTaskEntry(
                    task_id=task.task_id,
                    status=task.status.value,
                    requested_mode=task.requested_mode.value,
                    resolved_mode=(
                        None if task.resolved_mode is None else task.resolved_mode.value
                    ),
                )
                for task in snapshot.tasks
            ),
            messages=len(snapshot.messages),
            utf8_bytes=surface_utf8_bytes(snapshot.messages),
        )

    # -- latest frozen request --------------------------------------------

    def _request(
        self, events: tuple[EventEnvelope, ...]
    ) -> ContextRequestView | None:
        snapshot = next(
            (event for event in reversed(events) if event.type == "request/snapshot"),
            None,
        )
        if snapshot is None:
            return None
        # The latest snapshot is the one being described. Skipping a malformed
        # one to show an older request as "latest" would be a quiet lie, so it
        # fails closed instead.
        data = snapshot.data if isinstance(snapshot.data, dict) else {}
        if set(data) != REQUEST_SNAPSHOT_KEYS:
            raise ContextInspectionError("context-inspection-request-invalid")
        try:
            raw_composed = data["composed_request"]
            raw_dispatch = data["dispatch_request"]
            if not isinstance(raw_composed, dict) or not isinstance(raw_dispatch, dict):
                raise ValueError
            composed = ModelRequest.from_dict(raw_composed)
            dispatch = ModelRequest.from_dict(raw_dispatch)
            if (
                composed.to_dict() != raw_composed
                or dispatch.to_dict() != raw_dispatch
            ):
                raise ValueError
            source_seq = data["source_seq"]
            composition_revision = data["composition_revision"]
            composed_fingerprint = data["composed_fingerprint"]
            dispatch_fingerprint = data["dispatch_fingerprint"]
            if (
                type(source_seq) is not int
                or source_seq < 1
                or not isinstance(composition_revision, str)
                or not isinstance(composed_fingerprint, str)
                or not isinstance(dispatch_fingerprint, str)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ContextInspectionError("context-inspection-request-invalid") from None

        # The Product context this request carried must come from within its own
        # source boundary. Today's ProductTask head may be newer, and using it
        # would rewrite what the model saw when this request was frozen.
        historical = self._product(events, through_seq=source_seq)
        leading = 0
        product_bytes = 0
        if historical is not None:
            expected = self._product_messages(events, source_seq)
            if len(composed.messages) < len(expected) or any(
                composed.messages[index] != message
                for index, message in enumerate(expected)
            ):
                raise ContextInspectionError(
                    "context-inspection-request-product-mismatch"
                )
            leading = len(expected)
            product_bytes = surface_utf8_bytes(expected)
        conversation = composed.messages[leading:]
        return ContextRequestView(
            seq=snapshot.seq,
            source_seq=source_seq,
            composition_revision=composition_revision,
            provider=dispatch.provider,
            model=dispatch.model,
            composed_utf8_bytes=_canonical_utf8_bytes(raw_composed),
            dispatch_utf8_bytes=_canonical_utf8_bytes(raw_dispatch),
            composed_fingerprint=composed_fingerprint,
            dispatch_fingerprint=dispatch_fingerprint,
            system_prompt_utf8_bytes=len(
                (dispatch.system_prompt or "").encode("utf-8")
            ),
            product_context_messages=leading,
            product_context_utf8_bytes=product_bytes,
            conversation_messages=len(conversation),
            conversation_utf8_bytes=surface_utf8_bytes(conversation),
            tool_schemas=len(dispatch.tools),
            tool_utf8_bytes=_canonical_utf8_bytes(
                [tool.to_dict() for tool in dispatch.tools]
            ),
            composed_max_output_tokens=composed.max_output_tokens,
            dispatch_max_output_tokens=dispatch.max_output_tokens,
            dispatch_matches_composed=dispatch_request_matches_composed(
                composed, dispatch
            ),
        )

    def _product_messages(
        self, events: tuple[EventEnvelope, ...], source_seq: int
    ) -> tuple[ModelMessage, ...]:
        selected = tuple(event for event in events if event.seq <= source_seq)
        try:
            latest = latest_product_context(selected)
        except (TypeError, ValueError):
            raise ContextInspectionError(
                "context-inspection-product-context-invalid"
            ) from None
        return () if latest is None else latest[1].messages

    def _policy_view(self) -> ContextPolicyView | None:
        policy = self._policy
        if policy is None:
            return None
        return ContextPolicyView(
            enabled=policy.enabled,
            trigger_utf8_bytes=policy.trigger_utf8_bytes,
            max_summary_utf8_bytes=policy.max_summary_utf8_bytes,
            keep_recent_turns=policy.keep_recent_turns,
            digest=policy.digest,
        )


def _canonical_utf8_bytes(value: object) -> int:
    return len(canonical_json(value).encode("utf-8"))


__all__ = [
    "ContextCompactionFailure",
    "ContextCompactionRecord",
    "ContextInspectionError",
    "ContextInspectionReader",
    "ContextPolicyView",
    "ContextProductView",
    "ContextRequestView",
    "ContextSnapshot",
    "ContextTaskEntry",
]
