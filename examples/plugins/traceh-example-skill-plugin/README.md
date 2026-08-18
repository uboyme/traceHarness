# traceh-example-skill-plugin

A minimal, real TraceHarness plugin distribution. It exists to demonstrate - and
to let the test suite verify - the whole external plugin path: build a wheel,
install it, be discovered through `importlib.metadata`, and be activated only
when explicitly enabled.

## What it contributes

| Contribution | Name | Notes |
|---|---|---|
| Prompt section | `traceh.example.skill` | One sentence pointing the model at the tool |
| Tool | `example_skill_info` | `PURE_READ`, no arguments, returns a packaged `SKILL.md` |

## Installing and enabling

Installing makes it **discoverable**, not active:

```powershell
python -m pip install .\examples\plugins\traceh-example-skill-plugin
traceh plugins list
```

Enabling is a separate, explicit act:

```powershell
traceh plugins doctor traceh.example.skill
traceh run <workspace> "..." --plugin traceh.example.skill
```

`TRACEH_PLUGINS=traceh.example.skill` does the same thing; any `--plugin` on the
command line replaces the environment value entirely.

## Deliberate limits

This is an example, not a default capability. It reads no user directory (in
particular no Codex or Claude configuration), no environment variable and no
network resource, and it is never enabled just by being installed.

See [`../../../docs/plugins.md`](../../../docs/plugins.md) for the full author
contract.
