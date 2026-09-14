"""One explicit, model-bound estimate of complete request pressure.

This is not provider billing or a claim that a local encoding matches a remote
chat template. Actual usage stays in model/attempt-end. No request is dispatched
or source text persisted by this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.metadata import version

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelRequest

TOKEN_MEASUREMENT = "request/token-measurement"


@dataclass(frozen=True, slots=True)
class TokenBudgetPolicy:
    encoding: str
    window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    trigger_percent: int = 80

    def __post_init__(self):
        if not isinstance(self.encoding, str) or not self.encoding.strip():
            raise ValueError("token-encoding-required")
        for name in ("window_tokens", "output_reserve_tokens", "safety_margin_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "safety_margin_tokens" else 1):
                raise ValueError("token-budget-integer-invalid")
        if self.output_reserve_tokens + self.safety_margin_tokens >= self.window_tokens:
            raise ValueError("token-budget-no-input-space")
        if type(self.trigger_percent) is not int or not 1 <= self.trigger_percent <= 100:
            raise ValueError("token-trigger-percent-invalid")

    @property
    def input_limit(self):
        return self.window_tokens - self.output_reserve_tokens - self.safety_margin_tokens

    def to_dict(self):
        return asdict(self)


class RequestTokenBudgetExceeded(ValueError):
    """An audited local estimate exceeded the configured input allowance."""

    code = "request-token-budget-exceeded"

    def __init__(self, *, session_id: str, turn_id: str, step_id: str):
        super().__init__(self.code)
        self.session_id = session_id
        self.turn_id = turn_id
        self.step_id = step_id


class CanonicalTokenCounter:
    """The one canonical token count, usable without an input-window policy.

    Budget reservations need to count a request; the input measurement needs to
    count the same parts under a window policy. Both go through this object so a
    host can never hold two token counts that disagree. It states no policy of
    its own: a reservation's safety margin belongs to the Budget host, and the
    input allowance belongs to `TokenBudgetPolicy`.
    """

    __slots__ = ("_encoding", "identity")

    def __init__(self, encoding: str) -> None:
        if not isinstance(encoding, str) or not encoding.strip():
            raise ValueError("token-encoding-required")
        try:
            import tiktoken
        except ImportError:
            raise ValueError("token-meter-unavailable: install traceharness-py[tokens]") from None
        try:
            self._encoding = tiktoken.get_encoding(encoding)
        except Exception:
            raise ValueError("token-encoding-unavailable") from None
        self.identity = {
            "method": "canonical-parts-bpe-v1",
            "encoding": encoding,
            "library_version": version("tiktoken"),
            "quality": "estimated",
        }

    def count(self, value) -> int:
        # Special-looking source strings are ordinary untrusted text.
        return len(self._encoding.encode(canonical_json(value), disallowed_special=()))

    def count_request(self, request: ModelRequest) -> int:
        """Satisfy the Budget host's `TokenCounter` protocol."""

        return self.count(request.to_dict())


class RequestTokenMeter:
    def __init__(self, policy: TokenBudgetPolicy, *, provider=None, model=None):
        self._counter = CanonicalTokenCounter(policy.encoding)
        self.policy = policy
        self._binding = (provider, model)
        self.identity = self._counter.identity

    def _count(self, value):
        return self._counter.count(value)

    def count_message(self, message):
        """Count one actual message using the same canonical part as full requests."""
        return self._count(message.to_dict())

    def count_request(self, request: ModelRequest) -> int:
        """Satisfy `TokenCounter` using the same counting primitive as `measure`."""

        return self._counter.count_request(request)

    def measure(self, request: ModelRequest, *, product_messages=0, context_messages=1):
        if self._binding != (None, None) and self._binding != (request.provider, request.model):
            raise ValueError("token-meter-model-binding-changed")
        if request.max_output_tokens is None:
            raise ValueError("token-meter-output-limit-required")
        if request.max_output_tokens > self.policy.output_reserve_tokens:
            raise ValueError("token-meter-output-reserve-mismatch")
        messages = [message.to_dict() for message in request.messages]
        end = len(messages) - context_messages
        if not 0 <= product_messages <= end or context_messages not in (0, 1):
            raise ValueError("token-meter-message-partition-invalid")
        parts = {
            "system": self._count(request.system_prompt) if request.system_prompt else 0,
            "product": sum(self._count(m) for m in messages[:product_messages]),
            "conversation": sum(self._count(m) for m in messages[product_messages:end]),
            "references_and_current_request": sum(self._count(m) for m in messages[end:]),
            "tools": self._count([tool.to_dict() for tool in request.tools]),
            # Visible envelope is counted too; a remote provider's hidden template
            # is unknown, which is why this remains an estimate with explicit margin.
            "envelope": self._count(
                {
                    "model": request.model,
                    "messages": [],
                    "tools": [],
                    "temperature": request.temperature,
                    "max_tokens": request.max_output_tokens,
                }
            ),
        }
        count = sum(parts.values())
        return {
            "format": 1,
            "request_fingerprint": fingerprint(request.to_dict()),
            "provider": request.provider,
            "model": request.model,
            "counter": self.identity,
            "policy": self.policy.to_dict(),
            "parts": parts,
            "input_tokens": count,
            "input_limit": self.policy.input_limit,
            "trigger_tokens": max(1, self.policy.input_limit * self.policy.trigger_percent // 100),
            "output_reserve_tokens": self.policy.output_reserve_tokens,
            "safety_margin_tokens": self.policy.safety_margin_tokens,
            "window_tokens": self.policy.window_tokens,
            "over_limit": count > self.policy.input_limit,
            "product_messages": product_messages,
            "context_messages": context_messages,
        }


def validate_measurement(data, request):
    """Recompute from the frozen request and explicit counter/policy, never latest state."""
    if type(data) is not dict:
        raise ValueError("token-measurement-invalid")
    try:
        meter = RequestTokenMeter(TokenBudgetPolicy(**data["policy"]))
        expected = meter.measure(
            request,
            product_messages=data["product_messages"],
            context_messages=data["context_messages"],
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("token-measurement-invalid") from None
    if canonical_json(expected) != canonical_json(data):
        raise ValueError("token-measurement-mismatch")
    return expected
