"""Project identities, explicit bindings and real local Git membership proof."""

import asyncio
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from memory_fixtures import approve, bind, declare, memory_policy, setup
from test_local_git_workspaces import _git, _repository

from traceh.api.events import PendingEvent
from traceh.api.memory import ProjectScopeLimits
from traceh.memory.service import MemoryService
from traceh.projects.events import PROJECT_STREAM, reference
from traceh.projects.service import ProjectScopeService
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.workspaces.errors import WorkspaceError
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


async def test_same_source_or_session_cannot_be_owned_twice(tmp_path):
    _, scope, sessions, store, _ = await setup(tmp_path)
    source = (await scope.catalog()).sources["project-orion"]
    assert (
        await scope.bind_source(
            project_id="project-orion",
            source_id="source-orion",
            operation_id="read-existing",
            actor_id="operator",
            expected_head=3,
        )
        == source
    )
    first = await scope.resolve("requester")
    assert (
        await scope.bind_session(
            "requester",
            project_id="project-orion",
            operation_id="also-existing",
            actor_id="operator",
            expected_head=3,
        )
        == first
    )
    assert await store.head(PROJECT_STREAM) == 3
    await scope.create(
        project_id="other-project",
        label="Explicit other project",
        operation_id="other",
        actor_id="operator",
        expected_head=3,
    )
    with pytest.raises(ValueError, match="source-conflict"):
        await scope.bind_source(
            project_id="other-project",
            source_id="source-orion",
            operation_id="steal-source",
            actor_id="operator",
            expected_head=4,
        )
    assert await store.head(PROJECT_STREAM) == 4
    await sessions.create_session(await sessions.workspace_for("requester"), session_id="second")
    await bind(scope, "second")
    assert (await scope.resolve("second")).data["project_id"] == "project-orion"


@pytest.mark.parametrize("action", ["read", "approve", "propose"])
async def test_unbound_session_at_same_path_has_no_memory_rights(tmp_path, action):
    authority, _, sessions, store, _ = await setup(tmp_path)
    proposal = await declare(authority)
    await sessions.create_session(await sessions.workspace_for("requester"), session_id="unbound")
    with pytest.raises(ValueError, match="project-session-unbound"):
        if action == "read":
            await authority.read("unbound")
        elif action == "approve":
            await approve(authority, proposal, session_id="unbound")
        else:
            await declare(authority, proposal_id="unbound-proposal", session_id="unbound")
    assert await store.head("memory:project-orion") == 1


async def test_fresh_resolver_drift_refuses_reads_and_approval(tmp_path):
    authority, scope, _, store, resolver = await setup(tmp_path)
    proposal = await declare(authority)
    resolver.mappings["source-orion"] = tmp_path / "different-repository"
    with pytest.raises(ValueError):
        await scope.resolve("requester")
    with pytest.raises(ValueError):
        await approve(authority, proposal)
    assert await store.head("memory:project-orion") == 1


@pytest.mark.parametrize(
    "variant", ["foreign-session", "foreign-source", "schema", "unknown", "order", "extra"]
)
async def test_catalog_corruption_cannot_authorize_memory(tmp_path, variant):
    authority, scope, _, store, _ = await setup(tmp_path)
    binding = await scope.resolve("requester")
    data = {**binding.data, "operation_id": "bad-binding", "expected_head": 3}
    data["session_ref"] = {**data["session_ref"], "stream_id": "session:missing"}
    if variant == "foreign-session":
        data["session_ref"]["digest"] = "e" * 64
    elif variant == "foreign-source":
        data["source_binding_ref"] = {**data["source_binding_ref"], "digest": "f" * 64}
    elif variant == "order":
        data["expected_head"] = 0
    elif variant == "extra":
        data["metadata"] = {"project_id": "claim"}
    await store.append(
        PROJECT_STREAM,
        expected_seq=3,
        events=(
            PendingEvent(
                "project/unknown" if variant == "unknown" else "project/session-bound",
                data,
                schema_version=2 if variant == "schema" else 1,
                actor_id="operator",
            ),
        ),
    )
    with pytest.raises(ValueError):
        if variant == "foreign-session":
            await authority.read("missing")
        else:
            await authority.read("requester")
    assert await store.head("memory:project-orion") == 0


