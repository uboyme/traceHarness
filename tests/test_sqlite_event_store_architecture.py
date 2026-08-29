"""F1 guards for the one production EventStore and its explicit owners."""

from __future__ import annotations

import ast
from pathlib import Path

import traceh
from traceh.session.event_store import Durability

SOURCE_ROOT = Path(traceh.__file__).resolve().parent


def _function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        ),
        None,
    )
    assert function is not None, f"missing async owner {path.name}:{name}"
    return function


def _called_names(node: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
        if isinstance(call.func, ast.Name):
            names.append(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            names.append(call.func.attr)
    return tuple(names)


def test_every_production_runtime_factory_call_borrows_an_explicit_store() -> None:
    factories = {"build_default_runtime", "build_default_runtime_async"}
    for source in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else None
            )
            if name in factories:
                assert any(keyword.arg == "event_store" for keyword in call.keywords), (
                    source.relative_to(SOURCE_ROOT),
                    call.lineno,
                )


def test_production_has_one_sqlite_backend_and_one_durability_value() -> None:
    assert tuple(Durability) == (Durability.SYNC,)
    assert not (SOURCE_ROOT / "session" / "jsonl.py").exists()
    session_init = (SOURCE_ROOT / "session" / "__init__.py").read_text(encoding="utf-8")
    assert "SqliteEventStore" in session_init
    assert "JsonlEventStore" not in session_init


def test_cli_eval_and_comparison_own_sqlite_around_borrowing_runtimes() -> None:
    cli = SOURCE_ROOT / "cli" / "main.py"
    for owner in ("_runtime_scope", "_plain_runtime_scope", "_chat"):
        names = _called_names(_function(cli, owner))
        assert names.count("SqliteEventStore") == 1, owner
        assert "aclose" in names, owner

    cli_owners = {
        "_run": "_runtime_scope",
        "_resume": "_runtime_scope",
        "_recover": "_plain_runtime_scope",
        "_inspect": "_plain_runtime_scope",
        "_replay": "_plain_runtime_scope",
        "_sessions": "_plain_runtime_scope",
        "_compact": "_plain_runtime_scope",
    }
    for command, owner in cli_owners.items():
        assert owner in _called_names(_function(cli, command)), command

    attempt = SOURCE_ROOT / "evaluation" / "attempt.py"
    attempt_names = _called_names(_function(attempt, "run_attempt"))
    assert attempt_names.count("SqliteEventStore") == 1
    assert "aclose" in attempt_names

    comparison = SOURCE_ROOT / "evolution" / "comparison_probe.py"
    comparison_names = _called_names(_function(comparison, "_run_case"))
    assert comparison_names.count("SqliteEventStore") == 1
    assert "aclose" in comparison_names
