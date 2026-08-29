"""Two-stage model admission and provider dispatch boundary.

Admission may freeze a final provider-bound request and reserve host resources, but it
must not call the provider.  The Session owner durably records that admitted request and
its Attempt before :meth:`LlmAdmission.dispatch` can cross the external boundary.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from traceh.api.llm import (
    LlmProvider,
    ModelAttemptIdentity,
    ModelRequest,
    ModelResponse,
)
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory

TextDeltaHandler = Callable[[str], Awaitable[None]]


class LlmAdmissionStateError(RuntimeError):
    """An admitted invocation was dispatched or abandoned more than once."""


class LlmAdmissionBindingError(RuntimeError):
    """An admission does not bind the host-resolved Provider and Attempt."""


class LlmAdmissionAccounting(Protocol):
    """Accounting around a host-owned dispatch without owning the dispatch."""

    async def start(self) -> object:
        ...

    async def finish(
        self,
        started: object,
        *,
        response: ModelResponse | None,
        error: BaseException | None,
    ) -> None:
        ...

    async def abort(self) -> None:
        ...


class LlmAdmission:
    """One admitted, exactly-once provider invocation.

    The object is an ephemeral capability, not a fact source.  Its immutable
    identity/request fields are copied into Session evidence before ``dispatch``;
    the small state guard merely prevents one live caller from reusing the same
    capability twice.
    """

    __slots__ = (
        "_abort_task",
        "_accounting",
        "_attempt",
        "_lock",
        "_provider",
        "_provider_active_milliseconds",
        "_provider_clock",
        "_request",
        "_reservation_id",
        "_state",
    )

    def __init__(
        self,
        provider: LlmProvider,
        request: ModelRequest,
        *,
        attempt: ModelAttemptIdentity,
        provider_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(request) is not ModelRequest or type(attempt) is not ModelAttemptIdentity:
            raise LlmAdmissionBindingError("model-admission-binding-mismatch")
        self._provider = provider
        self._provider_clock = provider_clock
        self._provider_active_milliseconds: int | None = None
        self._request = request
        self._attempt = attempt
        self._reservation_id: str | None = None
        self._accounting: LlmAdmissionAccounting | None = None
        self._state = "admitted"
        self._lock = asyncio.Lock()
        self._abort_task: asyncio.Task[None] | None = None

    @property
    def request(self) -> ModelRequest:
        return self._request

    @property
    def provider(self) -> LlmProvider:
        return self._provider

    @property
    def attempt(self) -> ModelAttemptIdentity:
        return self._attempt

    @property
    def reservation_id(self) -> str | None:
        return self._reservation_id

    @property
    def provider_active_milliseconds(self) -> int | None:
        return self._provider_active_milliseconds

    def _bind_accounting(
        self,
        accounting: LlmAdmissionAccounting,
        *,
        reservation_id: str,
    ) -> None:
        """Attach the host Budget lifecycle before the capability is exposed."""

        if (
            type(self) is not LlmAdmission
            or self._state != "admitted"
            or self._accounting is not None
            or self._reservation_id is not None
            or not isinstance(reservation_id, str)
            or not reservation_id
        ):
            raise LlmAdmissionStateError("model-admission-accounting-not-bindable")
        self._accounting = accounting
        self._reservation_id = reservation_id

    async def dispatch(
        self,
        *,
        provider: LlmProvider,
        request: ModelRequest,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelResponse:
        async with self._lock:
            if self._state != "admitted":
                raise LlmAdmissionStateError("model-admission-not-dispatchable")
            if provider is not self._provider or request != self._request:
                raise LlmAdmissionBindingError("model-admission-binding-mismatch")
            self._state = "dispatched"
        accounting = self._accounting
        started: object | None = None
        if accounting is not None:
            try:
                started = await accounting.start()
            except BaseException as error:
                abort_error: BaseException | None = None
                try:
                    await accounting.abort()
                except BaseException as cleanup_error:
                    abort_error = cleanup_error
                combined = combine_failures(
                    error,
                    abort_error,
                    "model accounting start and abort both failed",
                )
                assert combined is not None
                raise combined from None

        provider_started = self._provider_clock()
        try:
            try:
                response = await provider.complete(request)
            except asyncio.CancelledError:
                raise
            except ProviderFailure as failure:
                if type(failure) is ProviderFailure:
                    raise
                raise ProviderFailure(
                    "provider-failure-unclassified",
                    ProviderFailureCategory.UNKNOWN,
                ) from None
            except Exception:
                # A Provider or plugin that did not implement the typed adapter
                # contract is still prevented from leaking exception text into
                # Session/CLI evidence. Unknown is deliberately non-retryable.
                raise ProviderFailure(
                    "provider-failure-unclassified",
                    ProviderFailureCategory.UNKNOWN,
                ) from None
            provider_finished = self._provider_clock()
            if provider_finished >= provider_started:
                self._provider_active_milliseconds = int(
                    (provider_finished - provider_started) * 1000
                )
            if response.content and on_text_delta is not None:
                await on_text_delta(response.content)
        except BaseException as error:
            provider_finished = self._provider_clock()
            if (
                self._provider_active_milliseconds is None
                and provider_finished >= provider_started
            ):
                self._provider_active_milliseconds = int(
                    (provider_finished - provider_started) * 1000
                )
            if accounting is not None:
                assert started is not None
                await accounting.finish(started, response=None, error=error)
            raise

        if accounting is not None:
            assert started is not None
            await accounting.finish(started, response=response, error=None)
        return response

    async def abort(self) -> None:
        """Release an admission that never obtained durable dispatch permission."""

        async with self._lock:
            if self._state == "dispatched":
                return
            if self._abort_task is None:
                self._state = "aborting"
                self._abort_task = asyncio.create_task(
                    self._abort(), name="traceh-model-admission-abort"
                )
            task = self._abort_task
        cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
            await await_worker_convergence(task)
        if task.cancelled():
            raise cancellation or asyncio.CancelledError()
        failure = task.exception()
        if failure is not None:
            if cancellation is not None:
                raise cancellation from failure
            raise failure
        async with self._lock:
            self._state = "aborted"
        if cancellation is not None:
            raise cancellation

    async def _abort(self) -> None:
        if self._accounting is not None:
            await self._accounting.abort()


class LlmRuntime:
    def __init__(self, *, provider_clock: Callable[[], float] = time.monotonic) -> None:
        self._provider_clock = provider_clock

    async def admit(
        self,
        provider: LlmProvider,
        request: ModelRequest,
        *,
        attempt: ModelAttemptIdentity,
    ) -> LlmAdmission:
        """Freeze one side-effect-free admission for the Session owner."""

        return LlmAdmission(
            provider,
            request,
            attempt=attempt,
            provider_clock=self._provider_clock,
        )


def require_llm_admission_binding(
    admission: object,
    *,
    provider: LlmProvider,
    attempt: ModelAttemptIdentity,
) -> LlmAdmission:
    """Reject extensible or rebound capabilities before durable permission."""

    if (
        type(admission) is not LlmAdmission
        or admission.provider is not provider
        or admission.attempt != attempt
    ):
        raise LlmAdmissionBindingError("model-admission-binding-mismatch")
    return admission


__all__ = [
    "LlmAdmission",
    "LlmAdmissionAccounting",
    "LlmAdmissionBindingError",
    "LlmAdmissionStateError",
    "LlmRuntime",
    "TextDeltaHandler",
    "require_llm_admission_binding",
]