async def test_real_git_binding_is_read_only_accepts_dirty_source_and_rejects_copies(tmp_path):
    source, _ = _repository(tmp_path / "source")
    managed = tmp_path / "uncreated-managed"
    provider = LocalGitWorkspaceProvider(managed_root=managed, sources={"source-orion": source})
    sessions = SessionService(InMemoryEventStore())
    scope = ProjectScopeService(sessions, provider, ProjectScopeLimits(30, 100))
    await sessions.create_session(source, session_id="requester")
    (source / "tracked.txt").write_text("dirty but same source\n", encoding="utf-8")
    await bind(scope, "requester")
    assert not managed.exists()
    assert (await scope.resolve("requester")).data["project_id"] == "project-orion"
    clone = tmp_path / "copy"
    shutil.copytree(source, clone)
    with pytest.raises(WorkspaceError):
        await provider.project_fingerprint("source-orion", clone)
    with pytest.raises(WorkspaceError):
        await provider.project_fingerprint("source-orion", source / ".git")
    worktree = tmp_path / "linked"
    _git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=source)
    assert await provider.project_fingerprint(
        "source-orion", worktree
    ) == await provider.project_fingerprint("source-orion", source)
    second = tmp_path / "linked-second"
    _git("worktree", "add", "--detach", str(second), "HEAD", cwd=source)
    marker = (worktree / ".git").read_bytes()
    other = (second / ".git").read_bytes()
    with (worktree / ".git").open("r+b") as stream:
        stream.write(other)
        stream.truncate()
    try:
        with pytest.raises(WorkspaceError):
            await provider.project_fingerprint("source-orion", worktree)
    finally:
        with (worktree / ".git").open("r+b") as stream:
            stream.write(marker)
            stream.truncate()


def _project_checkouts(tmp_path):
    main, _ = _repository(tmp_path / "primary")
    linked, sibling = tmp_path / "linked-source", tmp_path / "sibling"
    for checkout in (linked, sibling):
        _git("worktree", "add", "--detach", str(checkout), "HEAD", cwd=main)
    return main, linked, sibling


def _git_admin(checkout):
    return Path(_git("rev-parse", "--absolute-git-dir", cwd=checkout))


@contextmanager
def _changed_marker(checkout, replacement):
    marker = checkout / ".git"
    original = marker.read_bytes()

    def write(contents):
        # Git for Windows marks .git hidden: r+b preserves the existing file.
        with marker.open("r+b") as stream:
            stream.write(contents)
            stream.truncate()

    try:
        write(replacement)
        yield
    finally:
        write(original)


async def _linked_project(tmp_path):
    main, linked, sibling = _project_checkouts(tmp_path)
    provider = LocalGitWorkspaceProvider(
        managed_root=tmp_path / "uncreated-managed", sources={"source-orion": linked}
    )
    sessions = SessionService(InMemoryEventStore())
    scope = ProjectScopeService(sessions, provider, ProjectScopeLimits(30, 100))
    await sessions.create_session(linked, session_id="requester")
    await bind(scope, "requester")
    return scope, MemoryService(scope, memory_policy()), main, linked, sibling


@pytest.mark.parametrize("source_kind", ["main", "linked"])
@pytest.mark.parametrize("separate_git_dir", [False, True])
async def test_project_registration_uses_git_registry_for_source_and_consumer(
    tmp_path, source_kind, separate_git_dir, monkeypatch,
):
    main, linked, sibling = _project_checkouts(tmp_path)
    if separate_git_dir:
        # Git's registry does not identify this relocated primary checkout.
        # It must remain unavailable, not gain an inferred path exception.
        _git("init", "--separate-git-dir", str(tmp_path / "git-storage"), cwd=main)
        _git("worktree", "repair", cwd=main)
    source = main if source_kind == "main" else linked
    provider = LocalGitWorkspaceProvider(
        managed_root=tmp_path / "uncreated-managed", sources={"source-orion": source}
    )
    sessions = SessionService(InMemoryEventStore())
    scope = ProjectScopeService(sessions, provider, ProjectScopeLimits(30, 100))
    authority = MemoryService(scope, memory_policy())
    if separate_git_dir:
        registered = [
            Path(line.removeprefix("worktree "))
            for line in _git("worktree", "list", "--porcelain", cwd=source).splitlines()
            if line.startswith("worktree ")
        ]
        assert main not in registered
        await sessions.create_session(main, session_id="requester")
        with pytest.raises(WorkspaceError):
            await bind(scope, "requester")
        assert "requester" not in (await scope.catalog()).sessions
        assert await scope.store.head("memory:project-orion") == 0
        assert not provider.managed_root.exists()
        return
    expected = await provider.project_fingerprint("source-orion", None)
    admin_before = [_git_admin(p) for p in (main, linked, sibling)]
    for session_id, checkout in zip(
        ("requester", "later", "child"), (main, linked, sibling), strict=True,
    ):
        (checkout / "tracked.txt").write_text("dirty but same checkout\n", encoding="utf-8")
        await sessions.create_session(checkout, session_id=session_id)
        await bind(scope, session_id)
        assert await provider.project_fingerprint("source-orion", checkout) == expected
    proposal = await declare(authority)
    await approve(authority, proposal, session_id="later")
    for session_id in ("requester", "later", "child"):
        assert (await authority.read(session_id)).active[0].body == proposal.data["body"]
        assert (await scope.resolve_evidence(session_id)).data["project_id"] == "project-orion"
    assert [_git_admin(p) for p in (main, linked, sibling)] == admin_before
    assert not provider.managed_root.exists()
    with monkeypatch.context() as local:
        local.chdir(source)
        with pytest.raises(WorkspaceError):
            await provider.project_fingerprint("source-orion", Path("."))


