"""Human-readable projections over durable traces."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.projections import StateProjector
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector


class SessionInspector:
    def __init__(self, sessions: SessionService, surface: SurfaceProjector) -> None:
        self.sessions = sessions
        self.surface = surface
        self.state = StateProjector()
        self.invariants = CoreInvariantChecker()

    async def summary(self, session_id: str) -> dict[str, object]:
        events = await self.sessions.read_session(session_id)
        effects = await self.sessions.read_effects(session_id)
        await self.sessions.ensure_session(session_id)
        state = self.state.project(events)
        violations = self.invariants.check(events, effects)
        request_violations = await verify_request_snapshots(self.sessions, self.surface, session_id)
        return {
            "session_id": session_id,
            "workspace": str(await self.sessions.workspace_for(session_id)),
            "status": state.status.value,
            "last_seq": state.last_seq,
            "turns_completed": state.turns_completed,
            "steps_completed": state.steps_completed,
            "open_turn_id": state.open_turn_id,
            "open_step_id": state.open_step_id,
            "event_counts": dict(Counter(event.type for event in events)),
            "effect_counts": dict(Counter(event.type for event in effects)),
            "invariant_violations": [
                {"name": item.name, "message": item.message, "seq": item.seq}
                for item in violations
            ],
            "request_reconstruction_violations": list(request_violations),
        }

    async def render_text(self, session_id: str, *, include_events: bool = True) -> str:
        summary = await self.summary(session_id)
        lines = [
            f"Session: {summary['session_id']}",
            f"Workspace: {summary['workspace']}",
            f"Status: {summary['status']}",
            f"Events: {summary['last_seq']}",
            f"Turns / Steps: {summary['turns_completed']} / {summary['steps_completed']}",
            f"Invariant violations: {len(summary['invariant_violations'])}",
            f"Request reconstruction violations: "
            f"{len(summary['request_reconstruction_violations'])}",
        ]
        if include_events:
            lines.append("\nSEQ  TYPE                         DETAILS")
            lines.append("---  ---------------------------  ----------------------------------------")
            for event in await self.sessions.read_session(session_id):
                detail = ""
                for key in (
                    "content",
                    "reason",
                    "tool_name",
                    "status",
                    "model",
                    "message",
                ):
                    if key in event.data and event.data[key] not in (None, ""):
                        detail = f"{key}={event.data[key]}"
                        break
                detail = detail.replace("\n", " ")[:100]
                lines.append(f"{event.seq:>3}  {event.type:<27}  {detail}")
        return "\n".join(lines)

    async def replay_text(self, session_id: str) -> str:
        messages = self.surface.project(await self.sessions.read_session(session_id))
        lines = []
        for index, message in enumerate(messages, start=1):
            header = f"[{index}] {message.role}"
            if message.name:
                header += f" ({message.name})"
            if message.tool_call_id:
                header += f" call={message.tool_call_id}"
            lines.append(header)
            if message.content:
                lines.append(message.content)
            for call in message.tool_calls:
                lines.append(
                    f"tool_call {call.id}: {call.name} "
                    f"{json.dumps(call.arguments, ensure_ascii=False)}"
                )
            lines.append("")
        return "\n".join(lines).rstrip()

    async def render_html(self, session_id: str, output: Path) -> Path:
        summary = await self.summary(session_id)
        events = await self.sessions.read_session(session_id)
        effects = await self.sessions.read_effects(session_id)
        rows = []
        for event in (*events, *effects):
            rows.append(
                "<tr>"
                f"<td>{event.seq}</td>"
                f"<td>{html.escape(event.stream_id)}</td>"
                f"<td>{html.escape(event.type)}</td>"
                f"<td><pre>{html.escape(json.dumps(event.data, ensure_ascii=False, indent=2))}</pre></td>"
                "</tr>"
            )
        document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TraceHarness Session {html.escape(session_id)}</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem; color: #111; }}
h1 {{ margin-bottom: .25rem; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: .75rem; }}
.card {{ border: 1px solid #ddd; border-radius: 10px; padding: .8rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1.5rem; }}
th, td {{ border: 1px solid #ddd; padding: .5rem; vertical-align: top; text-align: left; }}
pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; max-width: 70vw; }}
.bad {{ color: #a00; font-weight: 700; }}
</style>
</head>
<body>
<h1>TraceHarness Session</h1>
<p>{html.escape(session_id)}</p>
<div class="summary">
<div class="card"><b>Status</b><br>{html.escape(str(summary['status']))}</div>
<div class="card"><b>Workspace</b><br>{html.escape(str(summary['workspace']))}</div>
<div class="card"><b>Events</b><br>{summary['last_seq']}</div>
<div class="card"><b>Violations</b><br><span class="{'bad' if summary['invariant_violations'] else ''}">{len(summary['invariant_violations'])}</span></div>
</div>
<table>
<thead><tr><th>Seq</th><th>Stream</th><th>Type</th><th>Data</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</body>
</html>"""
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(document, encoding="utf-8")
        return output
