"""Shared reconstruction of a Session's durable plugin identity.

The Session event log is the only durable authority for which external plugin
composition a Session may use.  Runtime admission, explicit migration and the
invariant checker all use this module so that they cannot drift into subtly
different interpretations of old snapshots or migration authorizations.
"""

from __future__ import annotations

from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from traceh.api.events import EventEnvelope
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginIdentity
from traceh.plugins.selection import is_plugin_id

MIGRATION_EVENT_TYPE = "composition/migration-authorized"
PLUGIN_METADATA_KEY = "traceh_plugins"
_MISSING = object()


@dataclass(frozen=True, slots=True)
class PersistedPluginComposition:
    """The latest valid durable identity and the event that established it."""

    plugins: tuple[PluginIdentity, ...]
    source_seq: int


@dataclass(frozen=True, slots=True)
class PluginIdentityIssue:
    """A stable, non-secret validation result for an identity event."""

    code: str
    seq: int


class PluginIdentityProtocolError(ValueError):
    """Raised when a durable identity fact is malformed or inconsistent."""

    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__(_safe_message(code))


def _safe_message(code: str) -> str:
    messages = {
        "session-created-present": "session plugin identity is missing its creation event",
        "session-plugins-valid": "session plugin identity is malformed",
        "session-plugins-duplicate": "session plugin identity contains duplicate ids",
        "composition-plugins-valid": "composition snapshot plugin identity is malformed",
        "migration-id-present": "migration authorization id is missing or invalid",
        "migration-source-present": "migration authorization source is missing or invalid",
        "migration-plugins-valid": "migration authorization plugin identity is malformed",
        "migration-from-matches-current": (
            "migration authorization source does not match the session"
        ),
        "migration-source-seq-matches": "migration authorization source sequence is stale",
        "migration-outside-turn": "migration authorization must be outside a Turn",
    }
    return messages.get(code, "session plugin identity protocol is invalid")


def _issue(code: str, event: EventEnvelope) -> PluginIdentityIssue:
    return PluginIdentityIssue(code, event.seq)


def parse_plugin_identities(
    value: object,
    *,
    allow_core: bool,
    error_code: str,
    seq: int,
    duplicate_error_code: str | None = None,
) -> tuple[PluginIdentity, ...]:
    """Parse a JSON identity list without coercing malformed values."""

    if not isinstance(value, list):
        raise PluginIdentityProtocolError(error_code, seq)
    identities: list[PluginIdentity] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise PluginIdentityProtocolError(error_code, seq)
        plugin_id = item.get("plugin_id")
        version = item.get("version")
        if not isinstance(plugin_id, str) or not isinstance(version, str):
            raise PluginIdentityProtocolError(error_code, seq)
        if not is_plugin_id(plugin_id):
            raise PluginIdentityProtocolError(error_code, seq)
        if not allow_core and plugin_id == CORE_PLUGIN_IDENTITY.plugin_id:
            raise PluginIdentityProtocolError(error_code, seq)
        try:
            Version(version)
        except InvalidVersion:
            raise PluginIdentityProtocolError(error_code, seq) from None
        if plugin_id in seen:
            raise PluginIdentityProtocolError(duplicate_error_code or error_code, seq)
        seen.add(plugin_id)
        identities.append(PluginIdentity(plugin_id, version))
    return tuple(identities)


def comparable_plugin_identities(
    identities: tuple[PluginIdentity, ...],
) -> tuple[tuple[str, Version], ...]:
    """Return the PEP 440 comparison key while retaining original text elsewhere."""

    return tuple((identity.plugin_id, Version(identity.version)) for identity in identities)


def external_plugin_identities(
    identities: tuple[PluginIdentity, ...],
) -> tuple[PluginIdentity, ...]:
    return tuple(
        identity
        for identity in identities
        if identity.plugin_id != CORE_PLUGIN_IDENTITY.plugin_id
    )


