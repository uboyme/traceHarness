"""Build explicit journey preconditions through production host operations."""

import json

from traceh.projects.events import reference

from .journey_fixtures import commit, git, seed_memory


class FixtureFailure(RuntimeError):
    """A named, non-secret evaluation precondition failed."""


def require(condition, code):
    if not condition:
        raise FixtureFailure(code)


async def bootstrap(state, corpus, case):
    runtime, identity = state["runtime"], state["identity"]
    setup = case["setup"]
    known = {
        "active",
        "active-cross-session",
        "proposed",
        "revoked",
        "superseded",
        "foreign",
        "history-first",
        "history-two",
        "history-stale",
        "skill-memory",
        "memory-history",
        "skill-memory-history",
        "current-file",
        "semantic-memory",
        "semantic-skill",
    }
    require(setup in known, "unknown-journey-setup")
    if setup in {
        "active",
        "active-cross-session",
        "skill-memory",
        "memory-history",
        "skill-memory-history",
    }:
        await seed_memory(state, corpus, "active")
    elif setup == "proposed":
        await seed_memory(state, corpus, "proposed", approve=False)
    elif setup == "revoked":
        await seed_memory(state, corpus, "revoked")
        await runtime.memory.revoke(
            state["session"],
            memory_id=identity("memory-revoked"),
            fact_slot=corpus["facts"]["revoked"]["fact_slot"],
            predecessor_ref=reference(state["activations"]["revoked"]),
            predecessor_digest=state["proposals"]["revoked"].data["proposal_digest"],
            operation_id=identity("revoke"),
            actor_id="evaluation-host",
            expected_head=(await runtime.memory.read(state["session"])).head,
        )
    elif setup == "superseded":
        await seed_memory(state, corpus, "predecessor")
        await seed_memory(state, corpus, "successor", predecessor="predecessor")
    elif setup == "foreign":
        foreign = await runtime.create_session(state["other"])
        state["sessions"].append(foreign)
        await state["bind"](foreign, "other-")
        await seed_memory(state, corpus, "foreign", session=foreign)
    elif setup == "semantic-memory":
        await seed_memory(state, corpus, "semantic")
    if setup == "active-cross-session":
        state["session"] = await runtime.create_session(state["workspace"])
        state["sessions"].append(state["session"])
        await state["bind"](state["session"])
    session = state["session"]
    state["selected_ids"] = []
    if setup in {"skill-memory", "skill-memory-history", "semantic-skill"}:
        await runtime.skill_context.select(
            session,
            operation_id=identity("select-skills"),
            expected_head=0,
            actor_id="evaluation-host",
            skills=state["selected"],
        )
        await runtime.skill_context.rebuild_index(session)
        state["selected_ids"] = [item["skill_id"] for item in state["selected"]]
    await runtime.memory.rebuild_index(session)
    keys = {
        "history-first": ["first"],
        "history-two": ["first", "second"],
        "memory-history": ["old_retention"],
        "skill-memory-history": ["batch"],
    }.get(setup, [])
    state["bootstrap_turns"] = []
    for key in keys:
        start_seq = len(await runtime.sessions.read_session(session))
        turn = await runtime.run_existing(
            session,
            corpus["history"][key] + "\n" + corpus["history"]["seed_instruction"],
        )
        state["bootstrap_turns"].append(turn.turn_id)
        require(turn.reason == "completed", "history-bootstrap-not-completed")
        events = await runtime.sessions.read_session(session)
        require(
            not any(e.type == "tool/call" and e.seq > start_seq for e in events),
            "history-bootstrap-unexpected-tool",
        )
    if setup == "history-stale":
        observed = corpus["files"]["observed"]
        start_seq = len(await runtime.sessions.read_session(session))
        turn = await runtime.run_existing(
            session,
            f"请读取工作区 {observed['path']}，只报告文件中当前状态值。",
        )
        state["bootstrap_turns"].append(turn.turn_id)
        require(turn.reason == "completed", "history-bootstrap-not-completed")
        events = await runtime.sessions.read_session(session)
        reads = [
            e
            for e in events
            if e.type == "tool/result"
            and e.seq > start_seq
            and e.data["tool_name"] == "read_file"
            and e.data["status"] == "succeeded"
            and e.data["data"].get("path") == observed["path"]
            and observed["before"].strip() in e.data["content"]
        ]
        require(bool(reads), "history-bootstrap-file-not-read")
        previous = reads[-1].data["workspace_observation"]
        require(
            previous is not None and previous["source_revision"] is not None,
            "history-bootstrap-observation-unknown",
        )
        (state["workspace"] / observed["path"]).write_text(observed["after"], encoding="utf-8")
        git(state["workspace"], "add", observed["path"])
        commit(state["workspace"], "synthetic observed file revision")
        current = await runtime.memory.observe_workspace(session)
        require(
            current is not None
            and current["source_revision"] is not None
            and current["source_identity"] == previous["source_identity"]
            and current["source_revision"] != previous["source_revision"],
            "history-bootstrap-revision-not-changed",
        )
        state["revision_change"] = {"previous": previous, "current": current}
    if state["bootstrap_turns"]:
        events = await runtime.sessions.read_session(session)
        await runtime.compaction.replace_through(
            session,
            through_seq=events[-1].seq,
            summary=corpus["history"]["summary"],
        )
        visible = json.dumps(
            [
                m.to_dict()
                for m in runtime.surface.project(await runtime.sessions.read_session(session))
            ],
            ensure_ascii=False,
        )
        bodies = [corpus["history"][key] for key in keys]
        if setup == "history-stale":
            bodies.append(corpus["files"]["observed"]["before"].strip())
        require(
            all(body not in visible for body in bodies), "history-bootstrap-source-still-visible"
        )
    state["target_start_seq"] = len(await runtime.sessions.read_session(session)) + 1


