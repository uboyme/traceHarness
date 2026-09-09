"""Derived reference allowance; original Context/Composition/events remain the sources."""

from dataclasses import dataclass

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelMessage, ModelRequest
from traceh.llm.token_meter import RequestTokenMeter, TokenBudgetPolicy
from traceh.session.surface import SurfaceProjector


@dataclass(frozen=True, slots=True)
class ReferenceTokenBudget:
    meter: RequestTokenMeter
    fixed_input_tokens: int
    fixed_input_fingerprint: str

    @classmethod
    def derive(cls, events, composition, meter):
        request = ModelRequest(
            provider=composition.provider,
            model=composition.model,
            system_prompt=composition.system_prompt,
            messages=SurfaceProjector().project(events),
            tools=composition.tools,
            temperature=composition.temperature,
            max_output_tokens=composition.max_output_tokens,
        )
        measured = meter.measure(request, context_messages=0)
        return cls(meter, measured["input_tokens"], fingerprint(request.to_dict()))

    @classmethod
    def from_frozen(cls, data):
        """Parse only the basis; the Context owner recomputes the final rendered count."""
        if type(data) is not dict or set(data) != {
            "status",
            "counter",
            "policy",
            "fixed_input_tokens",
            "fixed_input_fingerprint",
            "reference_limit_tokens",
            "reference_tokens",
            "input_tokens",
            "input_limit",
            "over_limit",
        }:
            raise ValueError("context-token-budget-invalid")
        try:
            meter = RequestTokenMeter(TokenBudgetPolicy(**data["policy"]))
            fixed = data["fixed_input_tokens"]
            digest = data["fixed_input_fingerprint"]
            if (
                data["status"] != "estimated"
                or canonical_json(data["counter"]) != canonical_json(meter.identity)
                or canonical_json(data["policy"]) != canonical_json(meter.policy.to_dict())
                or type(fixed) is not int
                or fixed < 0
                or type(digest) is not str
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("context-token-budget-invalid")
            return cls(meter, fixed, digest)
        except (KeyError, TypeError, ValueError):
            raise ValueError("context-token-budget-invalid") from None

    @property
    def reference_limit(self):
        return max(0, self.meter.policy.input_limit - self.fixed_input_tokens)

    def measure(self, content):
        count = self.meter.count_message(ModelMessage("user", content))
        total = self.fixed_input_tokens + count
        return {
            "status": "estimated",
            "counter": self.meter.identity,
            "policy": self.meter.policy.to_dict(),
            "fixed_input_tokens": self.fixed_input_tokens,
            "fixed_input_fingerprint": self.fixed_input_fingerprint,
            "reference_limit_tokens": self.reference_limit,
            "reference_tokens": count,
            "input_tokens": total,
            "input_limit": self.meter.policy.input_limit,
            "over_limit": total > self.meter.policy.input_limit,
        }
