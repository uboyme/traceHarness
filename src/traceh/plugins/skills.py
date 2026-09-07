"""Generation-owned, bounded snapshots of validated Skill resource roots.

Activation captures immutable bytes once. Reload may replace source files, but
an old Lease still reads its old snapshot. Cleanup uses the original Activation
registration; this module creates no task, persistent store or lifecycle.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.plugins import PluginIdentity
from traceh.api.skills import SkillContribution, SkillDescriptor, SkillPolicy
from traceh.kernel.activation import Activation
from traceh.version import __version__


def _verify_body(body: bytes, digest: str, size: int) -> None:
    body.decode("utf-8")
    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("skill-content-mismatch")


def _plain_path(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("skill-resource-link-rejected")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("skill-resource-type-invalid")
    return info


def _read_resource(root: Path, relative: str, limit: int) -> bytes:
    # The root comes from host configuration bound to the exact PluginIdentity.
    # The plugin can name only a validated portable relative resource path.
    _plain_path(root, directory=True)
    base = root.resolve(strict=True)
    path = base
    parts = relative.split("/")
    for index, part in enumerate(parts):
        if part.casefold() == ".git" or part.casefold().startswith(".env"):
            raise ValueError("skill-resource-private-path")
        path /= part
        info = _plain_path(path, directory=index != len(parts) - 1)
    if not path.resolve(strict=True).is_relative_to(base) or info.st_size > limit:
        raise ValueError("skill-resource-limit-or-containment")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
            raise ValueError("skill-resource-changed")
        body = source.read(limit + 1)
        final = os.fstat(source.fileno())
    current = _plain_path(path, directory=False)
    if (
        len(body) > limit
        or not os.path.samestat(opened, current)
        or opened.st_size != final.st_size
        or opened.st_mtime_ns != final.st_mtime_ns
        or not path.resolve(strict=True).is_relative_to(base)
    ):
        raise ValueError("skill-resource-changed")
    return body


class FrozenSkill:
    """One validated resource root snapshot, closed by its Activation only."""

    def __init__(
        self,
        descriptor: SkillDescriptor,
        sections: dict[str, bytes],
        resources: dict[str, bytes],
        activation: Activation,
    ) -> None:
        self._activation = activation
        self._descriptor = descriptor
        self._sections = sections
        self._resources = resources
        self._closed = False
        self._receipt = fingerprint(descriptor.to_dict())
        self.content_bytes = sum(map(len, sections.values())) + sum(map(len, resources.values()))

    @property
    def descriptor(self) -> SkillDescriptor:
        return self._descriptor

    def verify(self) -> None:
        if self._closed or fingerprint(self.descriptor.to_dict()) != self._receipt:
            raise ValueError("skill-receipt-invalid")
        if set(self._sections) != {section.section_id for section in self.descriptor.sections}:
            raise ValueError("skill-receipt-invalid")
        if set(self._resources) != {resource.resource_id for resource in self.descriptor.resources}:
            raise ValueError("skill-receipt-invalid")
        for section in self.descriptor.sections:
            _verify_body(
                self._sections[section.section_id], section.content_digest, section.content_bytes
            )
        for resource in self.descriptor.resources:
            _verify_body(
                self._resources[resource.resource_id],
                resource.content_digest,
                resource.content_bytes,
            )

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._sections.clear()
            self._resources.clear()

    def _read(self, kind: str, identity: str, chunk_id: str | None = None) -> bytes:
        if self._closed:
            raise RuntimeError("skill-resource-closed")
        values = self._sections if kind == "section" else self._resources
        if identity not in values:
            raise ValueError("skill-resource-not-declared")
        body = values[identity]
        if chunk_id is not None:
            resource = next(r for r in self.descriptor.resources if r.resource_id == identity)
            chunk = next((c for c in resource.chunks if c.chunk_id == chunk_id), None)
            if chunk is None:
                raise ValueError("skill-chunk-not-declared")
            body = body[chunk.byte_start : chunk.byte_end]
        return body


def freeze_skill(
    contribution: SkillContribution,
    plugin: PluginIdentity,
    policy: SkillPolicy,
    activation: Activation,
) -> FrozenSkill:
    if type(contribution) is not SkillContribution:
        raise ValueError("skill-contribution-invalid")
    descriptor = SkillDescriptor.from_dict(contribution.descriptor.to_dict())
    if descriptor.plugin != plugin:
        raise ValueError("skill-plugin-mismatch")
    if Version(__version__) not in SpecifierSet(descriptor.requires_traceh):
        raise ValueError("skill-core-incompatible")
    limits = policy.limits
    if (
        len(canonical_json(descriptor.to_dict()).encode("utf-8")) > limits.max_catalog_bytes
        or len(descriptor.summary.encode("utf-8")) > limits.max_summary_bytes
    ):
        raise ValueError("skill-descriptor-resource-limit")
    sections = {
        section.section_id: section.body.encode("utf-8") for section in contribution.sections
    }
    if set(sections) != {s.section_id for s in descriptor.sections}:
        raise ValueError("skill-section-binding-mismatch")
    total = sum(map(len, sections.values()))
    if total + sum(r.content_bytes for r in descriptor.resources) > limits.max_content_bytes:
        raise ValueError("skill-content-resource-limit")
    for section in descriptor.sections:
        _verify_body(sections[section.section_id], section.content_digest, section.content_bytes)
    resources: dict[str, bytes] = {}
    if descriptor.resources:
        roots = [root for root in policy.resource_roots if root.plugin == plugin]
        if len(roots) != 1:
            raise ValueError("skill-host-root-unavailable")
        for resource in descriptor.resources:
            if resource.content_bytes > limits.max_resource_bytes:
                raise ValueError("skill-resource-limit-or-containment")
            body = _read_resource(roots[0].path, resource.relative_path, limits.max_resource_bytes)
            _verify_body(body, resource.content_digest, resource.content_bytes)
            for chunk in resource.chunks:
                _verify_body(
                    body[chunk.byte_start : chunk.byte_end],
                    chunk.content_digest,
                    chunk.content_bytes,
                )
            resources[resource.resource_id] = body
    return FrozenSkill(descriptor, sections, resources, activation)


class LeasedSkillReader:
    """Read declared immutable content only during this exact Composition Lease.

    This host interface neither selects model context nor grants Tools. F2 will
    consume it under its separate durable selection and disclosure policy.
    """

    def __init__(self, skills: tuple[FrozenSkill, ...], active: Callable[[], bool]) -> None:
        self._skills = skills
        self._active = active

    @property
    def catalog(self) -> tuple[SkillDescriptor, ...]:
        self._check()
        return tuple(skill.descriptor for skill in self._skills)

    def _check(self) -> None:
        if not self._active():
            raise RuntimeError("skill-lease-inactive")

    def _read(self, skill_id: str, kind: str, identity: str, chunk_id: str | None = None) -> bytes:
        self._check()
        skill = next((item for item in self._skills if item.descriptor.skill_id == skill_id), None)
        if skill is None:
            raise ValueError("skill-not-in-leased-catalog")
        return skill._read(kind, identity, chunk_id)

    def read_section(self, skill_id: str, section_id: str) -> bytes:
        return self._read(skill_id, "section", section_id)

    def read_resource(self, skill_id: str, resource_id: str) -> bytes:
        return self._read(skill_id, "resource", resource_id)

    def read_chunk(self, skill_id: str, resource_id: str, chunk_id: str) -> bytes:
        return self._read(skill_id, "resource", resource_id, chunk_id)
