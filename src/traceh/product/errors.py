"""Stable, non-echoing failures for the ProductTask fact layer.

Every code here is a fixed identifier. None of them carries a payload value, a
requirement, a model's prose, a path or an exception message: a ProductTask
failure is read by a person and sometimes rendered, so it must not become a
channel for whatever produced it.
"""

from __future__ import annotations


class ProductError(Exception):
    code = "product-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ProductInputError(ProductError, ValueError):
    """A caller-supplied value this domain refuses to write."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"product {field} is not usable")


class ProductProtocolError(ProductError, ValueError):
    """A durable ProductTask fact this build refuses to interpret.

    ``seq`` points at the offending event. Checks that compare the stream as a
    whole report ``seq=0``.
    """

    def __init__(self, code: str, seq: int = 0) -> None:
        self.code = code
        self.seq = seq
        super().__init__("the product task stream protocol is invalid")


class ProductStateError(ProductError):
    """The task is not in a state that allows this operation."""

    def __init__(self, code: str, task_id: str | None = None) -> None:
        self.code = code
        self.task_id = task_id
        super().__init__("the product task is not in a state that allows this")


class ProductEvidenceError(ProductError):
    """A durable fact the caller claimed could not be found or did not match.

    Confirmation is a claim about a person: that they saw an offer and accepted
    it, in a Session, in a Turn, with a message. A DTO can carry those ids, but
    only the Session stream can show they exist and belong together, so this is
    what a failed replay produces rather than a rejected argument.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the claimed durable evidence was not found")


class ProductProfileError(ProductError):
    """Host configuration this build refuses to turn into a runnable plan.

    A Profile is a host decision, so a missing, duplicated, incomplete or
    self-contradicting one is a configuration failure rather than a rejected
    argument or a task state. There is no default Profile to fall back to: the
    absence of a decision is the failure.
    """

    def __init__(self, code: str, field: str | None = None) -> None:
        self.code = code
        self.field = field
        super().__init__("the product profile could not be resolved")


class ProductRoutingError(ProductError):
    """A router answer this build refuses to read as a decision.

    Every one of these is final. A malformed, over-long, ambiguous or unknown
    answer is never retried and never guessed at: re-asking would turn one
    bounded decision into an unbounded loop, and guessing would let prose become
    the decision that the enum beside it exists to carry.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the router answer is not a usable decision")


class ProductOperationConflictError(ProductError):
    code = "product-operation-reused"

    def __init__(self) -> None:
        super().__init__("the operation identity was already used for other content")


class ProductStreamConflictError(ProductError):
    code = "product-stream-changed"

    def __init__(self) -> None:
        super().__init__("the product task stream changed before this fact was recorded")


class ProductWriteError(ProductError):
    """An append that failed, with what is actually known about it.

    ``committed`` is deliberately three-valued. Collapsing ``None`` into
    ``False`` makes the strongest claim from the weakest evidence, exactly when
    the store is already misbehaving.
    """

    code = "product-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "product write failed and whether it was recorded is unknown"
        elif committed:
            message = "product write was recorded but the call failed"
        else:
            message = "product write could not be recorded"
        super().__init__(message)


class ProductContextError(ProductError):
    """The host could not freeze trustworthy Product status for a model Turn."""

    def __init__(self, code: str, *, committed: bool | None = False) -> None:
        self.code = code
        self.committed = committed
        super().__init__("the model-visible product context could not be established")


class ProductServiceClosedError(ProductError):
    code = "product-service-closed"

    def __init__(self) -> None:
        super().__init__("the product task service is closed")


__all__ = [
    "ProductError",
    "ProductContextError",
    "ProductEvidenceError",
    "ProductInputError",
    "ProductOperationConflictError",
    "ProductProfileError",
    "ProductProtocolError",
    "ProductRoutingError",
    "ProductServiceClosedError",
    "ProductStateError",
    "ProductStreamConflictError",
    "ProductWriteError",
]
