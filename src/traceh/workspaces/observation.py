"""Host observations of qualified Git revisions, without a mutable workspace cache."""

from traceh.projects.events import digest, exact
from traceh.workspaces.errors import WorkspaceError


def validate_observation(value):
    if value is None:
        return
    exact(value, {"source_identity", "source_revision"})
    digest(value["source_identity"])
    revision = value["source_revision"]
    if revision is not None and (
        type(revision) is not str
        or len(revision) not in {40, 64}
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise ValueError("workspace-observation-invalid")


class WorkspaceObserver:
    def __init__(self, scope):
        if not callable(getattr(scope.resolver, "project_observation", None)):
            raise ValueError("workspace-observation-unsupported")
        self._scope = scope

    async def observe(self, session_id):
        try:
            binding = await self._scope.resolve(session_id)
            catalog = await self._scope.catalog()
            source = catalog.sources[binding.data["project_id"]]
            workspace = await self._scope.sessions.workspace_for(session_id)
            value = await self._scope.resolver.project_observation(
                source.data["source_id"], workspace
            )
            validate_observation(value)
            if value is None or value["source_identity"] != source.data["repository_fingerprint"]:
                return None
            return value
        except (WorkspaceError, ValueError):
            # Observation is optional evidence, not a substitute for Memory's access check.
            return None


def stable_observation(before, after):
    validate_observation(before)
    validate_observation(after)
    return before if before is not None and before == after else None
