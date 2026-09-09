"""Explicit, immutable inputs for current-Session History disclosure."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal

from traceh.api.json_types import JsonValue, fingerprint

HISTORY_PAGE_POLICY_VERSION = "history-turn-pages-v2"


def _digest(value: object, code: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryReadPolicy:
    """All source, page and request limits are explicit host configuration.

    Source limits cover the observed Session prefix, including audit events.
    Page limits cover complete canonical message arrays, including punctuation.
    The request owner enforces ``max_requests``; the pure reader never accepts
    requests or creates authority to disclose a page.
    """

    max_blocks: int
    max_depth: int
    page_bytes: int
    page_messages: int
    max_source_events: int
    max_source_bytes: int
    max_requests: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in self.to_dict().values()):
            raise ValueError("history-policy-invalid")

    def to_dict(self) -> dict[str, JsonValue]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, raw: object) -> HistoryReadPolicy:
        if type(raw) is not dict or set(raw) != {field.name for field in fields(cls)}:
            raise ValueError("history-policy-invalid")
        return cls(**raw)

    @property
    def digest(self) -> str:
        return fingerprint({"version": HISTORY_PAGE_POLICY_VERSION, "config": self.to_dict()})


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    block_id: str
    policy_digest: str
    index: int

    def __post_init__(self) -> None:
        _digest(self.block_id, "history-cursor-invalid")
        _digest(self.policy_digest, "history-cursor-invalid")
        if type(self.index) is not int or self.index < 0:
            raise ValueError("history-cursor-invalid")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "block_id": self.block_id,
            "policy_digest": self.policy_digest,
            "index": self.index,
        }

    @classmethod
    def from_dict(cls, raw: object) -> HistoryCursor:
        if type(raw) is not dict or set(raw) != {"block_id", "policy_digest", "index"}:
            raise ValueError("history-cursor-invalid")
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class HistoryPageRequest:
    """An exact host input or Tool request, not proof that it was authorized."""

    block_id: str
    cursor: HistoryCursor
    requested_tier: Literal["section", "chunk"]

    def __post_init__(self) -> None:
        _digest(self.block_id, "history-request-invalid")
        if (
            type(self.cursor) is not HistoryCursor
            or self.cursor.block_id != self.block_id
            or type(self.requested_tier) is not str
            or self.requested_tier not in {"section", "chunk"}
        ):
            raise ValueError("history-request-invalid")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "block_id": self.block_id,
            "cursor": self.cursor.to_dict(),
            "requested_tier": self.requested_tier,
        }

    @classmethod
    def from_dict(cls, raw: object) -> HistoryPageRequest:
        if type(raw) is not dict or set(raw) != {"block_id", "cursor", "requested_tier"}:
            raise ValueError("history-request-invalid")
        return cls(
            block_id=raw["block_id"],
            cursor=HistoryCursor.from_dict(raw["cursor"]),
            requested_tier=raw["requested_tier"],
        )
