"""Initial material quotas and complete frozen-tree materialization."""

import pytest

from traceh.evaluation.errors import BenchmarkExecutionError
from traceh.evaluation.repositories import (
    InitialTreeLimits,
    build_attempt_repositories,
    capture_initial_tree,
    initial_tree_digest,
)


@pytest.mark.parametrize("values", [(True, 1, 1), (0, 1, 1), (1, -1, 1), (1, 2, 1)])
def test_invalid_tree_limits_are_rejected(values):
    with pytest.raises(ValueError, match="invalid initial tree limits"):
        InitialTreeLimits(*values)


@pytest.mark.parametrize("count,size", [(257, 1), (2, 1_048_577)])
def test_explicit_limits_admit_independent_large_trees(tmp_path, count, size):
    for index in range(count):
        (tmp_path / f"{index}.bin").write_bytes(b"x" * size)
    captured = capture_initial_tree(tmp_path, limits=InitialTreeLimits(count, size, count * size))
    assert len(captured) == count
    assert sum(len(data) for _, data in captured) == count * size


@pytest.mark.parametrize(
    "limits",
    [
        InitialTreeLimits(1, 4, 8),
        InitialTreeLimits(2, 3, 8),
        InitialTreeLimits(2, 4, 7),
    ],
)
def test_each_explicit_quota_rejects_before_building_a_repository(tmp_path, limits):
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "a").write_bytes(b"abcd")
    (initial / "b").write_bytes(b"efgh")
    with pytest.raises(BenchmarkExecutionError) as caught:
        capture_initial_tree(initial, limits=limits)
    assert caught.value.code == "benchmark-initial-tree-too-large"


@pytest.mark.asyncio
async def test_frozen_ignored_files_survive_build_and_bare_clone(tmp_path):
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / ".gitignore").write_bytes(b"fixture.dat\n")
    (initial / "fixture.dat").write_bytes(b"upstream tracked fixture\n")
    limits = InitialTreeLimits(2, 100, 200)
    frozen = capture_initial_tree(initial, limits=limits)
    result = await build_attempt_repositories(
        initial_dir=initial,
        source=tmp_path / "source",
        target=tmp_path / "target.git",
        limits=limits,
        expected_initial_digest=initial_tree_digest(frozen),
    )
    # Public Git contents, not an implementation-private index or copied file.
    import asyncio

    process = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        str(result.target),
        "show",
        f"{result.base_revision}:fixture.dat",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr
    assert stdout == b"upstream tracked fixture\n"


@pytest.mark.asyncio
async def test_material_drift_fails_before_creating_source_or_target(tmp_path):
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "a").write_bytes(b"before")
    limits = InitialTreeLimits(1, 20, 20)
    expected = initial_tree_digest(capture_initial_tree(initial, limits=limits))
    (initial / "a").write_bytes(b"after")
    source, target = tmp_path / "source", tmp_path / "target.git"
    with pytest.raises(BenchmarkExecutionError) as caught:
        await build_attempt_repositories(
            initial_dir=initial,
            source=source,
            target=target,
            limits=limits,
            expected_initial_digest=expected,
        )
    assert caught.value.code == "evaluation-frozen-input-drift"
    assert not source.exists()
    assert not target.exists()
