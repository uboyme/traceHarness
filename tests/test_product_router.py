"""v0.7-F2: the router decides between two values, and refuses everything else.

Every case here drives the public parser or the public router boundary. The
lifecycle cases use `Event` gates rather than sleeps, so a passing run means the
window was actually reached rather than merely likely to have been.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import pytest
from product_fixtures import (
    ROUTER_AGENT,
    ROUTER_SESSION,
    ROUTING_SUMMARY,
    Gate,
    ScriptedResponder,
    mode_router,
    profile,
    resolved_router,
)

from traceh.api.product import ResolvedTaskMode, TaskRouting
from traceh.product import (
    MAX_ROUTER_SUMMARY_CHARS,
    ProductInputError,
    ProductModeRouter,
    ProductRoutingError,
    ProductServiceClosedError,
    RouterResponse,
    StrictTaskRoutingParser,
)


def parse(text: str) -> TaskRouting:
    return StrictTaskRoutingParser().parse(text)


def refused(text: str) -> str:
    with pytest.raises(ProductRoutingError) as caught:
        parse(text)
    return caught.value.code


# ------------------------------------------------------------ the two answers


def test_the_two_legal_answers_parse_to_the_two_legal_modes() -> None:
    assert parse('{"mode": "single", "reason": null}') == TaskRouting(
        resolved_mode=ResolvedTaskMode.SINGLE, reason_display=None
    )
    assert parse('{"mode": "multi", "reason": "needs review"}') == TaskRouting(
        resolved_mode=ResolvedTaskMode.MULTI, reason_display="needs review"
    )


def test_surrounding_whitespace_is_the_only_thing_forgiven() -> None:
    assert (
        parse('\n  {"mode": "single", "reason": null}  \n').resolved_mode
        is ResolvedTaskMode.SINGLE
    )


def test_the_prose_never_decides_and_the_enum_always_does() -> None:
    """``reason_display`` is for a person to read. No branch may consult it."""

    routing = parse('{"mode": "single", "reason": "definitely use multi"}')
    assert routing.resolved_mode is ResolvedTaskMode.SINGLE
    assert routing.reason_display == "definitely use multi"


# ------------------------------------------------------------ refused answers


def test_an_unresolved_or_invented_mode_is_refused() -> None:
    """``auto`` is not a member of `ResolvedTaskMode`, so it cannot survive."""

    assert refused('{"mode": "auto", "reason": null}') == "product-router-mode-unknown"
    assert refused('{"mode": "SINGLE", "reason": null}') == "product-router-mode-unknown"
    assert refused('{"mode": "solo", "reason": null}') == "product-router-mode-unknown"
    assert refused('{"mode": true, "reason": null}') == "product-router-mode-invalid"
    assert refused('{"mode": 1, "reason": null}') == "product-router-mode-invalid"


def test_the_key_set_is_exact_in_both_directions() -> None:
    """An answer this build cannot fully account for is not one to act on."""

    assert (
        refused('{"mode": "single", "reason": null, "confidence": 0.9}')
        == "product-router-response-keys-unexpected"
    )
    assert refused('{"mode": "single"}') == "product-router-response-keys-unexpected"
    assert refused('{"reason": null}') == "product-router-response-keys-unexpected"
    assert refused("{}") == "product-router-response-keys-unexpected"


def test_free_text_is_never_mined_for_a_decision() -> None:
    assert refused("single") == "product-router-response-unparsable"
    assert refused("I think single is best.") == "product-router-response-unparsable"
    assert (
        refused('```json\n{"mode": "single", "reason": null}\n```')
        == "product-router-response-unparsable"
    )
    assert (
        refused('Sure! {"mode": "single", "reason": null}')
        == "product-router-response-unparsable"
    )


def test_two_answers_are_not_an_answer() -> None:
    """``json.loads`` refuses trailing content, so a second object fails too."""

    assert (
        refused('{"mode": "single", "reason": null}{"mode": "multi", "reason": null}')
        == "product-router-response-unparsable"
    )
    assert (
        refused('[{"mode": "single", "reason": null}]')
        == "product-router-response-invalid"
    )
    assert refused('"single"') == "product-router-response-invalid"
    assert refused("3") == "product-router-response-invalid"


def test_an_empty_or_non_text_answer_is_refused() -> None:
    assert refused("   \n ") == "product-router-response-empty"
    with pytest.raises(ProductRoutingError) as caught:
        StrictTaskRoutingParser().parse(b'{"mode": "single"}')  # type: ignore[arg-type]
    assert caught.value.code == "product-router-response-invalid"


def test_the_one_display_string_stays_bounded_and_single_line() -> None:
    """Prose that is eventually rendered must not forge a line or run away."""

    assert (
        refused('{"mode": "single", "reason": "%s"}' % ("x" * 300))
        == "product-router-reason-invalid"
    )
    assert (
        refused('{"mode": "single", "reason": "one\\ntwo"}')
        == "product-router-reason-invalid"
    )
    assert refused('{"mode": "single", "reason": 7}') == "product-router-reason-invalid"
    assert refused('{"mode": "single", "reason": ""}') == "product-router-reason-invalid"


# ------------------------------------------------------------ the host bounds


async def test_the_router_returns_the_decision_and_who_produced_it() -> None:
    router = mode_router(
        ScriptedResponder(text='{"mode": "multi", "reason": "needs review"}')
    )
    decision = await router.route(ROUTING_SUMMARY)
    assert decision.routing.resolved_mode is ResolvedTaskMode.MULTI
    assert decision.router_agent_id == ROUTER_AGENT
    assert decision.routing_session_id == ROUTER_SESSION
    await router.aclose()


async def test_an_over_long_answer_fails_before_it_is_parsed() -> None:
    """The bound is the Profile's, and it is measured in bytes, not characters."""

    task_profile = profile()
    bounded = replace(
        task_profile, router=replace(task_profile.router, max_response_bytes=32)
    )
    router = mode_router(
        ScriptedResponder(
            text='{"mode": "single", "reason": "%s"}' % ("padding " * 8)
        ),
        task_profile=bounded,
    )
    with pytest.raises(ProductRoutingError) as caught:
        await router.route(ROUTING_SUMMARY)
    assert caught.value.code == "product-router-response-too-large"
    await router.aclose()


