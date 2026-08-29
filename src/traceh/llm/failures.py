"""Stable, sanitized failures owned by Provider adapters.

The host may make retry and accounting decisions from these typed fields.  It
must never inspect a transport exception's text, HTTP body or headers.
"""

from __future__ import annotations

import re
from enum import StrEnum
from math import isfinite

from traceh.api.llm import Usage

_STABLE_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


class ProviderFailureCategory(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    CONFIGURATION = "configuration"
    PROTOCOL = "protocol"
    DNS = "dns"
    TIMEOUT = "timeout"
    TLS_EOF = "tls_eof"
    DISCONNECTED = "disconnected"
    RATE_LIMITED = "rate_limited"
    SERVER_TRANSIENT = "server_transient"
    UNKNOWN = "unknown"


class ProviderFailure(RuntimeError):
    """One adapter-classified failure with no raw provider material.

    ``str(error)`` deliberately contains only the stable code.  A numeric
    Retry-After hint and usage evidence are the only adapter facts allowed to
    cross the boundary in addition to the category.
    """

    __slots__ = ("category", "code", "retry_after_seconds", "usage")

    def __init__(
        self,
        code: str,
        category: ProviderFailureCategory,
        *,
        retry_after_seconds: float | None = None,
        usage: Usage | None = None,
    ) -> None:
        if type(code) is not str or _STABLE_CODE.fullmatch(code) is None:
            raise ValueError("provider failure code must be stable kebab-case")
        if type(category) is not ProviderFailureCategory:
            raise TypeError("provider failure category must be ProviderFailureCategory")
        if retry_after_seconds is not None and (
            type(retry_after_seconds) not in {int, float}
            or retry_after_seconds < 0
            or not isfinite(retry_after_seconds)
        ):
            raise ValueError("retry_after_seconds must be finite and non-negative")
        if usage is not None and type(usage) is not Usage:
            raise TypeError("provider failure usage must be Usage")
        super().__init__(code)
        self.code = code
        self.category = category
        self.retry_after_seconds = (
            None if retry_after_seconds is None else float(retry_after_seconds)
        )
        self.usage = usage


__all__ = ["ProviderFailure", "ProviderFailureCategory"]
