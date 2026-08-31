# Independent Plugin Package Template

Replace every angle-bracket placeholder with an explicitly approved value. Do
not leave placeholders in the candidate and do not infer missing product
behaviour from this structural template.

## Required tree

```text
<distribution-root>/
├── CANDIDATE.md
├── README.md
├── pyproject.toml
├── src/
│   └── <import_package>/
│       └── __init__.py
└── tests/
    ├── conftest.py
    └── test_<short_name>_plugin.py
```

`pyproject.toml` supplies the Wheel build configuration; do not add a second
packaging system unless the specification requires it.

## `pyproject.toml` shape

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "<distribution-name>"
version = "<version>"
description = "<approved-summary>"
requires-python = ">=3.12"
dependencies = ["traceharness-py>=0.8,<0.9"]

[project.entry-points."traceh.plugins"]
"<plugin.id>" = "<import_package>:<EntryClass>"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
asyncio_mode = "auto"
```

Add only explicitly approved runtime dependencies. Development-only tools do
not belong in the runtime dependency list.

## Plugin implementation shape

```python
from __future__ import annotations

from traceh.plugins import PluginContext, PluginManifest

PLUGIN_ID = "<plugin.id>"
PLUGIN_VERSION = "<version>"


class <EntryClass>:
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        requires_traceh=">=0.8,<0.9",
        allowed_scopes=("application",),
        trust_mode="trusted",
        provides=(<explicit-capability-ids>,),
    )

    async def setup(self, context: PluginContext, config: dict[str, object]) -> None:
        # Register only the explicitly requested contributions here.
        ...
```

The ellipsis is an instruction marker, not valid final candidate behaviour.
Implement the approved capability and remove it. Use public SDK contracts from
`traceh.plugins` for Tool, Prompt, Service, Provider, Policy, Middleware and
Verifier patterns. If cleanup or a background task is not required, do not add
one.

## Tests and README

Tests must exercise the actual contribution contract, at minimum:

- manifest and Entry Point identities agree;
- setup registers exactly the requested contribution kinds;
- one positive behaviour case;
- one material rejection or invalid-input case;
- one failure, cancellation or cleanup case when the capability owns lifecycle
  or external effects;
- no candidate code relies on a demo name or machine-specific path.

The README explains capability, installation-versus-enablement, exact doctor and
enable commands, authority, configuration, test command and current limits. It
must not claim validation that has not occurred.