def question(case):
    # The visible contract is an explicit input, separate from hidden expected values.
    fields = case["answer_fields"]
    if set(fields) != set(case["answers"]):
        raise ValueError("answer-fields-do-not-match-expected-keys")
    if not fields or any(
        not isinstance(value, str) or not value.strip() for value in fields.values()
    ):
        raise ValueError("answer-field-description-required")
    specification = json.dumps(fields, ensure_ascii=False)
    return (
        case["query"]
        + f"\n请只返回一个 JSON 对象，每个值都使用字符串。字段及取值要求为：{specification}。"
        "不要在对象外添加说明。"
    )


async def expected_sources(state, corpus, skill_corpus, case):
    active = (await state["runtime"].memory.read(state["session"])).active
    facts = {
        fact.memory_id: {
            "id": fact.memory_id,
            "version": fact.proposal.data["proposal_digest"],
            "source_refs": [reference(fact.proposal), reference(fact.activation)],
            "body": fact.body,
        }
        for fact in active
    }
    sources = {}
    for source in case["sources"]:
        kind, name = source.split(":", 1)
        if kind == "memory":
            sources[source] = facts[state["identity"]("memory-" + name)]
        elif kind == "history":
            sources[source] = {
                "body": corpus["files"]["observed"]["before"].strip()
                if name == "observed"
                else corpus["history"][name],
            }
        elif kind == "skill":
            parts = name.split("/")
            skill = next(s for s in skill_corpus["skills"] if s["key"] == parts[0])
            if len(parts) == 2:
                body = next(s["body"] for s in skill["sections"] if s["key"] == parts[1])
            else:
                resource = next(r for r in skill["resources"] if r["key"] == parts[1])
                body = next(c["body"] for c in resource["chunks"] if c["key"] == parts[2])
            sources[source] = {"identity": state["references"][name], "body": body}
        elif kind == "file":
            file = corpus["files"]["observed" if name == "observed-after" else name]
            sources[source] = {
                "path": file["path"],
                "body": file["after"] if name == "observed-after" else file["body"],
            }
        else:
            raise FixtureFailure("unknown-source-kind")
    return {
        "sources": sources,
        "active_memory": facts,
        "selected_ids": state["selected_ids"],
        "session_id": state["session"],
        "project_id": state["identity"]("project"),
        "target_start_seq": state["target_start_seq"],
        "bootstrap_turns": state["bootstrap_turns"],
        "revision_change": state.get("revision_change"),
        "forbidden_bodies": [
            corpus["facts"][name]["body"] for name in case.get("forbidden_bodies", [])
        ],
    }
