# Example Skill

This file is a packaged plugin resource. It ships inside the wheel and is read
through `importlib.resources`, so the plugin never depends on a path that only
exists in a source checkout.

## What the harness does with it

Nothing automatically. The plugin reads this file during `setup()` and turns its
title into one prompt section. The harness has no notion of a "skill file" and
does not scan any directory looking for one.

## What this example deliberately does not do

- It does not read `~/.codex`, `~/.claude`, or any other user or tool directory.
- It does not read environment variables or network resources.
- It does not become active because it is installed; it is active only when the
  operator names it via `--plugin` or `TRACEH_PLUGINS`.
