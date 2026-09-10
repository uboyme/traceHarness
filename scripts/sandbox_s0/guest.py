"""S0 experimental PID-1 supervisor; not production code."""

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
    signal.alarm(0)
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
    output_files = {}
    total = 0
    try:
        for path in sorted(root.rglob("*")):
            mode = path.lstat().st_mode
            name = path.relative_to(root).as_posix()
            if not valid(name) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("unsafe-workspace")
            if stat.S_ISREG(mode):
                total += path.stat().st_size
                if total > request["workspace_bytes"] or len(output_files) >= 128:
                    raise ValueError("workspace-limit")
                output_files[name] = base64.b64encode(path.read_bytes()).decode()
    except ValueError as exc:
        status = str(exc)
        output_files = {}
    result = {
        "status": status,
        "exit_code": code,
        "stdout": base64.b64encode(streams[0]).decode(),
        "stderr": base64.b64encode(streams[1]).decode(),
        "files": output_files,
        "elapsed": time.monotonic() - started,
        "cgroup": {
            name: Path("/sys/fs/cgroup", name).read_text()
            for name in ["cpu.max", "cpu.stat", "memory.max", "memory.events", "pids.max"]
        },
    }
    print(json.dumps(result), flush=True)
    os._exit(0)


signal.signal(signal.SIGALRM, lambda *_: finish("timed-out"))
signal.alarm(request["wall_seconds"])
os.environ.clear()
os.chown(root, UID, UID)
for name, encoded in request["files"].items():
    assert valid(name)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(encoded, validate=True))
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
        cwd=root,
        env=request["environment"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=restrict,
    )
except (ValueError, OSError):
    finish("start-failed")
poll = selectors.DefaultSelector()
for i, pipe in enumerate((process.stdout, process.stderr)):
    os.set_blocking(pipe.fileno(), False)
    poll.register(pipe, selectors.EVENT_READ, i)
while poll.get_map():
    for key, _ in poll.select(0.05):
        chunk = os.read(key.fd, 65536)
        if not chunk:
            poll.unregister(key.fileobj)
            continue
        room = limit - len(streams[key.data])
        streams[key.data].extend(chunk[:room])
        if len(chunk) > room:
            finish("output-exceeded")
finish("finished", process.wait())
