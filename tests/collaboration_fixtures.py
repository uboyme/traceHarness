"""Explicit test work contracts, never production defaults."""

MAIN_WORK = {
    "goal": "Create the requested output using the source findings.",
    "deliverable": "The requested file and its verification.",
    "uses_child_report": "Use source evidence to check the implementation boundary.",
}
CHILD_WORK = {
    "goal": "Inspect the original source relevant to the request.",
    "scope": "Read the initial checkout and identify relevant source facts.",
    "exclusions": "Do not implement the main output, write files, or spawn agents.",
    "deliverable": "Source findings with file and line evidence and unknowns.",
    "briefing": "The main agent owns implementation and verification.",
}
READONLY_ASSIGNMENT = {
    "assignment_id": "explicit-test-investigation",
    "role": "investigator",
    **CHILD_WORK,
}
def flat_main_work(main_work):
    """The plan tool's three top-level main-work arguments for one work object."""
    return {
        "main_goal": main_work["goal"],
        "main_deliverable": main_work["deliverable"],
        "main_uses_child_report": main_work["uses_child_report"],
    }


PLAN = {**flat_main_work(MAIN_WORK), "children": [READONLY_ASSIGNMENT]}


def assignment(assignment_id, role="investigator", **overrides):
    """One explicit test assignment; never a production default."""
    base = {"assignment_id": assignment_id, "role": role, **CHILD_WORK}
    return {**base, **overrides}


async def run_failed_product(tmp_path, store, source, target, provider):
    import test_product_f3_e2e as product

    from traceh.api.product import RequestedTaskMode
    from traceh.artifacts.cas import LocalArtifactCas
    from traceh.session.service import SessionService

    actions = product.ProductTurnActions()
    host = await product._build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.MULTI,
        provider,
    )
    runtime = product._chat_runtime(tmp_path, store, actions)
    console = product._Console(("please add the accepted file", "yes, do it", "START"))
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir(exist_ok=True)
    await product.run_chat(
        runtime, console.console, workspace=workspace, timeline=False, product=host
    )
    task_id = product._proposed_task_id(console.output)
    summary = await product.ProductTaskStreamReader(store).load(task_id)
    assert summary.status.value == "failed", console.output
    assert "awaiting_approval" not in console.output
    events = []
    for stream in await store.list_streams(prefix="session:"):
        events.extend(await SessionService(store).read_session(stream.removeprefix("session:")))
    return task_id, events
