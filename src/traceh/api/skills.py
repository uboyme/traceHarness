"""Typed Skill metadata and explicit host resource configuration.

Descriptors are data only. Contribution bodies are held by the existing
Activation/Generation owner and never assembled as system prompt sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath
from typing import Literal

from packaging.specifiers import SpecifierSet

from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.plugins import PluginIdentity


def _id(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("skill-id-invalid")


def _text(value: object) -> None:
    if type(value) is not str or not value:
        raise ValueError("skill-text-invalid")
    value.encode("utf-8")


def _navigation_text(title: str, summary: str) -> None:
    _text(title)
    _text(summary)
    if not title.strip() or not summary.strip():
        raise ValueError("skill-navigation-text-invalid")


def _navigation_entry(value, key: str) -> dict[str, JsonValue]:
    return {name: getattr(value, name) for name in (key, "title", "summary", "content_bytes")}


def _content(digest: object, size: object) -> None:
    if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("skill-digest-invalid")
    if type(size) is not int or size < 0:
        raise ValueError("skill-size-invalid")


def _exact(raw: object, cls) -> dict:
    if type(raw) is not dict or set(raw) != {item.name for item in fields(cls)}:
        raise ValueError("skill-descriptor-keys-invalid")
    return raw


def _unique(values: tuple, cls, key: str, *, ordered: bool = True) -> None:
    if type(values) is not tuple or any(type(item) is not cls for item in values):
        raise ValueError("skill-descriptor-invalid")
    ids = [getattr(item, key) for item in values]
    if len(ids) != len(set(ids)) or (ordered and ids != sorted(ids)):
        raise ValueError("skill-identities-invalid")


@dataclass(frozen=True, slots=True)
class SkillChunk:
    chunk_id: str
    byte_start: int
    byte_end: int
    content_digest: str
    content_bytes: int
    title: str = field(kw_only=True)
    summary: str = field(kw_only=True)

    def __post_init__(self) -> None:
        _id(self.chunk_id)
        _navigation_text(self.title, self.summary)
        _content(self.content_digest, self.content_bytes)
        if (
            type(self.byte_start) is not int
            or type(self.byte_end) is not int
            or not 0 <= self.byte_start < self.byte_end
            or self.content_bytes != self.byte_end - self.byte_start
        ):
            raise ValueError("skill-chunk-range-invalid")

    def to_dict(self) -> dict[str, JsonValue]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @classmethod
    def from_dict(cls, raw: object) -> SkillChunk:
        return cls(**_exact(raw, cls))


@dataclass(frozen=True, slots=True)
class SkillResource:
    resource_id: str
    relative_path: str
    content_digest: str
    content_bytes: int
    title: str = field(kw_only=True)
    summary: str = field(kw_only=True)
    chunks: tuple[SkillChunk, ...] = ()

    def __post_init__(self) -> None:
        _id(self.resource_id)
        _navigation_text(self.title, self.summary)
        _content(self.content_digest, self.content_bytes)
        _text(self.relative_path)
        path = PurePosixPath(self.relative_path)
        if (
            not path.parts
            or path.is_absolute()
            or str(path) != self.relative_path
            or any(p in {".", ".."} or p.endswith((".", " ")) for p in path.parts)
            or any(
                re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
                for p in path.parts
            )
            or any(c in self.relative_path for c in "\\:\x00")
            or any(ord(c) < 32 for c in self.relative_path)
        ):
            raise ValueError("skill-resource-path-invalid")
        _unique(self.chunks, SkillChunk, "chunk_id", ordered=False)
        end = 0
        for chunk in self.chunks:
            if chunk.byte_start < end or chunk.byte_end > self.content_bytes:
                raise ValueError("skill-chunk-range-invalid")
            end = chunk.byte_end

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "resource_id": self.resource_id,
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "content_bytes": self.content_bytes,
            "title": self.title,
            "summary": self.summary,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, raw: object) -> SkillResource:
        data = _exact(raw, cls)
        if type(data["chunks"]) is not list:
            raise ValueError("skill-descriptor-invalid")
        return cls(**{**data, "chunks": tuple(SkillChunk.from_dict(c) for c in data["chunks"])})


@dataclass(frozen=True, slots=True)
class SkillSection:
    section_id: str
    tier: Literal["section"]
    content_digest: str
    content_bytes: int
    title: str = field(kw_only=True)
    summary: str = field(kw_only=True)

    def __post_init__(self) -> None:
        _id(self.section_id)
        _navigation_text(self.title, self.summary)
        _content(self.content_digest, self.content_bytes)
        if self.tier != "section":
            raise ValueError("skill-section-tier-invalid")

    def to_dict(self) -> dict[str, JsonValue]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @classmethod
    def from_dict(cls, raw: object) -> SkillSection:
        return cls(**_exact(raw, cls))


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    skill_id: str
    version: str
    plugin: PluginIdentity
    title: str
    summary: str
    tags: tuple[str, ...]
    requires_traceh: str
    sections: tuple[SkillSection, ...]
    resources: tuple[SkillResource, ...]

    def __post_init__(self) -> None:
        _id(self.skill_id)
        _id(self.version)
        if type(self.plugin) is not PluginIdentity:
            raise ValueError("skill-plugin-invalid")
        _id(self.plugin.plugin_id)
        _text(self.plugin.version)
        _text(self.title)
        _text(self.summary)
        _text(self.requires_traceh)
        SpecifierSet(self.requires_traceh)
        if type(self.tags) is not tuple or any(type(tag) is not str for tag in self.tags):
            raise ValueError("skill-tags-invalid")
        for tag in self.tags:
            _text(tag)
        if tuple(sorted(set(self.tags))) != self.tags:
            raise ValueError("skill-tags-invalid")
        _unique(self.sections, SkillSection, "section_id")
        _unique(self.resources, SkillResource, "resource_id")

    def navigation(self) -> dict[str, JsonValue]:
        """Bounded author metadata, derived only from this frozen descriptor.

        Shared by directory Context and the disclosure receipt. Never read or
        summarize body bytes, infer meaning from IDs, or expose host paths.
        """
        return {
            "sections": [_navigation_entry(section, "section_id") for section in self.sections],
            "resources": [
                {
                    **_navigation_entry(resource, "resource_id"),
                    "relative_path": resource.relative_path,
                    "chunks": [_navigation_entry(chunk, "chunk_id") for chunk in resource.chunks],
                }
                for resource in self.resources
            ],
        }

    def directory(self) -> dict[str, JsonValue]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "plugin": self.plugin.to_dict(),
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            **self.navigation(),
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "plugin": self.plugin.to_dict(),
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "requires_traceh": self.requires_traceh,
            "sections": [section.to_dict() for section in self.sections],
            "resources": [resource.to_dict() for resource in self.resources],
        }

    @classmethod
    def from_dict(cls, raw: object) -> SkillDescriptor:
        data = _exact(raw, cls)
        plugin = data["plugin"]
        if type(plugin) is not dict or set(plugin) != {"plugin_id", "version"}:
            raise ValueError("skill-plugin-invalid")
        if any(type(data[k]) is not list for k in ("tags", "sections", "resources")):
            raise ValueError("skill-descriptor-invalid")
        result = cls(
            **{
                **data,
                "plugin": PluginIdentity(**plugin),
                "tags": tuple(data["tags"]),
                "sections": tuple(SkillSection.from_dict(x) for x in data["sections"]),
                "resources": tuple(SkillResource.from_dict(x) for x in data["resources"]),
            }
        )
        if canonical_json(result.to_dict()) != canonical_json(data):
            raise ValueError("skill-descriptor-invalid")
        return result


@dataclass(frozen=True, slots=True)
class SkillSectionContent:
    section_id: str
    body: str

    def __post_init__(self) -> None:
        _id(self.section_id)
        if type(self.body) is not str:
            raise ValueError("skill-section-content-invalid")
        self.body.encode("utf-8")


@dataclass(frozen=True, slots=True)
class SkillContribution:
    descriptor: SkillDescriptor
    sections: tuple[SkillSectionContent, ...]

    def __post_init__(self) -> None:
        if type(self.descriptor) is not SkillDescriptor:
            raise ValueError("skill-descriptor-invalid")
        _unique(self.sections, SkillSectionContent, "section_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillLimits:
    max_skills: int
    max_catalog_bytes: int
    max_summary_bytes: int
    max_content_bytes: int
    max_resource_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0
            for f in fields(self)
        ):
            raise ValueError("skill-limits-invalid")


@dataclass(frozen=True, slots=True)
class SkillResourceRoot:
    """Trusted host binding; never supplied through a plugin contribution."""

    plugin: PluginIdentity
    path: Path

    def __post_init__(self) -> None:
        if type(self.plugin) is not PluginIdentity or not isinstance(self.path, Path):
            raise ValueError("skill-root-binding-invalid")
        if not self.path.is_absolute():
            raise ValueError("skill-root-binding-invalid")


@dataclass(frozen=True, slots=True)
class SkillPolicy:
    limits: SkillLimits
    resource_roots: tuple[SkillResourceRoot, ...] = ()

    def __post_init__(self) -> None:
        if type(self.limits) is not SkillLimits or type(self.resource_roots) is not tuple:
            raise ValueError("skill-policy-invalid")
        if any(type(root) is not SkillResourceRoot for root in self.resource_roots):
            raise ValueError("skill-policy-invalid")
        ids = [root.plugin.plugin_id for root in self.resource_roots]
        if len(ids) != len(set(ids)):
            raise ValueError("skill-root-binding-ambiguous")


def validate_skill_catalog(
    catalog: tuple[SkillDescriptor, ...], plugins: tuple[PluginIdentity, ...]
) -> None:
    _unique(catalog, SkillDescriptor, "skill_id")
    if any(descriptor.plugin not in plugins for descriptor in catalog):
        raise ValueError("skill-catalog-plugin-mismatch")
