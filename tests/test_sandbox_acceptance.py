"""Production backend acceptance; no prototype, model API or full-suite runner."""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, replace

from tests.sandbox_fixtures import real_sandbox_service
from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.sandbox.reader import read_execution
from traceh.session.event_store import InMemoryEventStore
from traceh.session.sqlite import SqliteEventStore


async def execute(tmp_path, script):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SYNTHETIC_SECRET=must-not-copy")
    (workspace / "allowed.txt").write_text("authorized")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    service = real_sandbox_service(store, cas)
    service.policy = replace(service.policy, limits=replace(
        service.policy.limits, cpus=0.25, pids=16, wall_seconds=10,
    ))
    async with service.scope(
        SandboxOwner("verification", "acceptance"), stream_id="acceptance",
        workspace=workspace, data_dir=None, publish_changes=False,
    ) as scope:
        result = await scope.run(("python", "-u", "-c", script), timeout_seconds=10)
    events = await store.read("acceptance")
    view = await read_execution(
        store, cas, stream_id="acceptance", execution_id=events[0].data["execution_id"]
    )
    assert result.receipt["converged"] is True
    return result, view.result["payload"]


async def test_real_production_network_filesystem_and_credentials_are_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_HOST_CREDENTIAL", "must-not-inherit")
    result, _ = await execute(tmp_path, """
import os,socket
from pathlib import Path
assert Path('allowed.txt').read_text() == 'authorized'
assert not Path('.env').exists()
assert 'SYNTHETIC_HOST_CREDENTIAL' not in os.environ
assert os.getuid() == 65532
assert not Path('/var/run/docker.sock').exists()
try: Path('/denied-write').write_text('escape')
except OSError: pass
else: raise AssertionError('root filesystem writable')
try:
    s=socket.socket();s.settimeout(0.3);s.connect(('198.51.100.1',443))
except OSError: pass
else: raise AssertionError('network escaped')
print('actual-denials-confirmed')
""")
    assert result.status == "finished" and result.exit_code == 0
    assert result.stdout == b"actual-denials-confirmed\n"


async def test_real_production_cpu_and_fork_limit(tmp_path):
    result, payload = await execute(tmp_path, """
import os,time,errno
deadline=time.monotonic()+0.6
while time.monotonic()<deadline: pass
children=0
while True:
    try: pid=os.fork()
    except OSError as error:
        assert error.errno==errno.EAGAIN
        print('fork-denied',children,flush=True)
        break
    if pid==0:
        while True: pass
    children+=1
assert children>0
""")
    assert result.status == "finished" and result.exit_code == 0
    assert result.stdout.startswith(b"fork-denied ")
    values = dict(line.split() for line in payload["cgroup"]["cpu.stat"].splitlines())
    assert int(values["nr_throttled"]) > 0
    assert payload["cgroup"]["cpu.max"].strip() == "25000 100000"
    assert payload["cgroup"]["pids.max"].strip() == "16"


async def test_real_production_memory_limit_records_oom(tmp_path):
    result, payload = await execute(
        tmp_path, "print('before-allocation',flush=True); payload=bytearray(256*1024*1024)"
    )
    assert result.stdout == b"before-allocation\n"
    assert result.exit_code != 0
    values = dict(line.split() for line in payload["cgroup"]["memory.events"].splitlines())
    assert int(values["oom_kill"]) >= 1


async def test_real_host_crash_leaves_unknown_receipt_but_guest_deadline_converges(tmp_path):
    from tests.sandbox_fixtures import real_sandbox_policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = real_sandbox_policy()
    config = tmp_path / "policy.json"
    config.write_text(json.dumps(dict(format=2, policy=asdict(policy), plugin_grants=[])))
    child = tmp_path / "host.py"
    child.write_text('''
import asyncio,sys
from pathlib import Path
from traceh.api.sandbox import SandboxConfiguration,SandboxOwner,SandboxStdioLimits
from traceh.sandbox.config import load_sandbox_file
from traceh.sandbox.service import build_sandbox_service
from traceh.session.sqlite import SqliteEventStore
async def run():
    root=Path(sys.argv[1])
    store=SqliteEventStore(root/'events')
    service=build_sandbox_service(store,SandboxConfiguration(
        load_sandbox_file(root/'policy.json').policy,root/'cas'))
    async with service.scope(SandboxOwner('verification','host-crash'),
            stream_id='host-crash',workspace=root/'workspace',data_dir=None,
            publish_changes=False) as scope:
        process=await scope.open_process(('python','-u','-c',
            "import sys,subprocess;subprocess.Popen(['python','-c','while True: pass'],"
            "start_new_session=True);print('guest-ready',flush=True);sys.stdin.read()"),
            timeout_seconds=5,stdio=SandboxStdioLimits(4096,1024))
        assert await process.read(1024)==b'guest-ready\\n'
        print('host-ready',flush=True)
        await asyncio.Event().wait()
asyncio.run(run())
''', encoding="utf-8")

    def crash():
        with tempfile.TemporaryFile() as output:
            host = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(child), str(tmp_path)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    output.seek(0)
                    if b"host-ready" in output.read():
                        host.kill()  # Deliberately bypass every host cleanup/finalizer.
                        host.wait(timeout=5)
                        return
                    assert host.poll() is None, "host ended before real server acknowledgement"
                    time.sleep(0.02)
                raise AssertionError("host did not acknowledge its real connection")
            finally:
                if host.poll() is None:
                    host.kill()
                    host.wait(timeout=5)

    store = SqliteEventStore(tmp_path / "events")
    try:
        await asyncio.to_thread(crash)
        events = await store.read("host-crash")
        assert [e.type for e in events] == ["sandbox/request"]
        request = events[0].data

        def inspect_and_wait():
            name = "traceh-exec-" + request["execution_id"]
            argv = ["docker", "--context", policy.docker_context]
            with tempfile.TemporaryFile() as output:
                waited = subprocess.run(argv + ["wait", name], stdout=output,
                                        stderr=subprocess.DEVNULL, timeout=20)
                assert waited.returncode == 0
                output.seek(0)
                output.truncate()
                checked = subprocess.run(argv + ["inspect", name], stdout=output,
                                         stderr=subprocess.DEVNULL, timeout=10)
                assert checked.returncode == 0
                output.seek(0)
                state = json.load(output)[0]["State"]
                assert not state["Running"] and state["Pid"] == 0
        await asyncio.to_thread(inspect_and_wait)
        view = await read_execution(
            store, LocalArtifactCas(tmp_path / "cas"), stream_id="host-crash",
            execution_id=request["execution_id"],
        )
        assert view.outcome is None  # A stopped container is not a fabricated terminal event.
    finally:
        # Test cleanup is explicit and checks this request's exact label first;
        # production does not pretend hard-crash orphan reconciliation exists.
        events = await store.read("host-crash")
        await store.aclose()
        for event in events:
            if event.type != "sandbox/request":
                continue
            identity = event.data["execution_id"]

            def cleanup(identity=identity):
                argv = ["docker", "--context", policy.docker_context]
                name = "traceh-exec-" + identity
                with tempfile.TemporaryFile() as output:
                    checked = subprocess.run(argv + ["inspect", name], stdout=output,
                                             stderr=subprocess.DEVNULL, timeout=10)
                    if checked.returncode:
                        return
                    output.seek(0)
                    container = json.load(output)[0]
                    assert container["Config"]["Labels"]["traceh.execution"] == identity
                    subprocess.run(argv + ["rm", "--force", container["Id"]], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            await asyncio.to_thread(cleanup)