@pytest.mark.parametrize("action", ["read", "approve"])
@pytest.mark.parametrize("target_kind", ["sibling", "main"])
async def test_linked_source_marker_change_refuses_memory_access(tmp_path, action, target_kind):
    scope, authority, main, linked, sibling = await _linked_project(tmp_path)
    first = await declare(authority)
    await approve(authority, first)
    pending = await declare(authority, proposal_id="pending", body="A durable second goal.")
    target = sibling if target_kind == "sibling" else main
    target_admin = _git_admin(target)
    assert _git_admin(linked) != target_admin
    # Include a marker aimed at common itself: actual==common alone is not proof
    # that this registered linked checkout is the primary one.
    replacement = f"gitdir: {target_admin.as_posix()}\n".encode()
    before = await scope.store.read("memory:project-orion")
    with _changed_marker(linked, replacement):
        assert _git_admin(linked) == target_admin
        with pytest.raises(WorkspaceError):
            if action == "read":
                await authority.read("requester")
            else:
                # Call the real approval entry directly; no helper read may fail first.
                await authority.approve(
                    "requester",
                    proposal_ref=reference(pending),
                    proposal_digest=pending.data["proposal_digest"],
                    memory_id="second-memory",
                    fact_slot="second-slot",
                    operation_id="second-approval",
                    actor_id="operator",
                    expected_head=3,
                )
        assert await scope.store.read("memory:project-orion") == before
    assert len((await authority.read("requester")).active) == 1


@pytest.mark.parametrize("action", ["mapping", "evidence", "bind-source"])
async def test_linked_source_mapping_is_proven_without_a_consumer(tmp_path, action):
    scope, _, _, linked, sibling = await _linked_project(tmp_path)
    provider = scope.resolver
    expected = await provider.project_fingerprint("source-orion", None)
    fresh = ProjectScopeService(SessionService(InMemoryEventStore()), provider, scope.limits)
    await fresh.create(
        project_id="fresh-project", label="Explicit new association", operation_id="create",
        actor_id="operator", expected_head=0,
    )
    with _changed_marker(linked, (sibling / ".git").read_bytes()):
        assert _git_admin(linked) == _git_admin(sibling)
        with pytest.raises(WorkspaceError):
            if action == "mapping":
                await provider.project_fingerprint("source-orion", None)
            elif action == "evidence":
                await scope.resolve_evidence("requester")
            else:
                await fresh.bind_source(
                    project_id="fresh-project", source_id="source-orion", operation_id="bind",
                    actor_id="operator", expected_head=1,
                )
        assert await fresh.store.head(PROJECT_STREAM) == 1
        assert await scope.store.head(PROJECT_STREAM) == 3
    assert await provider.project_fingerprint("source-orion", None) == expected
    assert not provider.managed_root.exists()


@pytest.mark.parametrize("phase", ["spawn", "wait"])
async def test_project_registration_cancellation_converges_child_before_approval_returns(
    tmp_path, monkeypatch, phase,
):
    scope, authority, _, _, _ = await _linked_project(tmp_path)
    pending = await declare(authority)
    entered, release = asyncio.Event(), asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec
    processes = []

    async def controlled_spawn(*argv, **kwargs):
        if argv[-1] != "--absolute-git-dir":
            return await real_spawn(*argv, **kwargs)
        # Hold an actual child at this Git I/O boundary using pipe readiness,
        # not a sleep. _GitRunner still owns the spawn, cancellation and reaping.
        process = await real_spawn(
            sys.executable, "-c",
            "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)",
            cwd=kwargs["cwd"], env=kwargs["env"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(process)
        assert (await process.stdout.readline()).rstrip(b"\r\n") == b"ready"
        if phase == "spawn":
            entered.set()
            await release.wait()
        else:
            real_wait = process.wait

            async def wait():
                entered.set()
                return await real_wait()

            monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", controlled_spawn)
    call = asyncio.create_task(authority.approve(
        "requester", proposal_ref=reference(pending),
        proposal_digest=pending.data["proposal_digest"], memory_id="memory",
        fact_slot="goal", operation_id="approve", actor_id="operator", expected_head=1,
    ))
    try:
        async with asyncio.timeout(15):
            await entered.wait()
            assert len(processes) == 1 and processes[0].returncode is None
            call.cancel()
            call.cancel()
            assert not call.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await call
        assert processes[0].returncode is not None
        assert await scope.store.head("memory:project-orion") == 1
        assert await scope.store.head(PROJECT_STREAM) == 3
    finally:
        release.set()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
            process.stdin.close()
