"""Workspace launch preference uses the existing project authority, never replaces it."""

from uuid import uuid4


async def eligible_projects(runtime, workspace):
    scope = runtime.project_scope
    if scope is None:
        return ()
    catalog = await scope.catalog()
    labels = {
        e.data["project_id"]: e.data["label"] for e in catalog.events if e.type == "project/created"
    }
    matches = []
    for project_id, source in sorted(catalog.sources.items()):
        try:
            observed = await scope.resolver.project_fingerprint(source.data["source_id"], workspace)
        except ValueError:
            continue
        # Resolver-specific stable errors indicate this checkout is not that source.
        except Exception as error:
            from traceh.workspaces.errors import WorkspaceError

            if not isinstance(error, WorkspaceError):
                raise
            continue
        if observed == source.data["repository_fingerprint"]:
            matches.append((project_id, labels[project_id]))
    return tuple(matches)


async def bind_workspace_project(runtime, session_id, *, project_id, actor_id):
    """Fresh proof, original CAS writer, and original derived Memory index."""
    scope = runtime.project_scope
    if scope is None:
        raise ValueError("project-memory-disabled")
    workspace = await runtime.sessions.workspace_for(session_id)
    if project_id not in dict(await eligible_projects(runtime, workspace)):
        raise ValueError("workspace-project-preference-invalid")
    catalog = await scope.catalog()
    existing = catalog.sessions.get(session_id)
    if existing is not None:
        if existing.data["project_id"] != project_id:
            raise ValueError("project-binding-conflict")
        await scope.resolve(session_id)
    else:
        await scope.bind_session(
            session_id,
            project_id=project_id,
            actor_id=actor_id,
            operation_id=str(uuid4()),
            expected_head=catalog.head,
        )
    if runtime.config.context_input is not None and runtime.config.context_input.memory is not None:
        await runtime.memory.rebuild_index(session_id)


async def restore_workspace_project(runtime, session_id, args, *, choose):
    if runtime.project_scope is None:
        if getattr(args, "default_project_id", None):
            raise ValueError("project-memory-disabled")
        return
    scope = runtime.project_scope
    catalog = await scope.catalog()
    if session_id in catalog.sessions:
        await scope.resolve(session_id)
        if (
            runtime.config.context_input is not None
            and runtime.config.context_input.memory is not None
        ):
            await runtime.memory.rebuild_index(session_id)
        return
    workspace = await runtime.sessions.workspace_for(session_id)
    project = getattr(args, "default_project_id", None)
    actor = getattr(args, "project_actor_id", None)
    if project:
        if getattr(args, "project_workspace", None) != workspace:
            raise ValueError("workspace-project-preference-invalid")
        await bind_workspace_project(runtime, session_id, project_id=project, actor_id=actor)
        return
    options = await eligible_projects(runtime, workspace)
    if not options:
        return
    selected = await choose(options)
    if selected is None:
        return
    project, actor, remember = selected
    await bind_workspace_project(runtime, session_id, project_id=project, actor_id=actor)
    if remember:
        from copy import copy

        from traceh.cli.tui_config import form_values, save_profile

        draft = copy(args)
        draft.default_project_id, draft.project_actor_id = project, actor
        draft.project_workspace = workspace
        # A preference is scoped to this exact checkout; ordinary profile discovery
        # resets session targets but must never transport this choice to another folder.
        draft.workspace = workspace
        draft.session_id = None
        save_profile(draft.tui_profile, form_values(draft))
        args.default_project_id, args.project_actor_id = project, actor
        args.project_workspace = workspace
