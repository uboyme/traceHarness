"""The strict mode router: one bounded question, one of two answers.

The router exists for exactly one decision - ``single`` or ``multi`` - and this
module is deliberately hostile to every other thing a model might return. An
unknown mode, an extra key, a missing key, prose, a code fence, two answers, an
over-long body or a body that is not JSON at all are all the same outcome: a
stable :class:`ProductRoutingError` and no durable fact. Nothing is retried and
nothing is inferred from free text, because re-asking turns one bounded decision
into an unbounded loop and guessing lets prose become the decision.

Two seams keep the router from becoming a planner. :class:`RouterResponder` is
handed a bounded summary string and returns a bounded answer string plus the
Agent and Session identity that produced it, so no Supervisor, Workflow,
Workspace, Artifact, Promotion or Registry handle arrives through it.
:class:`StrictTaskRoutingParser` sees only text. Neither can decide how many
Agents run, what the graph looks like, what the Budget is, which Workspace is
used, what gets verified or where anything is promoted - those are fixed by the
topology and the host Profile before the router is ever called.

``reason_display`` is carried because a person should be able to see why a mode
was chosen. It is display-only and no branch in this package reads it; the
decision is the enum beside it, and a reason that contradicts the enum changes
nothing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

from traceh.api.product import (
    ProductRouterProfile,
    ResolvedTaskMode,
    TaskRouting,
)
from traceh.concurrency import await_worker_convergence
from traceh.product.errors import (
    ProductInputError,
    ProductRoutingError,
    ProductServiceClosedError,
)
from traceh.product.events import (
    require_display_text,
    require_hex_digest,
    require_product_identifier,
)
from traceh.product.registry import ResolvedAgentAssembly, router_assembly_digest

MAX_ROUTER_SUMMARY_CHARS = 4096
"""How much task summary the product protocol will hand a router at all.

This is a protocol bound, not host policy. The host's own limits - how long the
router may take and how many bytes it may answer with - come from
:class:`ProductRouterProfile` and have no value here. What this fixes is the
other direction: the largest question this protocol is willing to ask, so a
requirement of unbounded length cannot become an unbounded prompt.
"""

MAX_ROUTER_TIMEOUT_MILLISECONDS = 3_600_000
MAX_ROUTER_RESPONSE_BYTES = 1_048_576
"""Sanity ceilings on the host's own bounds.

A Profile must still state both; these only refuse a value that would make the
bound meaningless. They are not defaults, and omitting the Profile's decision is
a construction error rather than a fall-through to either of these.
"""

ROUTER_RESPONSE_KEYS = frozenset({"mode", "reason"})
"""The complete key set of a router answer.

