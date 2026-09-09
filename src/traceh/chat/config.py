"""Explicit host file for Context governance; no model or project defaults."""

import json
from dataclasses import dataclass, fields
from pathlib import Path

from traceh.api.memory import MemoryPolicy, ProjectMemoryConfig, ProjectScopeLimits
from traceh.api.plugins import PluginIdentity
from traceh.api.skills import SkillLimits, SkillPolicy, SkillResourceRoot
from traceh.session.context_input import ContextInputPolicy
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("context-host-config-shape-invalid")
    return value


def memory_limits(raw):
    data = exact(raw, (field.name for field in fields(MemoryPolicy)))
    if type(data["denied_patterns"]) is not list:
        raise ValueError("memory-content-rules-required")
    return MemoryPolicy(**{**data, "denied_patterns": tuple(data["denied_patterns"])})


@dataclass(frozen=True)
class ContextHostSettings:
    context: ContextInputPolicy
    skill_policy: SkillPolicy | None
    memory: ProjectMemoryConfig | None
    sources: tuple[tuple[str, Path], ...]
    managed_root: Path | None


def load_context_host_file(path):
    path = Path(path).resolve(strict=True)
    return parse_context_host_config(json.loads(path.read_text(encoding="utf-8")), path=path)


def parse_context_host_config(raw, *, path):
    """The same strict parser for disk loading and a not-yet-saved UI draft."""
    path = Path(path).resolve()
    raw = exact(
        raw,
        {"format", "context", "skill_policy", "project"},
    )
    if type(raw["format"]) is not int or raw["format"] != 1:
        raise ValueError("context-host-config-version-unsupported")
    policy = ContextInputPolicy.from_dict(raw["context"])
    skill = raw["skill_policy"]
    if skill is not None:
        skill = exact(skill, {"limits", "resource_roots"})
        if type(skill["resource_roots"]) is not list:
            raise ValueError("skill-root-binding-invalid")
        roots = []
        for entry in skill["resource_roots"]:
            entry = exact(entry, {"plugin", "path"})
            identity = PluginIdentity(**exact(entry["plugin"], {"plugin_id", "version"}))
            if type(entry["path"]) is not str or not entry["path"]:
                raise ValueError("skill-root-binding-invalid")
            roots.append(SkillResourceRoot(identity, (path.parent / entry["path"]).resolve()))
        skill = SkillPolicy(
            SkillLimits(**exact(skill["limits"], (f.name for f in fields(SkillLimits)))),
            tuple(roots),
        )
    project = raw["project"]
    memory, sources, managed = None, (), None
    if project is not None:
        project = exact(project, {"sources", "managed_root", "limits", "memory_policy"})

        def host_path(value):
            if type(value) is not str or not value:
                raise ValueError("context-host-path-invalid")
            return (path.parent / value).resolve()

        if type(project["sources"]) is not dict or not project["sources"]:
            raise ValueError("context-project-sources-required")
        sources = tuple(sorted((k, host_path(v)) for k, v in project["sources"].items()))
        managed = host_path(project["managed_root"])
        memory = ProjectMemoryConfig(
            ProjectScopeLimits(
                **exact(project["limits"], (f.name for f in fields(ProjectScopeLimits)))
            ),
            memory_limits(project["memory_policy"]),
            LocalGitWorkspaceProvider(managed_root=managed, sources=dict(sources)),
        )
    if policy.memory is not None and memory is None:
        raise ValueError("context-memory-authority-required")
    if policy.skills is not None and skill is None:
        raise ValueError("context-skill-limits-required")
    return ContextHostSettings(policy, skill, memory, sources, managed)
