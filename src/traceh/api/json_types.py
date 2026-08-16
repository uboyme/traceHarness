"""JSON-compatible type aliases and canonical encoding helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TypeAlias
from uuid import UUID

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def to_json_value(value: object) -> JsonValue:
    """Convert framework values into deterministic JSON-compatible values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return to_json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_value(item) for item in value]
    raise TypeError(f"Value is not JSON serializable: {type(value)!r}")


def canonical_json(value: object) -> str:
    return json.dumps(
        to_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