async def test_the_question_this_protocol_will_ask_is_bounded_too() -> None:
    router = mode_router()
    for value, code in (
        ("", "product-router-summary-invalid"),
        ("   ", "product-router-summary-invalid"),
        ("x" * (MAX_ROUTER_SUMMARY_CHARS + 1), "product-router-summary-too-large"),
    ):
        with pytest.raises(ProductRoutingError) as caught:
            await router.route(value)
        assert caught.value.code == code
    with pytest.raises(ProductRoutingError):
        await router.route(None)  # type: ignore[arg-type]
    await router.aclose()


def test_neither_bound_has_a_code_default() -> None:
    """A bound nobody decided is a bound this file would silently invent."""

    task_profile = profile()
    for router_profile in (
        replace(task_profile.router, timeout_milliseconds=0),
        replace(task_profile.router, timeout_milliseconds=None),  # type: ignore[arg-type]
        replace(task_profile.router, max_response_bytes=0),
        replace(task_profile.router, max_response_bytes=True),  # type: ignore[arg-type]
    ):
        with pytest.raises(ProductInputError):
            ProductModeRouter(
                ScriptedResponder(),
                StrictTaskRoutingParser(),
                profile=router_profile,
                assembly=resolved_router(),
            )
    with pytest.raises(ProductInputError):
        ProductModeRouter(
            ScriptedResponder(),
            StrictTaskRoutingParser(),
            profile=task_profile,
            assembly=resolved_router(),
        )


async def test_a_responder_that_answers_for_nobody_is_refused() -> None:
    router = mode_router(ScriptedResponder(router_agent_id=""))
    with pytest.raises(ProductInputError):
        await router.route(ROUTING_SUMMARY)
    await router.aclose()


# ------------------------------------------------------------------ lifecycle


@dataclass(slots=True)
class _Hanging:
    """A router call that never answers unless the test lets it."""

    gate: Gate
    cancelled: bool = False

    async def respond(self, summary: str) -> RouterResponse:
        del summary
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return RouterResponse(
            text='{"mode": "single", "reason": null}',
            router_agent_id=ROUTER_AGENT,
            routing_session_id=ROUTER_SESSION,
        )


class _Stubborn:
    """Work that absorbs cancellation, so releasing early would be visible."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def respond(self, summary: str) -> RouterResponse:
        del summary
        self.started.set()
        while not self.release.is_set():
            try:
                await asyncio.shield(self.release.wait())
            except asyncio.CancelledError:
                continue
        self.finished = True
        return RouterResponse(
            text='{"mode": "single", "reason": null}',
            router_agent_id=ROUTER_AGENT,
            routing_session_id=ROUTER_SESSION,
        )


async def test_a_deadline_converges_the_call_it_gives_up_on() -> None:
    """A timeout that abandoned the responder would leave it still talking."""

    task_profile = profile()
    impatient = replace(
        task_profile, router=replace(task_profile.router, timeout_milliseconds=1)
    )
    responder = _Hanging(Gate())
    router = ProductModeRouter(
        responder,
        StrictTaskRoutingParser(),
        profile=impatient.router,
        assembly=resolved_router(),
    )
    with pytest.raises(ProductRoutingError) as caught:
        await router.route(ROUTING_SUMMARY)
    assert caught.value.code == "product-router-timeout"
    assert responder.cancelled is True
    await router.aclose()


async def test_a_cancelled_caller_gets_its_own_cancellation_back() -> None:
    responder = _Hanging(Gate())
    router = mode_router(responder)  # type: ignore[arg-type]
    caller = asyncio.create_task(router.route(ROUTING_SUMMARY))
    await responder.gate.entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert responder.cancelled is True
    await router.aclose()


async def test_cancelling_again_cannot_release_the_caller_early() -> None:
    responder = _Stubborn()
    router = mode_router(responder)  # type: ignore[arg-type]
    caller = asyncio.create_task(router.route(ROUTING_SUMMARY))
    await responder.started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done(), "the caller was released while work was still running"
    responder.release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert responder.finished is True
    await router.aclose()


async def test_close_refuses_new_questions_and_waits_for_the_one_asked() -> None:
    responder = _Hanging(Gate())
    router = mode_router(responder)  # type: ignore[arg-type]
    caller = asyncio.create_task(router.route(ROUTING_SUMMARY))
    await responder.gate.entered.wait()
    closing = asyncio.create_task(router.aclose())
    await asyncio.sleep(0)
    with pytest.raises(ProductServiceClosedError):
        await router.route(ROUTING_SUMMARY)
    assert not closing.done()
    responder.gate.release.set()
    decision = await caller
    await closing
    assert decision.routing.resolved_mode is ResolvedTaskMode.SINGLE