Exact, in both directions. A missing ``reason`` is as invalid as an extra
``confidence``: an answer this build cannot fully account for is not an answer it
should act on, and silently ignoring unknown keys is how a second, unread channel
of instructions gets established.
"""


@dataclass(frozen=True, slots=True)
class RouterResponse:
    """One bounded router answer and the durable identity that produced it.

    The identities travel with the text because ``product/task-routed`` records
    them: a routing fact that could not name the Agent and Session it was charged
    to would be a decision with no attributable cost.
    """

    text: str
    router_agent_id: str
    routing_session_id: str


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """A parsed routing answer, ready to be recorded by the ProductTask writer."""

    routing: TaskRouting
    router_agent_id: str
    routing_session_id: str


class RouterResponder(Protocol):
    """The host's router Agent, reduced to text in and text out.

    The caller creates the Agent, owns its Session and Budget account and grants
    it no Tool. This seam is what stops any of that authority from arriving at
    the decision boundary: an implementation is handed a string and returns an
    answer plus two identities, and there is no argument through which a
    Supervisor, Workflow, Workspace, Artifact or Promotion handle could reach it.

    That the Agent itself holds no Tool is proven by the resolved
    ``router_assembly_digest`` in :mod:`traceh.product.registry`, not by this
    declaration.
    """

    async def respond(self, summary: str, *, task_id: str) -> RouterResponse:
        ...


def require_router_summary(value: object) -> str:
    """The one question this protocol is willing to ask a router."""

    if type(value) is not str or not value.strip():
        raise ProductRoutingError("product-router-summary-invalid")
    if len(value) > MAX_ROUTER_SUMMARY_CHARS:
        raise ProductRoutingError("product-router-summary-too-large")
    return value


class StrictTaskRoutingParser:
    """Turn one bounded router answer into a resolved mode, or refuse it.

    Implements ``TaskRoutingParser``. It parses an answer; it does not obtain
    one, and it holds nothing but its own configuration.

    The accepted shape is exactly one JSON object with exactly ``mode`` and
    ``reason``. Surrounding whitespace is stripped and nothing else is: a code
    fence, a sentence before the object, a second object after it or a JSON array
    are all refused rather than salvaged. ``json.loads`` already rejects trailing
    content, so "two answers" fails on the same rule as "one answer plus prose".

    ``mode`` must be exactly ``single`` or ``multi``. ``auto`` is not a member of
    :class:`ResolvedTaskMode`, so an unresolved answer cannot survive parsing.
    """

    __slots__ = ()

    def parse(self, response: str) -> TaskRouting:
        if type(response) is not str:
            raise ProductRoutingError("product-router-response-invalid")
        text = response.strip()
        if not text:
            raise ProductRoutingError("product-router-response-empty")
        try:
            decoded = json.loads(text)
        except Exception:
            raise ProductRoutingError("product-router-response-unparsable") from None
        if type(decoded) is not dict:
            raise ProductRoutingError("product-router-response-invalid")
        if frozenset(decoded) != ROUTER_RESPONSE_KEYS:
            raise ProductRoutingError("product-router-response-keys-unexpected")
        mode = decoded["mode"]
        if type(mode) is not str:
            raise ProductRoutingError("product-router-mode-invalid")
        try:
            resolved = ResolvedTaskMode(mode)
        except ValueError:
            raise ProductRoutingError("product-router-mode-unknown") from None
        try:
            reason = require_display_text(decoded["reason"], field="reason-display")
        except ProductInputError:
            raise ProductRoutingError("product-router-reason-invalid") from None
        return TaskRouting(resolved_mode=resolved, reason_display=reason)


class ProductModeRouter:
    """The host boundary that asks a router once, within its declared bounds.

    Every bound comes from an explicit :class:`ProductRouterProfile`. There is no
    code default for the timeout or the response size, because a bound nobody
    decided is a bound that silently becomes whatever this file happened to say.

    Construction also binds the resolved Router assembly digest. The product
    assembler compares both this Profile and that digest against its fresh
    preflight before the first call, so the live responder cannot be a separately
    configured Router hidden behind a paper-only receipt.

    The response task is owned here so a deadline is a *convergence* and not an
    abandonment: on timeout the responder is cancelled and waited for before the
    failure is raised, so a router that was mid-call cannot still be talking to a
    provider after ``route()`` returned. A cancelled caller gets the same
    treatment and then its own ``CancelledError`` back; cancelling again cannot
    release it early.
    """

    __slots__ = (
        "_closed",
        "_lock",
        "_max_response_bytes",
        "_parser",
        "_pending",
        "_profile",
        "_responder",
        "_router_assembly_digest",
        "_timeout_seconds",
    )

    def __init__(
        self,
        responder: RouterResponder,
        parser: StrictTaskRoutingParser,
        *,
        profile: ProductRouterProfile,
        assembly: ResolvedAgentAssembly,
    ) -> None:
        if type(profile) is not ProductRouterProfile:
            raise ProductInputError("product-router-profile-invalid", "profile")
        require_product_identifier(profile.preset, field="router_preset")
        timeout = _require_bounded_int(
            profile.timeout_milliseconds,
            maximum=MAX_ROUTER_TIMEOUT_MILLISECONDS,
            field="router_timeout",
        )
        self._max_response_bytes = _require_bounded_int(
            profile.max_response_bytes,
            maximum=MAX_ROUTER_RESPONSE_BYTES,
            field="router_response_bytes",
        )
        self._profile = profile
        self._router_assembly_digest = router_assembly_digest(assembly)
        self._timeout_seconds = timeout / 1000
        self._responder = responder
        self._parser = parser
        self._lock = asyncio.Lock()
        self._pending: set[asyncio.Task[RouterResponse]] = set()
        self._closed = False

    def binds(
        self,
        profile: ProductRouterProfile,
        router_assembly_digest: str,
    ) -> bool:
        """Whether this live router is the exact assembly preflight resolved.

        The router is injected separately from the Profile Registry, so its
        timeout/response envelope and resolved Agent composition must be bound
        explicitly. Otherwise preflight could describe one no-tool Router while
        :meth:`route` invokes another object with different bounds or authority.
        """

        if type(profile) is not ProductRouterProfile:
            return False
        try:
            digest = require_hex_digest(
                router_assembly_digest,
                lengths=(64,),
                field="router-assembly-digest",
            )
        except ProductInputError:
            return False
        return self._profile == profile and self._router_assembly_digest == digest

    async def route(self, summary: str, *, task_id: str) -> RouterDecision:
        """Ask once, inside the Profile's bounds, and refuse anything else."""

        text = require_router_summary(summary)
        task_id = require_product_identifier(task_id, field="task_id")
        async with self._lock:
            if self._closed:
                raise ProductServiceClosedError
            task = asyncio.create_task(
                self._responder.respond(text, task_id=task_id),
                name="traceh-product-route",
            )
            self._pending.add(task)
        try:
            response = await _converge_router(
                task, timeout_seconds=self._timeout_seconds
            )
        finally:
            async with self._lock:
                self._pending.discard(task)
        return RouterDecision(
            routing=self._parser.parse(self._bounded_text(response)),
            router_agent_id=require_product_identifier(
                response.router_agent_id, field="router_agent_id"
            ),
            routing_session_id=require_product_identifier(
                response.routing_session_id, field="routing_session_id"
            ),
        )

    def _bounded_text(self, response: object) -> str:
        if type(response) is not RouterResponse:
            raise ProductRoutingError("product-router-response-invalid")
        text = response.text
        if type(text) is not str:
            raise ProductRoutingError("product-router-response-invalid")
        try:
            encoded = text.encode("utf-8")
        except Exception:
            raise ProductRoutingError("product-router-response-invalid") from None
        if len(encoded) > self._max_response_bytes:
            raise ProductRoutingError("product-router-response-too-large")
        return text

    async def aclose(self) -> None:
        """Refuse new questions and wait for the one already asked.

        Waiting rather than cancelling is deliberate: the in-flight call belongs
        to a ``route()`` caller that is still awaiting it, and cutting it short
        here would hand that caller a cancellation nobody requested.
        """

        async with self._lock:
            self._closed = True
            pending = tuple(self._pending)
        for task in pending:
            await await_worker_convergence(task)


async def _converge_router(
    task: asyncio.Task[RouterResponse], *, timeout_seconds: float
) -> RouterResponse:
    """Wait for the owned response, converging it on timeout or cancellation."""

    cancellation: asyncio.CancelledError | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.shield(task)
    except TimeoutError:
        task.cancel()
        await await_worker_convergence(task)
        raise ProductRoutingError("product-router-timeout") from None
    except asyncio.CancelledError as error:
        cancellation = error
        task.cancel()
        await await_worker_convergence(task)
    if task.cancelled():
        raise cancellation
    failure = task.exception()
    if failure is not None:
        raise cancellation from failure
    raise cancellation


def _require_bounded_int(value: object, *, maximum: int, field: str) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise ProductInputError(f"product-{field.replace('_', '-')}-invalid", field)
    return value


__all__ = [
    "MAX_ROUTER_RESPONSE_BYTES",
    "MAX_ROUTER_SUMMARY_CHARS",
    "MAX_ROUTER_TIMEOUT_MILLISECONDS",
    "ROUTER_RESPONSE_KEYS",
    "ProductModeRouter",
    "RouterDecision",
    "RouterResponder",
    "RouterResponse",
    "StrictTaskRoutingParser",
    "require_router_summary",
]