def _scan(
    events: tuple[EventEnvelope, ...],
) -> tuple[PersistedPluginComposition | None, tuple[PluginIdentityIssue, ...]]:
    issues: list[PluginIdentityIssue] = []
    if not events or events[0].type != "session/created":
        seq = events[0].seq if events else 0
        return None, (PluginIdentityIssue("session-created-present", seq),)

    created = events[0]
    metadata = created.data.get("metadata", {})
    if not isinstance(metadata, dict):
        return None, (PluginIdentityIssue("session-plugins-valid", created.seq),)
    raw_initial = metadata.get(PLUGIN_METADATA_KEY, _MISSING)
    if raw_initial is _MISSING:
        current: tuple[PluginIdentity, ...] = ()
    else:
        try:
            current = parse_plugin_identities(
                raw_initial,
                allow_core=False,
                error_code="session-plugins-valid",
                seq=created.seq,
                duplicate_error_code="session-plugins-duplicate",
            )
        except PluginIdentityProtocolError as error:
            return None, (PluginIdentityIssue(error.code, error.seq),)

    source_seq = created.seq
    migration_ids: set[str] = set()
    open_turn = False
    open_step = False

    for event in events[1:]:
        if event.type == "turn/start":
            open_turn = True
        elif event.type == "step/start":
            open_step = True
        elif event.type == "step/end":
            open_step = False
        elif event.type == "turn/end":
            open_turn = False

        if event.type == "composition/snapshot":
            raw_plugins = event.data.get("plugins", _MISSING)
            if raw_plugins is _MISSING:
                # v0.3 snapshots did not carry plugin identities.  They do not
                # replace the last known identity fact.
                continue
            try:
                snapshot = parse_plugin_identities(
                    raw_plugins,
                    allow_core=True,
                    error_code="composition-plugins-valid",
                    seq=event.seq,
                )
            except PluginIdentityProtocolError as error:
                issues.append(PluginIdentityIssue(error.code, error.seq))
                continue
            current = external_plugin_identities(snapshot)
            source_seq = event.seq
            continue

        if event.type != MIGRATION_EVENT_TYPE:
            continue

        data = event.data
        event_issues: list[PluginIdentityIssue] = []
        migration_id = data.get("migration_id")
        if not isinstance(migration_id, str) or not migration_id.strip():
            event_issues.append(_issue("migration-id-present", event))
        elif migration_id in migration_ids:
            event_issues.append(_issue("migration-id-present", event))

        raw_source_seq = data.get("source_seq")
        if (
            not isinstance(raw_source_seq, int)
            or isinstance(raw_source_seq, bool)
            or raw_source_seq < 1
        ):
            event_issues.append(_issue("migration-source-present", event))

        try:
            from_plugins = parse_plugin_identities(
                data.get("from_plugins"),
                allow_core=False,
                error_code="migration-plugins-valid",
                seq=event.seq,
            )
            to_plugins = parse_plugin_identities(
                data.get("to_plugins"),
                allow_core=False,
                error_code="migration-plugins-valid",
                seq=event.seq,
            )
        except PluginIdentityProtocolError as error:
            event_issues.append(PluginIdentityIssue(error.code, error.seq))
            from_plugins = None
            to_plugins = None

        if open_turn or open_step:
            event_issues.append(_issue("migration-outside-turn", event))
        if from_plugins is not None and comparable_plugin_identities(from_plugins) != (
            comparable_plugin_identities(current)
        ):
            event_issues.append(_issue("migration-from-matches-current", event))
        if (
            isinstance(raw_source_seq, int)
            and not isinstance(raw_source_seq, bool)
            and raw_source_seq != source_seq
        ):
            event_issues.append(_issue("migration-source-seq-matches", event))

        if event_issues:
            issues.extend(event_issues)
            continue
        migration_ids.add(migration_id)
        current = to_plugins
        source_seq = event.seq

    facts = PersistedPluginComposition(current, source_seq)
    return facts, tuple(issues)


def validate_plugin_identity_events(
    events: tuple[EventEnvelope, ...],
) -> tuple[PluginIdentityIssue, ...]:
    """Return stable validation codes without exposing third-party text."""

    _, issues = _scan(events)
    return issues


def rebuild_plugin_identity(events: tuple[EventEnvelope, ...]) -> PersistedPluginComposition:
    """Rebuild the latest durable Session identity or raise a fixed error."""

    facts, issues = _scan(events)
    if issues:
        first = issues[0]
        raise PluginIdentityProtocolError(first.code, first.seq)
    if facts is None:
        raise PluginIdentityProtocolError("session-created-present", 0)
    return facts


def migration_event_data(
    *,
    migration_id: str,
    source_seq: int,
    from_plugins: tuple[PluginIdentity, ...],
    to_plugins: tuple[PluginIdentity, ...],
) -> dict[str, object]:
    """Build the bounded append-only authorization payload."""

    return {
        "migration_id": migration_id,
        "source_seq": source_seq,
        "from_plugins": [identity.to_dict() for identity in from_plugins],
        "to_plugins": [identity.to_dict() for identity in to_plugins],
    }


def find_migration_event(
    events: tuple[EventEnvelope, ...],
    migration_id: str,
) -> EventEnvelope | None:
    """Find a durable authorization by exact, typed migration id."""

    for event in events:
        if event.type != MIGRATION_EVENT_TYPE:
            continue
        if event.data.get("migration_id") == migration_id:
            return event
    return None


__all__ = [
    "MIGRATION_EVENT_TYPE",
    "PLUGIN_METADATA_KEY",
    "PersistedPluginComposition",
    "PluginIdentityIssue",
    "PluginIdentityProtocolError",
    "comparable_plugin_identities",
    "external_plugin_identities",
    "find_migration_event",
    "migration_event_data",
    "parse_plugin_identities",
    "rebuild_plugin_identity",
    "validate_plugin_identity_events",
]
