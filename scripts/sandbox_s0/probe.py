"""Explicit S0 real Docker experiments; does not run any project test suite."""

import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--image", required=True, help="Explicit local sha256 image ID; Python 3.12 required"
)
parser.add_argument("--context", required=True, help="Explicit Docker context")
parser.add_argument("--output", required=True, type=Path, help="New experiment evidence directory")
args = parser.parse_args()
if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
    parser.error("image must be a pinned sha256 image ID")
DOCKER_CONTEXT = args.context
IMAGE = args.image
GUEST = (ROOT / "guest.py").read_text("utf-8")
RESULTS = []
RUN_ROOT = args.output.resolve()
RUN_ROOT.mkdir(parents=True, exist_ok=False)


def cli(*args, timeout=20):
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            ["docker", "--context", DOCKER_CONTEXT, *args], stdout=output, stderr=output
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("docker-cli-timeout") from None
        output.seek(0)
        text = output.read(2_000_000).decode("utf-8", "replace")
        if process.returncode:
            raise RuntimeError(f"docker-{args[0]}-failed: {text[:500]}")
        return text


def probe(name, code, *, wall=4, mode="normal"):
    directory = RUN_ROOT / name
    directory.mkdir()
    request = {
        "argv": ["python", "-c", code],
        "files": {"allowed.txt": base64.b64encode(b"allowed-input").decode()},
        "environment": {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        "wall_seconds": wall,
        "output_bytes": 4096,
        "workspace_bytes": 8_388_608,
    }
    control = directory / "request.json"
    control.write_text(json.dumps(request), "utf-8")
    container = "traceh-s0-" + uuid.uuid4().hex
    created = False
    try:
        identity = cli(
            "create",
            "--name",
            container,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "KILL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "0:0",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--cpus",
            "0.5",
            "--restart",
            "no",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=32m",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--mount",
            f"type=bind,source={control},target=/request.json,readonly",
            "--label",
            "traceh.s0=probe",
            "--entrypoint",
            "python",
            IMAGE,
            "-I",
            "-c",
            GUEST,
        ).strip()
        created = True
        configuration = json.loads(cli("inspect", container))[0]
        assert configuration["Image"] == IMAGE
        assert configuration["HostConfig"]["NetworkMode"] == "none"
        assert configuration["HostConfig"]["ReadonlyRootfs"]
        if mode == "normal":
            output = cli("start", "--attach", container, timeout=wall + 15)
        else:
            # The start CLI exits before the workload: no live host controller
            # is needed for the guest supervisor's deadline to remain effective.
            cli("start", container)
            if mode == "cancel":
                until = time.monotonic() + 8
                while True:
                    top = cli("top", container, "-eo", "pid,uid")
                    if "65532" in top:
                        break
                    if time.monotonic() > until:
                        raise AssertionError("workload never started")
                cli("kill", container)
            until = time.monotonic() + wall + 8
            while json.loads(cli("inspect", container))[0]["State"]["Running"]:
                if time.monotonic() > until:
                    raise AssertionError("guest outlived deadline")
            output = cli("logs", container)
        state = json.loads(cli("inspect", container))[0]["State"]
        assert not state["Running"] and state["Pid"] == 0
        result = json.loads(output) if output.strip() else None
        if result:
            result["stdout_text"] = base64.b64decode(result["stdout"]).decode("utf-8", "replace")
            result["stderr_text"] = base64.b64decode(result["stderr"]).decode("utf-8", "replace")
        record = {
            "case": name,
            "container": identity,
            "mode": mode,
            "state": {k: state[k] for k in ["Status", "Running", "Pid", "ExitCode", "OOMKilled"]},
            "result": result,
        }
        (directory / "result.json").write_text(json.dumps(record, indent=2), "utf-8")
        RESULTS.append(record)
        print(
            name,
            state["ExitCode"],
            None if result is None else result["status"],
            None if result is None else result["exit_code"],
            flush=True,
        )
        return result, state
    finally:
        if created:
            cli("rm", "-f", container)
        (RUN_ROOT / "results.json").write_text(json.dumps(RESULTS, indent=2), "utf-8")


os.environ["TRACEH_S0_PRIVATE_TOKEN"] = "synthetic-never-inherit"
r, _ = probe(
    "basic",
    (
        "from pathlib import Path; import os; assert os.getuid()==65532; asse"
        "rt 'TRACEH_S0_PRIVATE_TOKEN' not in os.environ; assert Path('allowed"
        ".txt').read_text()=='allowed-input'; Path('changed.txt').write_text("
        "'中文结果'); print('basic-ok')"
    ),
)
assert r["exit_code"] == 0 and "changed.txt" in r["files"]
r, _ = probe(
    "filesystem",
    (
        "from pathlib import Path; import os; assert not Path('/var/run/docke"
        "r.sock').exists(); assert not Path('/mnt/c/Users').exists();\ntry: Pa"
        "th('/etc/traceh-escape').write_text('x')\nexcept OSError: print('root"
        "-write-denied')\nelse: raise AssertionError('root writable')"
    ),
)
assert r["exit_code"] == 0 and "root-write-denied" in r["stdout_text"]
r, _ = probe(
    "network",
    (
        "import socket;\ntry: socket.create_connection(('1.1.1.1',443),0.5)\nex"
        "cept OSError: print('network-denied')\nelse: raise AssertionError('ne"
        "twork escaped')"
    ),
)
assert r["exit_code"] == 0
r, _ = probe(
    "supervisor-protection",
    (
        "import os,signal;\ntry: os.kill(1,signal.SIGSTOP)\nexcept PermissionEr"
        "ror: print('supervisor-protected')\nelse: raise AssertionError('deadl"
        "ine can be stopped')"
    ),
)
assert r["exit_code"] == 0
r, _ = probe(
    "pids",
    (
        "import os,signal,errno; children=[]\ntry:\n for _ in range(80):\n  pid="
        "os.fork()\n  if pid==0: signal.pause(); os._exit(0)\n  children.append"
        "(pid)\nexcept OSError as e:\n assert e.errno==errno.EAGAIN; print('pid"
        "s-limited',len(children),flush=True)\nelse: raise AssertionError('pid"
        "s unlimited')\nfinally:\n for pid in children: os.kill(pid,signal.SIGK"
        "ILL)\n for pid in children: os.waitpid(pid,0)"
    ),
)
assert r["exit_code"] == 0 and "pids-limited" in r["stdout_text"]
r, _ = probe("output-flood", "import os;\nwhile True: os.write(1,b'x'*65536)")
assert r["status"] == "output-exceeded" and len(base64.b64decode(r["stdout"])) == 4096
r, state = probe("memory", "data=bytearray(256*1024*1024); print(len(data))")
assert state["OOMKilled"] or (
    r and r["exit_code"] == -9 and "oom_kill 1" in r["cgroup"]["memory.events"]
)
r, _ = probe("symlink-output", "import os; os.symlink('/request.json','stolen')")
assert r["status"] == "unsafe-workspace" and not r["files"]
r, _ = probe(
    "background-child",
    (
        "import subprocess; subprocess.Popen(['python','-c','while True: pass"
        "'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subproces"
        "s.DEVNULL); print('parent-complete')"
    ),
)
assert r["exit_code"] == 0
r, _ = probe(
    "controller-exit",
    (
        "import subprocess; subprocess.Popen(['python','-c','while True: pass"
        "'],start_new_session=True);\nwhile True: pass"
    ),
    wall=2,
    mode="detach",
)
assert r["status"] == "timed-out"
assert any(
    line.startswith("nr_throttled ") and int(line.split()[1]) > 0
    for line in r["cgroup"]["cpu.stat"].splitlines()
)
r, state = probe("cancel", "while True: pass", wall=20, mode="cancel")
assert state["ExitCode"] == 137
r, _ = probe(
    "workspace-byte-limit",
    "from pathlib import Path; Path('large').write_bytes(b'x' * (8*1024*1024))",
)
assert r["status"] == "workspace-limit" and not r["files"]
r, _ = probe(
    "workspace-file-limit", "from pathlib import Path; [Path(str(i)).touch() for i in range(129)]"
)
assert r["status"] == "workspace-limit" and not r["files"]
print("S0 real probes passed:", len(RESULTS), flush=True)
