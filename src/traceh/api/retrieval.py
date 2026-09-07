"""Explicit offline Skill retrieval policy. No models or numeric tuning defaults."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import asdict, dataclass

from traceh.api.json_types import fingerprint


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillRetrievalPolicy:
    unicode_version: str
    default_tier: str
    match_fields: tuple[str, ...]
    k1: float
    b: float
    rrf_constant: int
    exact_weight: int
    fts_weight: int
    skill_bytes: int
    max_catalog_bytes: int
    max_terms: int
    max_corpus_items: int
    max_corpus_bytes: int
    max_candidates: int
    max_requests: int

    def __post_init__(self) -> None:
        if self.unicode_version != unicodedata.unidata_version:
            raise ValueError("skill-unicode-version-unsupported")
        if self.default_tier not in {"directory", "summary"}:
            raise ValueError("skill-disclosure-policy-invalid")
        if (
            type(self.match_fields) is not tuple
            or not self.match_fields
            or len(set(self.match_fields)) != len(self.match_fields)
            or not set(self.match_fields) <= {"id", "symbol", "path", "error", "tag"}
        ):
            raise ValueError("skill-exact-policy-invalid")
        for name in ("k1", "b"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError("skill-ranker-policy-invalid")
        if self.k1 <= 0 or not 0 <= self.b <= 1:
            raise ValueError("skill-ranker-policy-invalid")
        for name in set(self.__dataclass_fields__) - {
            "unicode_version",
            "default_tier",
            "match_fields",
            "k1",
            "b",
        }:
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "skill_bytes" else 1):
                raise ValueError("skill-resource-policy-invalid")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["match_fields"] = list(self.match_fields)
        return result

    @property
    def digest(self) -> str:
        return fingerprint(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> SkillRetrievalPolicy:
        if type(data) is not dict or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("skill-retrieval-policy-unsupported")
        if type(data["match_fields"]) is not list:
            raise ValueError("skill-exact-policy-invalid")
        return cls(**{**data, "match_fields": tuple(data["match_fields"])})
