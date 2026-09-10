"""Host-owned admission and convergence around the original finite AO experiment.

Only scheduling facts are written here. Evaluation owns grades and actual usage;
Session owns conversation evidence. Reservations are conservative and not refunded.
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


@dataclass(frozen=True)
class BackgroundPeriod:
    period_id: str
    workspace: str
    baseline_digest: str
    experiment_settings_digest: str
    expires_at: datetime
    max_episodes: int
    max_trials: int
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
                    self.max_trials,
                    self.max_control_tokens,
                    self.max_observations,
                    self.cooldown_seconds,
                )
            )
        ):
            raise ValueError("background-period-invalid")


@dataclass(frozen=True)
class EpisodeReservation:
    trials: int
    control_tokens: int

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.trials, self.control_tokens)):
            raise ValueError("background-reservation-invalid")


@dataclass(frozen=True)
class EpisodeSettlement:
    """Derived by the original experiment inspector, never supplied by a model."""

    evidence_path: str
    candidate_digest: str | None
    await_review: bool
    usage_known: bool
    converged: bool


def project_background(events):
    """Discardable view of scheduling facts. No task or evaluation projection."""
    state = {
        "seq": 0,
        "period": None,
        "enabled": False,
        "active": None,
        "episodes": 0,
        "trials": 0,
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
        if event.schema_version != 1 or not event.type.startswith("optimization-background/"):
            raise ValueError("background-protocol-unsupported")
        data, kind = event.data, event.type.split("/", 1)[1]
        if kind == "period-opened":
            if state["active"] or state["pending_review"] or state["blocked"]:
                raise ValueError("background-period-unsettled")
            state["consumed"].update(state["observations"])
            state.update(
                period=data, enabled=False, episodes=0, trials=0, control_tokens=0, observations={}
            )
        elif kind == "enabled":
            state["enabled"] = data["enabled"]
        elif kind == "observed":
            state["observations"][data["identity"]] = data["observation"]
        elif kind == "admitted":
            if state["active"]:
                raise ValueError("background-overlapping-episodes")
            state["active"] = data["episode_id"]
            state["episodes"] += 1
            state["trials"] += data["reservation"]["trials"]
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
            if data["await_review"]:
                state["pending_review"] = data["episode_id"]
        elif kind == "review-dismissed":
            if state["pending_review"] != data["episode_id"]:
                raise ValueError("background-review-owner-mismatch")
            state["pending_review"] = None
        else:
            raise ValueError("background-protocol-unsupported")
        state["seq"] = event.seq
    return state


class BackgroundOptimizationHost:
    """One application-lifetime worker; durable CAS admission works across hosts."""

    def __init__(self, sessions, period, *, reservation, execute, clock=None):
        self.sessions, self.period = sessions, period
        self.reservation, self.execute = reservation, execute
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stream = "optimization-background:" + fingerprint(period.workspace)
        self._worker = None
        self._started = None
        self._cancel_requested = None
        self._closed = False
        self._foreground = False
        self._lock = asyncio.Lock()

    async def view(self):
        return project_background(await self.sessions.store.read(self.stream))

    async def _append(self, view, kind, data):
        await self.sessions.store.append(
            self.stream,
            expected_seq=view["seq"],
            events=(PendingEvent("optimization-background/" + kind, to_json_value(data)),),
        )

    async def open(self):
        state = await self.view()
        definition = to_json_value(self.period)
        if state["period"] is None:
            try:
                await self._append(state, "period-opened", definition)
            except ConcurrencyConflict:
                pass
        state = await self.view()
        if any(
            state["period"][k] != definition[k]
            for k in definition
            if k not in {"period_id", "expires_at"}
        ):
            raise ValueError("background-period-settings-mismatch")
        self.period = BackgroundPeriod(
            **{
                **state["period"],
                "expires_at": datetime.fromisoformat(state["period"]["expires_at"]),
            }
        )
        return state

    async def renew(self, expires_at):
        """Explicit human renewal; no timer, restart or candidate can refill quotas."""
        if self._closed or expires_at.tzinfo is not UTC or expires_at <= self.clock():
            raise ValueError("background-renewal-invalid")
        async with self._lock:
            state = await self.open()
            if state["active"] or state["pending_review"] or state["blocked"]:
                raise ValueError("background-period-unsettled")
            period = replace(self.period, period_id=str(uuid4()), expires_at=expires_at)
            await self._append(state, "period-opened", to_json_value(period))
            self.period = period

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

    async def observe(
        self, session_id, turn_id, *, feedback, failure_class="user-feedback-unverified"
    ):
        """Explicit feedback only; full transcript stays in the original Session."""
        if (
            self._closed
            or type(feedback) is not str
            or not feedback.strip()
            or len(feedback) > 4000
        ):
            raise ValueError("background-feedback-invalid")
        if failure_class not in {"user-feedback-unverified", "runtime-signal-unverified"}:
            raise ValueError("background-feedback-kind-invalid")
        workspace = await self.sessions.workspace_for(session_id)
        if str(workspace.resolve()) != self.period.workspace:
            raise ValueError("background-observation-outside-scope")
        events = await self.sessions.read_session(session_id)
        ended = next(
            (
                e
                for e in reversed(events)
                if e.type == "turn/end" and e.data.get("turn_id") == turn_id
            ),
            None,
        )
        if ended is None:
            raise ValueError("background-turn-not-completed")
        evidence = tuple(e for e in events if e.seq <= ended.seq)
        observation = RuntimeObservation(
            session_id,
            turn_id,
            ended.seq,
            fingerprint(evidence),
            failure_class,
            feedback.strip(),
            (f"session:{session_id}@{ended.seq}",),
        )
        identity = fingerprint((session_id, turn_id, ended.seq, failure_class))
        async with self._lock:
            state = await self.open()
            if not state["enabled"]:
                raise ValueError("background-observation-not-enabled")
            if identity in state["observations"] or identity in state["consumed"]:
                return False
            if len(state["observations"]) >= self.period.max_observations:
                raise ValueError("background-observation-limit")
            await self._append(
                state,
                "observed",
                {
                    "identity": identity,
                    "observation": to_json_value(observation),
                },
            )
        await self.kick()
        return True

    async def observe_completed_turn(self, session_id, turn_id):
        """Cheap structural signal, not an answer judgment or transcript upload."""
        if self._closed or not (await self.open())["enabled"]:
            return False
        events = await self.sessions.read_session(session_id)
        results = [
            e for e in events if e.type == "tool/result" and e.data.get("turn_id") == turn_id
        ]
        denied = sum(e.data.get("status") == "denied" for e in results)
        failed = sum(e.data.get("status") == "failed" for e in results)
        if not denied and not failed:
            return False
        return await self.observe(
            session_id,
            turn_id,
            failure_class="runtime-signal-unverified",
            feedback=f"This completed Turn has {denied} denied and {failed} failed tool results. "
            "This is only a structural signal, not proof of an incorrect answer. Denials may be "
            "expected. Propose nothing unless the available evidence supports "
            "a general improvement.",
        )

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
            pending = {k: v for k, v in state["observations"].items() if k not in state["consumed"]}
            if (
                not state["enabled"]
                or not pending
                or state["active"]
                or state["pending_review"]
                or state["blocked"]
                or now >= self.period.expires_at
                or (
                    state["cooldown_until"]
                    and now < datetime.fromisoformat(state["cooldown_until"])
                )
                or state["episodes"] >= self.period.max_episodes
                or state["trials"] + self.reservation.trials > self.period.max_trials
                or state["control_tokens"] + self.reservation.control_tokens
                > self.period.max_control_tokens
            ):
                return False
            episode_id = str(uuid4())
            try:
                await self._append(
                    state,
                    "admitted",
                    {
                        "episode_id": episode_id,
                        "observations": tuple(pending),
                        "reservation": to_json_value(self.reservation),
                    },
                )
            except ConcurrencyConflict:
                return False
            # No await between successful admission and owning the task.
            self._started = asyncio.Event()
            self._worker = asyncio.create_task(
                self._run(
                    episode_id,
                    tuple(observation_from_dict(v) for v in pending.values()),
                    tuple(sorted(state["candidates"])),
                ),
                name="traceh-background-optimization",
            )
            return True

    async def _run(self, episode_id, observations, seen):
        self._started.set()
        result, blocked = None, None
        try:
            result = await self.execute(episode_id, observations, seen, self.period.expires_at)
            if not isinstance(result, EpisodeSettlement):
                raise ValueError("background-settlement-invalid")
            if not result.converged or not result.usage_known:
                blocked = "experiment-convergence-or-usage-unproven"
        except BaseException as error:
            # A partial call may cost money; do not refund or manufacture exact usage.
            blocked = (
                "cancelled-unsettled"
                if isinstance(error, asyncio.CancelledError)
                else "experiment-failed"
            )

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
                            "await_review": result.await_review if result else False,
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

    async def dismiss(self, episode_id):
        """Human declines this candidate; never installs or approves a patch."""
        async with self._lock:
            state = await self.open()
            if state["pending_review"] != episode_id:
                raise ValueError("background-review-owner-mismatch")
            await self._append(state, "review-dismissed", {"episode_id": episode_id})

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
            await asyncio.shield(worker)
        return await self.view()

    async def foreground(self, active):
        async with self._lock:
            self._foreground = active
        if active:
            await self.pause_work()

    async def aclose(self):
        self._closed = True
        await self.pause_work()
