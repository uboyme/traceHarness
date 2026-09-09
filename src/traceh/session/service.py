"""High-level Session and Effect stream service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.history import HistoryPageRequest, HistoryReadPolicy
from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import (
    ModelAttemptIdentity,
    ModelRequest,
    dispatch_request_matches_composed,
    model_attempt_reservation_id,
)
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore
from traceh.session.protocol import CONTEXT_PROTOCOL, require_session_protocol


class SessionNotFoundError(LookupError):
    pass


class ModelAttemptConflictError(RuntimeError):
    """The caller did not acquire this Step ordinal's dispatch permit."""

    code = "model-attempt-dispatch-conflict"

    def __init__(self, *, ownership_lost: bool = False) -> None:
        super().__init__(self.code)
        self.ownership_lost = ownership_lost


class ContextInputWriteError(RuntimeError):
    """The Context append failed, with an explicitly reconciled commit state."""

    code = "context-input-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        super().__init__(self.code)
        self.committed = committed


class SessionService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def session_stream(session_id: str) -> str:
        return f"session:{session_id}"

    @staticmethod
    def effect_stream(session_id: str) -> str:
        return f"effects:{session_id}"

    def _lock(self, stream_id: str) -> asyncio.Lock:
        return self._locks.setdefault(stream_id, asyncio.Lock())

    async def _append(
        self,
        stream_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        expected_seq: int | None = None,
        durability: Durability = Durability.SYNC,
        actor_id: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        composition_revision: str | None = None,
    ) -> EventEnvelope:
        async with self._lock(stream_id):
            actual_expected_seq = (
                await self.store.head(stream_id) if expected_seq is None else expected_seq
            )
            appended = await self.store.append(
                stream_id,
                expected_seq=actual_expected_seq,
                events=(
                    PendingEvent(
                        type=event_type,
                        data=data,
                        actor_id=actor_id,
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                        composition_revision=composition_revision,
                    ),
                ),
                durability=durability,
            )
            return appended[0]

    async def create_session(
        self,
        workspace: Path,
        *,
        metadata: dict[str, JsonValue] | None = None,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or str(uuid4())
        stream_id = self.session_stream(session_id)
        existing = await self.store.read(stream_id)
        if existing:
            raise ValueError(f"session already exists: {session_id}")
        await self._append(
            stream_id,
            "session/created",
            {
                "session_id": session_id,
                "workspace": str(workspace.resolve()),  # noqa: ASYNC240
                "metadata": metadata or {},
                "context_protocol": CONTEXT_PROTOCOL,
            },
            expected_seq=0,
        )
        return session_id

    async def ensure_session(self, session_id: str) -> None:
        events = await self.read_session(session_id)
        if not events or events[0].type != "session/created":
            raise SessionNotFoundError(session_id)

    async def append_session(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        expected_seq: int | None = None,
        durability: Durability = Durability.SYNC,
        actor_id: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        composition_revision: str | None = None,
    ) -> EventEnvelope:
        await self.ensure_session(session_id)
        return await self._append(
            self.session_stream(session_id),
            event_type,
            data,
            expected_seq=expected_seq,
            durability=durability,
            actor_id=actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            composition_revision=composition_revision,
        )

    async def append_context_input(
        self,
        session_id: str,
        data: dict[str, JsonValue],
        *,
        expected_seq: int,
        correlation_id: UUID | None = None,
    ) -> EventEnvelope:
        """Freeze this open Step's one Context before its Composition.

        The append is owned until it converges even on repeated cancellation.
        A may-have-committed result is reconciled, never automatically retried.
        """

        from traceh.agents.commit_reconciliation import committed_after_failure
        from traceh.session.context_input import (
            parse_context_input,
            validate_context_input_sources,
        )

        snapshot = parse_context_input(data)
        frozen = snapshot.to_dict()
        if (
            type(expected_seq) is not int
            or expected_seq < 1
            or frozen["session_id"] != session_id
            or frozen["observed_session_seq"] > expected_seq
        ):
            raise ValueError("context-input-binding-mismatch")
        stream_id = self.session_stream(session_id)
        revision = frozen["composition_revision"]
        canonical_payload = canonical_json(frozen)
        async with self._lock(stream_id):
            events = await self.read_session(session_id)
            if not events:
                raise SessionNotFoundError(session_id)
            if events[-1].seq != expected_seq:
                raise ConcurrencyConflict("context source boundary changed")
            validate_context_input_sources(snapshot, events)
            from traceh.memory.context import read_frozen_source, verify_blocks
            from traceh.session.context_input import _parse_policy

            memory_source = await read_frozen_source(data, self.store.read, events)
            if memory_source is not None:
                verify_blocks(data, memory_source, _parse_policy(data["policy"]).memory)

            open_turn: str | None = None
            open_step: str | None = None
            step_start_seq: int | None = None
            has_input_or_composition = False
            for event in events:
                if event.type == "turn/start":
                    open_turn = event.data.get("turn_id")
                elif event.type == "turn/end":
                    open_turn = None
                elif event.type == "step/start":
                    open_step = event.data.get("step_id")
                    step_start_seq = event.seq
                    has_input_or_composition = False
                elif event.type == "step/end":
                    open_step = None
                elif event.type in {"context/input", "composition/snapshot"}:
                    has_input_or_composition = True
            if (
                open_turn != frozen["turn_id"]
                or open_step != frozen["step_id"]
                or step_start_seq is None
                or frozen["observed_session_seq"] < step_start_seq
                or has_input_or_composition
            ):
                raise ValueError("context-input-binding-mismatch")
            pending = PendingEvent(
                type="context/input",
                data=frozen,
                correlation_id=correlation_id,
                composition_revision=revision,
            )
            task = asyncio.create_task(
                self.store.append(
                    stream_id,
                    expected_seq=expected_seq,
                    events=(pending,),
                    durability=Durability.SYNC,
                ),
                name="traceh-context-input-append",
            )
            try:
                return (await asyncio.shield(task))[0]
            except (asyncio.CancelledError, Exception) as primary:
                await await_worker_convergence(task)
                committed = await committed_after_failure(
                    lambda: self.read_session(session_id),
                    lambda event: (
                        event.type == "context/input"
                        and event.stream_id == stream_id
                        and event.seq == expected_seq + 1
                        and event.correlation_id == correlation_id
                        and event.composition_revision == revision
                        and canonical_json(event.data) == canonical_payload
                    ),
                )
                failure = ContextInputWriteError(committed=committed)
                if isinstance(primary, asyncio.CancelledError):
                    raise primary from failure
                raise failure from primary

    async def append_history_requests(
        self,
        session_id: str,
        *,
        turn_id: str,
        step_id: str,
        user_message_ref: dict[str, JsonValue],
        requests: tuple[HistoryPageRequest, ...],
        policy: HistoryReadPolicy,
        correlation_id: UUID | None = None,
    ) -> tuple[EventEnvelope, ...]:
        """Atomically record explicit host requests before the first Context.

        The Turn owner supplies typed inputs. This owner validates their prior
        disclosure, owns the entire append through cancellation, and reports an
        exact batch commit state without resubmitting an uncertain operation.
        """

        from traceh.agents.commit_reconciliation import committed_after_failure
        from traceh.session.history_requests import (
            HistoryRequestWriteError,
            validate_user_requests,
        )

        stream_id = self.session_stream(session_id)
        async with self._lock(stream_id):
            events = await self.read_session(session_id)
            if not events:
                raise SessionNotFoundError(session_id)
            payloads = validate_user_requests(
                events,
                session_id=session_id,
                turn_id=turn_id,
                step_id=step_id,
                user_message_ref=user_message_ref,
                requests=requests,
                policy=policy,
            )
            expected_seq = events[-1].seq
            pending = tuple(
                PendingEvent(
                    type="history/requested",
                    data=payload,
                    event_id=uuid4(),
                    correlation_id=correlation_id,
                )
                for payload in payloads
            )
            canonical_payloads = tuple(canonical_json(item.data) for item in pending)

            async def read_batch() -> tuple[EventEnvelope, ...]:
                observed = await self.read_session(session_id)
                found: list[EventEnvelope] = []
                for offset, item in enumerate(pending, 1):
                    seq = expected_seq + offset
                    if seq > len(observed):
                        continue
                    candidate = observed[seq - 1]
                    if (
                        candidate.event_id == item.event_id
                        and candidate.stream_id == stream_id
                        and candidate.seq == seq
                        and candidate.type == "history/requested"
                        and candidate.correlation_id == correlation_id
                        and candidate.composition_revision is None
                        and canonical_json(candidate.data) == canonical_payloads[offset - 1]
                    ):
                        found.append(candidate)
                if found and len(found) != len(pending):
                    raise ValueError("history-request-partial-batch")
                return tuple(found)

            task = asyncio.create_task(
                self.store.append(
                    stream_id,
                    expected_seq=expected_seq,
                    events=pending,
                    durability=Durability.SYNC,
                ),
                name="traceh-history-request-append",
            )
            try:
                return await asyncio.shield(task)
            except (asyncio.CancelledError, Exception) as primary:
                await await_worker_convergence(task)
                committed = await committed_after_failure(read_batch, lambda event: True)
                failure = HistoryRequestWriteError(committed=committed)
                if isinstance(primary, asyncio.CancelledError):
                    raise primary from failure
                raise failure from primary

    async def start_model_attempt(
        self,
        session_id: str,
        *,
        attempt: ModelAttemptIdentity,
        source_seq: int,
        composition_revision: str,
        composed_request: ModelRequest,
        composed_fingerprint: str,
        dispatch_request: ModelRequest,
        dispatch_fingerprint: str,
        reservation_id: str | None,
        retry_wait_milliseconds: int = 0,
        retry_failure_code: str | None = None,
        retry_failure_category: str | None = None,
        correlation_id: UUID | None = None,
    ) -> tuple[EventEnvelope, EventEnvelope]:
        """Atomically freeze the Step request and claim one dispatch ordinal.

        The first ordinal appends ``request/snapshot`` and
        ``model/attempt-start`` in one Store CAS. Later ordinals reuse that one
        snapshot. A concurrent or recovered owner can therefore observe the
        existing permit, but can never append another ordinal by treating fact
        idempotency as execution authority.
        """

        if attempt.session_id != session_id:
            raise ModelAttemptConflictError
        if type(source_seq) is not int or source_seq < 1:
            raise ModelAttemptConflictError
        if not isinstance(composition_revision, str) or not composition_revision:
            raise ModelAttemptConflictError
        expected_metadata = {
            "session_id": session_id,
            "turn_id": attempt.turn_id,
            "step_id": attempt.step_id,
            "composition_revision": composition_revision,
        }
        for request in (composed_request, dispatch_request):
            if any(request.metadata.get(key) != value for key, value in expected_metadata.items()):
                raise ModelAttemptConflictError
        if fingerprint(composed_request.to_dict()) != composed_fingerprint:
            raise ModelAttemptConflictError
        if fingerprint(dispatch_request.to_dict()) != dispatch_fingerprint:
            raise ModelAttemptConflictError
        if not dispatch_request_matches_composed(composed_request, dispatch_request):
            raise ModelAttemptConflictError
        expected_reservation_id = model_attempt_reservation_id(attempt)
        if reservation_id is not None and reservation_id != expected_reservation_id:
            raise ModelAttemptConflictError
        if type(retry_wait_milliseconds) is not int or retry_wait_milliseconds < 0:
            raise ModelAttemptConflictError
        if attempt.ordinal == 1:
            if (
                retry_wait_milliseconds != 0
                or retry_failure_code is not None
                or retry_failure_category is not None
            ):
                raise ModelAttemptConflictError
        elif (
            not isinstance(retry_failure_code, str)
            or not retry_failure_code
            or not isinstance(retry_failure_category, str)
            or not retry_failure_category
        ):
            raise ModelAttemptConflictError

        stream_id = self.session_stream(session_id)
        async with self._lock(stream_id):
            events = await self.store.read(stream_id)
            if not events or events[0].type != "session/created":
                raise SessionNotFoundError(session_id)
            require_session_protocol(events, session_id=session_id)
            # Import at the ownership boundary rather than module import time:
            # the invariant checker also validates plugin identity and that
            # dependency graph reaches ToolRuntime, which itself uses this
            # service.
            from traceh.session.invariants import CoreInvariantChecker

            if CoreInvariantChecker().check(events):
                # This service owns the only durable dispatch permit.  A later
                # Attempt cannot be authorized from history that the shared
                # Session projector already proves is not legally replayable.
                raise ModelAttemptConflictError(ownership_lost=True)
            head = events[-1].seq
            if source_seq > head:
                raise ModelAttemptConflictError
            from traceh.runtime.request_builder import build_request_from_events
            from traceh.session.surface import SurfaceProjector

            try:
                rebuilt = build_request_from_events(
                    events,
                    SurfaceProjector(),
                    session_id=session_id,
                    turn_id=attempt.turn_id,
                    step_id=attempt.step_id,
                    through_seq=source_seq,
                )
                if (
                    rebuilt.request.metadata["composition_revision"] != composition_revision
                    or rebuilt.fingerprint != composed_fingerprint
                    or canonical_json(rebuilt.request.to_dict())
                    != canonical_json(composed_request.to_dict())
                ):
                    raise ValueError("request-context-binding-mismatch")
            except (KeyError, TypeError, ValueError):
                raise ModelAttemptConflictError(ownership_lost=True) from None
            from traceh.api.llm import request_source_fields

            input_fields = request_source_fields(rebuilt.request.metadata)

            open_turn: str | None = None
            open_step: str | None = None
            starts: list[EventEnvelope] = []
            all_attempt_ids: set[str] = set()
            ended: set[str] = set()
            attempt_ends: dict[str, EventEnvelope] = {}
            snapshots: list[EventEnvelope] = []
            for event in events:
                if event.type == "turn/start":
                    open_turn = str(event.data.get("turn_id", ""))
                elif event.type == "turn/end":
                    open_turn = None
                elif event.type == "step/start":
                    open_step = str(event.data.get("step_id", ""))
                elif event.type == "step/end":
                    open_step = None
                elif event.type == "request/snapshot" and (
                    event.data.get("turn_id") == attempt.turn_id
                    and event.data.get("step_id") == attempt.step_id
                ):
                    snapshots.append(event)
                elif event.type == "model/attempt-start" and (
                    event.data.get("turn_id") == attempt.turn_id
                    and event.data.get("step_id") == attempt.step_id
                ):
                    starts.append(event)
                    all_attempt_ids.add(str(event.data.get("attempt_id", "")))
                elif event.type == "model/attempt-start":
                    all_attempt_ids.add(str(event.data.get("attempt_id", "")))
                elif event.type == "model/attempt-end":
                    ended_id = str(event.data.get("attempt_id", ""))
                    ended.add(ended_id)
                    attempt_ends[ended_id] = event

            if open_turn != attempt.turn_id or open_step != attempt.step_id:
                raise ModelAttemptConflictError(ownership_lost=True)
            if any(str(event.data.get("attempt_id", "")) not in ended for event in starts):
                raise ModelAttemptConflictError(ownership_lost=True)
            if attempt.attempt_id in all_attempt_ids:
                raise ModelAttemptConflictError(ownership_lost=True)
            if any(event.data.get("ordinal") == attempt.ordinal for event in starts):
                raise ModelAttemptConflictError(ownership_lost=True)
            ordinals = [event.data.get("ordinal") for event in starts]
            if ordinals != list(range(1, len(starts) + 1)):
                raise ModelAttemptConflictError(ownership_lost=True)
            if attempt.ordinal != len(starts) + 1:
                raise ModelAttemptConflictError(ownership_lost=True)
            if attempt.ordinal > 1:
                previous = starts[-1]
                previous_end = attempt_ends.get(str(previous.data.get("attempt_id", "")))
                if (
                    previous_end is None
                    or previous_end.data.get("status") != "failed"
                    or previous_end.data.get("failure_code") != retry_failure_code
                    or previous_end.data.get("failure_category") != retry_failure_category
                ):
                    raise ModelAttemptConflictError(ownership_lost=True)

            pending: list[PendingEvent] = []
            if attempt.ordinal == 1:
                if snapshots:
                    raise ModelAttemptConflictError(ownership_lost=True)
                request_snapshot_seq = head + 1
                snapshot_data: dict[str, JsonValue] = {
                    "turn_id": attempt.turn_id,
                    "step_id": attempt.step_id,
                    "source_seq": source_seq,
                    "composition_revision": composition_revision,
                    **input_fields,
                    "composed_fingerprint": composed_fingerprint,
                    "dispatch_fingerprint": dispatch_fingerprint,
                    "composed_request": composed_request.to_dict(),
                    "dispatch_request": dispatch_request.to_dict(),
                }
                pending.append(
                    PendingEvent(
                        type="request/snapshot",
                        data=snapshot_data,
                        correlation_id=correlation_id,
                        composition_revision=composition_revision,
                    )
                )
            else:
                if len(snapshots) != 1:
                    raise ModelAttemptConflictError
                snapshot = snapshots[0]
                if (
                    snapshot.data.get("composed_fingerprint") != composed_fingerprint
                    or snapshot.data.get("dispatch_fingerprint") != dispatch_fingerprint
                ):
                    raise ModelAttemptConflictError
                request_snapshot_seq = snapshot.seq

            start_data: dict[str, JsonValue] = {
                "turn_id": attempt.turn_id,
                "step_id": attempt.step_id,
                "attempt_id": attempt.attempt_id,
                "ordinal": attempt.ordinal,
                "request_snapshot_seq": request_snapshot_seq,
                "dispatch_fingerprint": dispatch_fingerprint,
                "reservation_id": reservation_id,
                "provider": dispatch_request.provider,
                "model": dispatch_request.model,
                "retry_wait_milliseconds": retry_wait_milliseconds,
                "retry_failure_code": retry_failure_code,
                "retry_failure_category": retry_failure_category,
            }
            pending.append(
                PendingEvent(
                    type="model/attempt-start",
                    data=start_data,
                    correlation_id=correlation_id,
                    composition_revision=composition_revision,
                )
            )
            try:
                appended = await self.store.append(
                    stream_id,
                    expected_seq=head,
                    events=tuple(pending),
                    durability=Durability.SYNC,
                )
            except ConcurrencyConflict:
                raise ModelAttemptConflictError(ownership_lost=True) from None
            if attempt.ordinal == 1:
                return appended[0], appended[1]
            return snapshots[0], appended[0]

    async def append_effect(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        durability: Durability = Durability.SYNC,
        causation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEnvelope:
        return await self._append(
            self.effect_stream(session_id),
            event_type,
            data,
            durability=durability,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )

    async def read_session(self, session_id: str) -> tuple[EventEnvelope, ...]:
        events = await self.store.read(self.session_stream(session_id))
        require_session_protocol(events, session_id=session_id)
        contexts = [e for e in events if e.type == "context/input"]
        if contexts:
            from traceh.session.skill_selection import head_ref, project_selection, validate_head

            selections = await self.read_skill_selection(session_id)
            for event in contexts:
                from traceh.session.context_input import parse_context_input
                from traceh.session.skill_retrieval import receipt_skill_ids

                data = parse_context_input(event.data).to_dict()
                from traceh.memory.context import read_frozen_source, verify_blocks
                from traceh.session.context_input import _parse_policy

                memory_source = await read_frozen_source(data, self.store.read, events)
                if memory_source is not None:
                    verify_blocks(data, memory_source, _parse_policy(data["policy"]).memory)
                reference = data.get("selection_head")
                validate_head(reference, session_id)
                prefix = selections[: reference["head_seq"]]
                if head_ref(session_id, prefix) != reference:
                    raise ValueError("context-selection-binding-mismatch")
                selection = project_selection(prefix, session_id)
                skill_receipt = data["retrieval"]["skill"] if data["retrieval"] else None
                if skill_receipt is not None:
                    from traceh.kernel.composition import CompositionSnapshot
                    from traceh.session.context_input import _parse_policy
                    from traceh.session.retrieval import validate_coverage
                    from traceh.session.skill_retrieval import exact_values, prepare_corpus

                    following = []
                    for following_event in events[event.seq :]:
                        if following_event.type in {"step/end", "step/start", "turn/end"}:
                            break
                        if following_event.type == "composition/snapshot":
                            following.append(following_event)
                    if len(following) == 1:
                        composition = CompositionSnapshot.from_dict(following[0].data)
                        policy = _parse_policy(data["policy"]).skills
                        corpus, _, rows, descriptors, _ = prepare_corpus(
                            composition, prefix, session_id, policy
                        )
                        import json

                        manifest = json.loads(corpus.manifest_json)
                        if (
                            skill_receipt["corpus_key"] != corpus.key
                            or skill_receipt["corpus_digest"] != manifest["corpus_digest"]
                            or skill_receipt["eligible_count"] != manifest["item_count"]
                        ):
                            raise ValueError("context-retrieval-source-mismatch")
                        validate_coverage(
                            skill_receipt,
                            rows,
                            exact_values(descriptors),
                            data["query"]["text"],
                            policy,
                        )
                for skill_id, version in receipt_skill_ids(skill_receipt):
                    if (
                        selection is None
                        or selection["catalog_digest"] != data["skill_catalog_digest"]
                        or {"skill_id": skill_id, "version": version} not in selection["skills"]
                    ):
                        raise ValueError("context-skill-selection-mismatch")
                for block in data.get("blocks", []):
                    if block["kind"] == "skill" and (
                        selection is None
                        or selection["catalog_digest"] != data["skill_catalog_digest"]
                        or {"skill_id": block["id"], "version": block["version"]}
                        not in selection["skills"]
                    ):
                        raise ValueError("context-skill-selection-mismatch")
        return events

    async def read_skill_selection(self, session_id):
        from traceh.session.skill_selection import project_selection

        events = await self.store.read(f"context-selection:{session_id}")
        project_selection(events, session_id)
        return events

    def _context_index_backend(self):
        from traceh.session.event_feed import PublishingEventStore
        from traceh.session.sqlite import SqliteEventStore

        store = self.store
        while isinstance(store, PublishingEventStore):
            store = store.inner
        return store if isinstance(store, SqliteEventStore) else None

    async def query_context_index(self, corpus, terms):
        backend = self._context_index_backend()
        if backend is None:
            return None
        return await backend.query_context_index(corpus, terms)

    async def rebuild_context_index(self, corpus):
        backend = self._context_index_backend()
        if backend is None:
            raise ValueError("context-index-unavailable")
        return await backend.rebuild_context_index(corpus)

    async def read_effects(self, session_id: str) -> tuple[EventEnvelope, ...]:
        return await self.store.read(self.effect_stream(session_id))

    async def list_sessions(self) -> tuple[str, ...]:
        streams = await self.store.list_streams(prefix="session:")
        return tuple(stream.removeprefix("session:") for stream in streams)

    async def workspace_for(self, session_id: str) -> Path:
        await self.ensure_session(session_id)
        events = await self.read_session(session_id)
        workspace = events[0].data.get("workspace")
        if not isinstance(workspace, str):
            raise ValueError(f"session {session_id} has no valid workspace")
        return Path(workspace)
