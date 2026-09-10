"""Docker selection is presentation; saved authority remains a pinned policy."""

import asyncio
import json
import os
import subprocess
import sys
import threading

import pytest

from traceh.tui import docker_choices as choices

IMAGE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
REAL_CONTEXT = os.environ.get("TRACEH_SANDBOX_TEST_CONTEXT")
REAL_IMAGE = os.environ.get("TRACEH_SANDBOX_TEST_IMAGE")


async def test_read_only_queries_group_aliases_and_pin_manual_tag(monkeypatch):
    calls = []

    async def query(*argv):
        calls.append(argv)
        if argv[:2] == ("context", "ls"):
            return '"研究环境"\n"other"\n"other"\n'
        if "ls" in argv:
            return "\n".join(json.dumps(row) for row in [
                dict(ID=IMAGE, Repository="research/python", Tag="dev"),
                dict(ID=IMAGE, Repository="alias", Tag="v2"),
                dict(ID=OTHER, Repository="<none>", Tag="<none>"),
            ])
        return json.dumps(dict(id=IMAGE, os="linux", volumes=None))

    monkeypatch.setattr(choices, "_query", query)
    assert await choices.contexts() == [("other", "other"), ("研究环境", "研究环境")]
    options = dict((identity, label) for label, identity in await choices.images("研究环境"))
    assert len(options) == 2 and "research/python:dev" in options[IMAGE]
    assert "alias:v2" in options[IMAGE] and "未命名" in options[OTHER]
    assert await choices.resolve_image("研究环境", "research/python:dev") == IMAGE
    assert calls[-1][-2:] == ("--", "research/python:dev")
    assert all(argv[:2] == ("--context", "研究环境") for argv in calls[1:])
    assert not any(word in {"pull", "run", "create", "build"} for argv in calls for word in argv)


@pytest.mark.parametrize("reply", [
    "not json", "{}", '"bad shape"',
    json.dumps(dict(id=IMAGE, os="windows", volumes=None)),
    json.dumps(dict(id=IMAGE, os="linux", volumes={"/data": {}})),
    json.dumps(dict(id=OTHER, os="linux", volumes=None)),
])
async def test_invalid_or_changed_identity_is_refused(monkeypatch, reply):
    async def query(*argv):
        return reply

    monkeypatch.setattr(choices, "_query", query)
    with pytest.raises(choices.DockerChoiceError):
        await choices.resolve_image("some-engine", IMAGE)


@pytest.mark.parametrize("reference", ["", "  ", "--help", "one two", "a\0b"])
async def test_missing_ambiguous_or_option_input_does_not_dispatch(monkeypatch, reference):
    async def query(*argv):
        pytest.fail("invalid input must not reach Docker")

    monkeypatch.setattr(choices, "_query", query)
    with pytest.raises(choices.DockerChoiceError):
        await choices.resolve_image("engine", reference)


async def test_query_cancellation_waits_for_started_process_and_does_not_expose_stderr(monkeypatch):
    original = subprocess.Popen
    ready = threading.Event()
    processes = []

    def start(argv, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        ready.set()
        return process

    monkeypatch.setattr(choices.subprocess, "Popen", start)
    task = asyncio.create_task(choices.contexts())
    assert await asyncio.to_thread(ready.wait, 5)
    assert processes[0].poll() is None
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("mode", ["failed", "timeout", "oversize", "missing"])
async def test_cli_errors_are_bounded_and_safe(monkeypatch, mode):
    original = subprocess.Popen
    processes = []

    def start(argv, **kwargs):
        if mode == "missing":
            raise FileNotFoundError("synthetic-private-diagnostic")
        code = {
            "failed": "import sys; sys.stderr.write('synthetic-private-diagnostic'); sys.exit(1)",
            "timeout": "import time; time.sleep(60)",
            "oversize": "print('x'*2048)",
        }[mode]
        process = original([sys.executable, "-c", code], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(choices.subprocess, "Popen", start)
    monkeypatch.setattr(choices, "QUERY_SECONDS", 0.2)
    monkeypatch.setattr(choices, "MAX_QUERY_BYTES", 1024)
    with pytest.raises(choices.DockerChoiceError) as error:
        await choices.contexts()
    assert "synthetic-private-diagnostic" not in str(error.value)
    assert all(process.poll() is not None for process in processes)


@pytest.mark.skipif(not REAL_CONTEXT or not REAL_IMAGE, reason="explicit local Docker selection")
async def test_real_local_docker_choices_resolve_the_selected_existing_image():
    assert REAL_CONTEXT in {value for _, value in await choices.contexts()}
    options = {value: label for label, value in await choices.images(REAL_CONTEXT)}
    assert REAL_IMAGE in options
    assert await choices.resolve_image(REAL_CONTEXT, REAL_IMAGE) == REAL_IMAGE
    label = options[REAL_IMAGE].split(" · ")[0].split(" / ")[0]
    if label != "未命名镜像":
        assert await choices.resolve_image(REAL_CONTEXT, label) == REAL_IMAGE
