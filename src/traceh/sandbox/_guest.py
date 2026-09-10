"""Trusted Linux PID-1 controller, launched only by the Docker backend.

The workload runs under a different UID and cannot stop the deadline owner.
This module is supplied as a script to the guest, never imported by the host.
"""

import base64
import json
import os
import resource
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath

UID = 65532
streams = [bytearray(), bytearray()]
stream_eof = [False, False]
started = time.monotonic()
request = json.loads(Path("/request.json").read_text())
limit = request["output_bytes"]
root = Path("/workspace")


def valid(name):
    path = PurePosixPath(name)
    return (
        name == path.as_posix()
        and not path.is_absolute()
        and all(p not in ("", ".", "..", ".git", ".traceh") for p in path.parts)
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
    )


def finish(status, code=None):
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.setitimer(signal.ITIMER_REAL, 5)
    # PID 1 is excluded by kill(-1); no process can change to its root identity.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            break
    # Workload descendants may inherit stdout/stderr after their parent exits.
    # Kill/reap them first, then retain the already-written bounded pipe tail.
    # EOF is not the lifecycle owner: the direct command's exit is.
    if "process" in globals():
        for index, pipe in enumerate((process.stdout, process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            while True:
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                room = limit - len(streams[index])
                streams[index].extend(chunk[:room])
                if len(chunk) > room:
                    if status == "finished":
                        status = "output-exceeded"
                    break
    output_files = {}
    total = 0
    nodes = 0
    executables = []
    directories = []
    try:
        for path in sorted(root.rglob("*")) if request["export_workspace"] else ():
            nodes += 1
            if nodes > request["workspace_files"]:
                raise ValueError("workspace-limit")
            info = path.lstat()
            mode = info.st_mode
            name = path.relative_to(root).as_posix()
            if not valid(name) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("unsafe-workspace")
            if stat.S_ISDIR(mode):
                directories.append(name)
            if stat.S_ISREG(mode):
                if info.st_nlink != 1:
                    raise ValueError("unsafe-workspace")
                if mode & stat.S_IXUSR:
                    executables.append(name)
                total += info.st_size
                if (
                    total > request["workspace_bytes"]
                    or len(output_files) >= request["workspace_files"]
                ):
                    raise ValueError("workspace-limit")
                output_files[name] = base64.b64encode(path.read_bytes()).decode()
    except (ValueError, OSError) as exc:
        status = str(exc) if isinstance(exc, ValueError) else "workspace-unreadable"
        output_files = {}
        directories = []
    result = {
        "status": status,
        "exit_code": code,
        "stdout": base64.b64encode(streams[0]).decode(),
        "stderr": base64.b64encode(streams[1]).decode(),
        "files": output_files,
        "executables": executables,
        "directories": directories,
        "elapsed": time.monotonic() - started,
        "cgroup": {
            name: Path("/sys/fs/cgroup", name).read_text()
            for name in ["cpu.max", "cpu.stat", "memory.max", "memory.events", "pids.max"]
        },
    }
    print(json.dumps(result), flush=True)
    os._exit(0)


signal.signal(signal.SIGALRM, lambda *_: finish("timed-out"))
signal.setitimer(signal.ITIMER_REAL, request["wall_seconds"])
os.environ.clear()
os.chown(root, UID, UID)
for name in request["directories"]:
    assert valid(name)
    (root / name).mkdir(parents=True, exist_ok=True)
for name, encoded in request["files"].items():
    assert valid(name)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(encoded, validate=True))
    path.chmod(0o755 if name in request["executables"] else 0o644)
for path in root.rglob("*"):
    os.chown(path, UID, UID)


def restrict():
    os.setgroups([])
    os.setgid(UID)
    os.setuid(UID)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (request["workspace_bytes"],) * 2)


try:
    process = subprocess.Popen(
        request["argv"],
        cwd=root / request["cwd"],
        env=request["environment"],
        stdin=subprocess.PIPE if request["stdio"] else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=restrict,
    )
except (ValueError, OSError):
    finish("start-failed")
if request["stdio"]:
    GuestStdio(process, streams, stream_eof, request["stdio"])  # noqa: F821 -- trusted prelude
poll = selectors.DefaultSelector()
for i, pipe in enumerate((process.stdout, process.stderr)):
    os.set_blocking(pipe.fileno(), False)
    poll.register(pipe, selectors.EVENT_READ, i)
while poll.get_map():
    for key, _ in poll.select(0.05):
        chunk = os.read(key.fd, 65536)
        if not chunk:
            stream_eof[key.data] = True
            poll.unregister(key.fileobj)
            continue
        room = limit - len(streams[key.data])
        streams[key.data].extend(chunk[:room])
        if len(chunk) > room:
            finish("output-exceeded")
    if process.poll() is not None:
        finish("finished", process.returncode)
finish("finished", process.wait())
