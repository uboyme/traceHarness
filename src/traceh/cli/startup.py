"""Read-only old-data diagnostic and explicit fresh-data launch intention."""

import json
import sqlite3
from contextlib import closing
from copy import copy
from pathlib import Path
from uuid import uuid4

from traceh.cli.tui_config import LaunchConfigurationError, form_values, save_profile
from traceh.session.protocol import CONTEXT_PROTOCOL
from traceh.session.sqlite import APPLICATION_ID, DATABASE_FILENAME, SCHEMA_VERSION


def contains_old_data(data_dir: Path, session_id: str | None = None) -> bool:
    """Presentation only; production Store and Session readers remain authoritative."""
    root = Path(data_dir).resolve() / "events"
    if any(root.glob("*.jsonl")):
        return True
    path = root / DATABASE_FILENAME
    if not path.exists():
        return False
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)) as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if app_id != APPLICATION_ID:
                raise ValueError
            if 0 < version < SCHEMA_VERSION:
                return True
            if version != SCHEMA_VERSION:
                raise ValueError
            sql = "SELECT envelope_json FROM events WHERE seq=1 AND stream_id LIKE 'session:%'"
            parameters = ()
            if session_id:
                sql += " AND stream_id=?"
                parameters = (f"session:{session_id}",)
            for (raw,) in db.execute(sql, parameters):
                marker = json.loads(raw)["data"].get("context_protocol")
                if marker is None or (type(marker) is int and 0 <= marker < CONTEXT_PROTOCOL):
                    return True
        return False
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise LaunchConfigurationError(
            "数据目录无法检查；请在完整配置中检查路径，原数据保留。"
        ) from None


def start_fresh(args):
    """Invoked only by the explicit new-data button; never rename or migrate old data."""
    result = copy(getattr(args, "_tui_launch_inputs", args))
    result.tui_api_key = getattr(args, "tui_api_key", None)
    # A new sibling avoids nesting storage into the old store being preserved.
    old = Path(args.data_dir).resolve()
    result.data_dir = old.parent / f"{old.name}-v{CONTEXT_PROTOCOL}-{uuid4().hex[:8]}"
    result.data_dir.mkdir(exist_ok=False)
    result.workspace = getattr(args, "_entry_workspace", None) or args.workspace or Path.cwd()
    result.session_id = None
    result.default_project_id = result.project_actor_id = None
    result.project_workspace = None
    save_profile(result.tui_profile, form_values(result))
    return result
