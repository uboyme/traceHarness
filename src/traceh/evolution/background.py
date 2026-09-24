"""Host-owned detection and bounded suggestion around the original AO proposal path.

Only scheduling facts are written here (ADR-0082). Sessions own conversation
evidence, the evaluation run owns its evidence, and the proposal directory owns
the analysis call and its actual usage. The host never evaluates a suggestion:
whether one is worth verifying, and on what, is the user's decision through the
ordinary evaluation command. Reservations are conservative and not refunded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from traceh.api.events import PendingEvent
from traceh.api.json_types import fingerprint, to_json_value
from traceh.api.optimization import RuntimeObservation, observation_from_dict
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict

BACKGROUND_SCHEMA_VERSION = 2
"""Schema 1 (in-background evaluation) is refused; it requires a new data directory."""

MIN_SOURCES = 2
"""Distinct sources a mechanism must appear in before one suggestion is admitted."""

USER_FEEDBACK = "user-feedback-unverified"


@dataclass(frozen=True)
class BackgroundPeriod:
    period_id: str
    workspace: str
    baseline_digest: str
    experiment_settings_digest: str
    expires_at: datetime
    max_episodes: int
    max_control_tokens: int
    max_observations: int
    cooldown_seconds: int

    def __post_init__(self):
        if (
            not self.period_id
            or not self.baseline_digest
            or not self.experiment_settings_digest
            or str(Path(self.workspace).resolve()) != self.workspace
            or self.expires_at.tzinfo is not UTC
            or any(
                type(n) is not int or n < 1
                for n in (
                    self.max_episodes,
                    self.max_control_tokens,
                    self.max_observations,
                    self.cooldown_seconds,
                )
            )
        ):
            raise ValueError("background-period-invalid")

    def definition(self):
        """What must match for the durable period to still describe this host."""
        return {
            k: v for k, v in to_json_value(self).items() if k not in {"period_id", "expires_at"}
        }


@dataclass(frozen=True)
class EpisodeReservation:
    control_tokens: int

    def __post_init__(self):
        if type(self.control_tokens) is not int or self.control_tokens < 1:
            raise ValueError("background-reservation-invalid")


@dataclass(frozen=True)
class EpisodeSettlement:
    """Derived by the original proposal inspector, never supplied by a model."""

    evidence_path: str
    candidate_digest: str | None
    usage_known: bool
    converged: bool


@dataclass(frozen=True)
class Finding:
    """One observation and the source it came from; the source decides clustering."""

    source: str
    observation: object

    @property
    def identity(self):
        return fingerprint((self.source, self.observation.failure_class))


def project_background(events):
    """Discardable view of scheduling facts. No task, evaluation or proposal projection."""
    state = {
        "seq": 0,
        "period": None,
        "enabled": False,
        "active": None,
        "episodes": 0,
        "control_tokens": 0,
        "observations": {},
        "consumed": set(),
        "candidates": set(),
        "pending_review": None,
        "blocked": None,
        "cooldown_until": None,
        "last_evidence": None,
    }
    for event in events:
        if event.schema_version != BACKGROUND_SCHEMA_VERSION or not event.type.startswith(
            "optimization-background/"
        ):
            raise ValueError("background-protocol-unsupported")
        data, kind = event.data, event.type.split("/", 1)[1]
        if kind == "period-opened":
            if state["active"] or state["pending_review"] or state["blocked"]:
                raise ValueError("background-period-unsettled")
            state["consumed"].update(state["observations"])
            state.update(period=data, enabled=False, episodes=0, control_tokens=0, observations={})
        elif kind == "enabled":
            state["enabled"] = data["enabled"]
        elif kind == "observed":
            state["observations"][data["identity"]] = {
                "source": data["source"],
                "observation": data["observation"],
            }
        elif kind == "admitted":
            if state["active"]:
                raise ValueError("background-overlapping-episodes")
            state["active"] = data["episode_id"]
            state["episodes"] += 1
            state["control_tokens"] += data["reservation"]["control_tokens"]
            state["consumed"].update(data["observations"])
        elif kind == "settled":
            if state["active"] != data["episode_id"]:
                raise ValueError("background-settlement-owner-mismatch")
            state["active"] = None
            state["cooldown_until"] = data["cooldown_until"]
            state["last_evidence"] = data["evidence_path"]
            state["blocked"] = data["blocked"]
            candidate = data["candidate_digest"]
            if candidate:
                state["candidates"].add(candidate)
                if not data["blocked"]:
                    state["pending_review"] = data["episode_id"]
        elif kind == "review-dismissed":
            if state["pending_review"] != data["episode_id"]:
                raise ValueError("background-review-owner-mismatch")
            state["pending_review"] = None
        elif kind == "blocked-acknowledged":
            if not state["blocked"] or state["active"]:
                raise ValueError("background-acknowledgement-invalid")
            state["blocked"] = None
        else:
            raise ValueError("background-protocol-unsupported")
        state["seq"] = event.seq
    return state


def pending_clusters(state):
    """Unconsumed observations grouped by mechanism, with their distinct sources."""
    clusters = {}
    for identity, item in state["observations"].items():
        if identity in state["consumed"]:
            continue
        entry = clusters.setdefault(
            item["observation"]["failure_class"], {"identities": [], "sources": set()}
        )
        entry["identities"].append(identity)
        entry["sources"].add(item["source"])
    return clusters


class BackgroundOptimizationHost:
    """One application-lifetime worker; durable CAS admission works across hosts."""

    def __init__(self, sessions, period, *, reservation, execute, clock=None, scope=None):
        self.sessions, self.period = sessions, period
        self.configured = period
        self.reservation, self.execute = reservation, execute
        self.scope = scope
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stream = "optimization-background:" + fingerprint(period.workspace)
        self.stale = False
        self._worker = None
        self._started = None
        self._cancel_requested = None
        self._closed = False
        self._foreground = False
        self._lock = asyncio.Lock()

    async def view(self):
        state = project_background(await self.sessions.store.read(self.stream))
        state["stale"] = self.stale
        return state

    async def _append(self, view, kind, data):
        await self._append_many(view, ((kind, data),))

    async def _append_many(self, view, items):
        await self.sessions.store.append(
            self.stream,
            expected_seq=view["seq"],
            events=tuple(
                PendingEvent(
                    "optimization-background/" + kind,
                    to_json_value(data),
                    schema_version=BACKGROUND_SCHEMA_VERSION,
                )
                for kind, data in items
            ),
        )

    async def open(self):
        state = await self.view()
        if state["period"] is None:
            try:
                await self._append(state, "period-opened", self.configured)
            except ConcurrencyConflict:
                pass
            state = await self.view()
        durable = BackgroundPeriod(
            **{
                **state["period"],
                "expires_at": datetime.fromisoformat(state["period"]["expires_at"]),
            }
        )
        # Changed source or settings do not break the stream: the durable period
        # keeps its quotas and de-duplication, and no new work is admitted until a
        # person explicitly approves a period bound to the current definition.
        self.stale = durable.definition() != self.configured.definition()
        self.period = durable
        state["stale"] = self.stale
        return state

    async def renew(self, expires_at):
        """Explicit human approval of a new period for the current source and settings."""
        if self._closed or expires_at.tzinfo is not UTC or expires_at <= self.clock():
            raise ValueError("background-renewal-invalid")
        async with self._lock:
            state = await self.open()
            if state["active"] or state["pending_review"] or state["blocked"]:
                raise ValueError("background-period-unsettled")
            period = replace(self.configured, period_id=str(uuid4()), expires_at=expires_at)
            await self._append(state, "period-opened", period)
            self.period, self.stale = period, False

    async def acknowledge_blocked(self, note):
        """A person has checked the unproven cost or convergence; quotas stay spent."""
        if self._closed or type(note) is not str or not note.strip() or len(note) > 2000:
            raise ValueError("background-acknowledgement-invalid")
        async with self._lock:
            state = await self.open()
            if not state["blocked"] or state["active"]:
                raise ValueError("background-acknowledgement-invalid")
            await self._append(
                state,
                "blocked-acknowledged",
                {"reason": state["blocked"], "evidence": state["last_evidence"], "note": note},
            )
        await self.kick()

    async def set_enabled(self, enabled):
        if type(enabled) is not bool or self._closed:
            raise ValueError("background-host-closed-or-invalid")
        async with self._lock:
            state = await self.open()
            if state["active"] and (self._worker is None or self._worker.done()):
                raise ValueError("background-episode-owned-by-other-host-or-unsettled")
            await self._append(state, "enabled", {"enabled": enabled})
        if not enabled:
            await self.pause_work()
        else:
            await self.kick()

    async def _completed_turn(self, session_id, turn_id):
        workspace = await self.sessions.workspace_for(session_id)
        if str(workspace.resolve()) != self.period.workspace:
            raise ValueError("background-observation-outside-scope")
        from traceh.evolution.detection import turn_bounds

        events = await self.sessions.read_session(session_id)
        bounds = turn_bounds(events, turn_id)
        if bounds is None:
            raise ValueError("background-turn-not-completed")
        return events, bounds

    async def observe(self, session_id, turn_id, *, feedback):
        """Explicit feedback only; full transcript stays in the original Session."""
        if (
            self._closed
            or type(feedback) is not str
            or not feedback.strip()
            or len(feedback) > 4000
        ):
            raise ValueError("background-feedback-invalid")
        events, (_, end) = await self._completed_turn(session_id, turn_id)
        observation = RuntimeObservation(
            session_id,
            turn_id,
            end,
            fingerprint(tuple(e for e in events if e.seq <= end)),
            USER_FEEDBACK,
            feedback.strip(),
            (f"session:{session_id}@{end}",),
        )
        return await self._accept((Finding(f"chat:{session_id}/{turn_id}", observation),))

    async def observe_completed_turn(self, session_id, turn_id):
        """Mechanisms the finished Turn's own events show; no answer judgment."""
        if self._closed or not (await self.open())["enabled"]:
            return False
        from traceh.evolution.detection import detect, runtime_observation

        events, bounds = await self._completed_turn(session_id, turn_id)
        stream = self.sessions.session_stream(session_id)
        findings = tuple(
            Finding(
                f"chat:{session_id}/{turn_id}",
                runtime_observation(session_id, turn_id, events, detection),
            )
            for detection in detect(session_id, events, stream_id=stream, within=bounds)
        )
        return await self._accept(findings) if findings else False

    async def observe_product(self, reader, task_id):
        """Follow the original Product reader, scoped through its confirming Chat."""
        if self._closed or not (await self.open())["enabled"]:
            return False
        from traceh.evolution.product_feedback import product_findings

        findings = await product_findings(self.sessions, self.period, reader, task_id)
        return await self._accept(findings) if findings else False

    async def observe_evaluation(self, run_root):
        """Findings in a finished evaluation run of this period's own benchmark."""
        if self._closed or self.scope is None or not (await self.open())["enabled"]:
            raise ValueError("background-observation-not-enabled")
        from traceh.evaluation.evidence import load_run
        from traceh.evolution.detection import evaluation_findings

        frozen = (await asyncio.to_thread(load_run, Path(run_root)))[0]
        if frozen["benchmark"]["sha256"] != self.scope["benchmark_digest"]:
            raise ValueError("background-observation-outside-scope")
        findings = tuple(
            Finding(item.source, item.observation)
            for item in await asyncio.to_thread(evaluation_findings, Path(run_root))
            if item.observation.case_id in self.scope["case_ids"]
        )
        return await self._accept(findings) if findings else False

    async def _accept(self, findings):
        async with self._lock:
            state = await self.open()
            if not state["enabled"]:
                raise ValueError("background-observation-not-enabled")
            fresh = {}
            for finding in findings:
                identity = finding.identity
                if identity not in state["observations"] and identity not in state["consumed"]:
                    fresh.setdefault(identity, finding)
            if not fresh:
                return False
            if len(state["observations"]) + len(fresh) > self.period.max_observations:
                raise ValueError("background-observation-limit")
            await self._append_many(
                state,
                tuple(
                    (
                        "observed",
                        {
                            "identity": identity,
                            "source": finding.source,
                            "observation": to_json_value(finding.observation),
                        },
                    )
                    for identity, finding in fresh.items()
                ),
            )
        await self.kick()
        return True

    async def kick(self):
        async with self._lock:
            if (
                self._closed
                or self._foreground
                or (self._worker is not None and not self._worker.done())
            ):
                return False
            state = await self.open()
            now = self.clock()
            ready = sorted(
                (
                    (-len(entry["sources"]), failure_class, entry)
                    for failure_class, entry in pending_clusters(state).items()
                    if len(entry["sources"]) >= MIN_SOURCES
                ),
                key=lambda item: item[:2],
            )
            if (
                not ready
                or self.stale
                or not state["enabled"]
                or state["active"]
                or state["pending_review"]
                or state["blocked"]
                or now >= self.period.expires_at
                or (
                    state["cooldown_until"]
                    and now < datetime.fromisoformat(state["cooldown_until"])
                )
                or state["episodes"] >= self.period.max_episodes
                or state["control_tokens"] + self.reservation.control_tokens
                > self.period.max_control_tokens
            ):
                return False
            _, failure_class, entry = ready[0]
            identities = tuple(sorted(entry["identities"]))
            episode_id = str(uuid4())
            try:
                await self._append(
                    state,
                    "admitted",
                    {
                        "episode_id": episode_id,
                        "failure_class": failure_class,
                        "observations": identities,
                        "reservation": to_json_value(self.reservation),
                    },
                )
            except ConcurrencyConflict:
                return False
            observations = tuple(
                observation_from_dict(state["observations"][i]["observation"]) for i in identities
            )
            # No await between successful admission and owning the task.
            self._started = asyncio.Event()
            self._worker = asyncio.create_task(
                self._run(episode_id, observations, tuple(sorted(state["candidates"]))),
                name="traceh-background-suggestion",
            )
            return True

    async def _run(self, episode_id, observations, seen):
        self._started.set()
        result, blocked, cancelled = None, None, None
        try:
            result = await self.execute(episode_id, observations, seen, self.period.expires_at)
            if not isinstance(result, EpisodeSettlement):
                raise ValueError("background-settlement-invalid")
            if not result.converged or not result.usage_known:
                blocked = "suggestion-convergence-or-usage-unproven"
        except asyncio.CancelledError as error:
            # The original owner has converged before this propagates. A partial
            # call may cost money: never refund or manufacture exact usage.
            blocked, cancelled = "cancelled-unsettled", error
        except Exception:
            blocked = "suggestion-failed"

        async def settle():
            while True:
                state = await self.view()
                try:
                    await self._append(
                        state,
                        "settled",
                        {
                            "episode_id": episode_id,
                            "blocked": blocked,
                            "evidence_path": result.evidence_path if result else None,
                            "candidate_digest": result.candidate_digest if result else None,
                            "cooldown_until": (
                                self.clock() + timedelta(seconds=self.period.cooldown_seconds)
                            ).isoformat(),
                        },
                    )
                    return
                except ConcurrencyConflict:
                    continue

        closing = asyncio.create_task(settle())
        await await_worker_convergence(closing)
        if not closing.cancelled() and closing.exception() is not None:
            raise closing.exception()
        if cancelled is not None:
            raise cancelled

    async def dismiss(self, episode_id):
        """Human declines this suggestion; never installs or approves a patch."""
        async with self._lock:
            state = await self.open()
            if state["pending_review"] != episode_id:
                raise ValueError("background-review-owner-mismatch")
            await self._append(state, "review-dismissed", {"episode_id": episode_id})
        await self.kick()

    async def pause_work(self):
        worker = self._worker
        if worker is not None:
            if not worker.done() and self._started is not None and not self._started.is_set():
                # Cancelling a Task before its coroutine starts would bypass its
                # settlement altogether and leave a durable admitted episode orphaned.
                await await_worker_convergence(asyncio.create_task(self._started.wait()))
            if not worker.done() and self._cancel_requested is not worker:
                self._cancel_requested = worker
                worker.cancel()
            await await_worker_convergence(worker)
            if not worker.cancelled() and worker.exception() is not None:
                raise worker.exception()

    async def wait_idle(self):
        """Wait for this host's admitted episode without cancelling it."""
        worker = self._worker
        if worker is not None:
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if not worker.cancelled():
                    raise
        return await self.view()

    async def foreground(self, active):
        """Foreground work always wins new admissions.

        A suggestion already running is one bounded analysis call in its own plugin
        Runtime, sharing no workspace or Session with the foreground. Cancelling it
        would leave its usage unknown and block the host for a human check, so it
        is allowed to finish; closing the application still cancels and converges it.
        """
        async with self._lock:
            self._foreground = active

    async def aclose(self):
        self._closed = True
        await self.pause_work()
