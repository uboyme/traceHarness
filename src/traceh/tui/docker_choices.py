"""Read-only Docker discovery for the settings UI, never a workload executor."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import threading
import time

from traceh.concurrency import await_worker_convergence

QUERY_SECONDS = 10.0
MAX_QUERY_BYTES = 1024 * 1024
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


class DockerChoiceError(ValueError):
    """Only fixed, user-facing diagnostics; no raw Docker stderr."""


def _query_sync(argv: tuple[str, ...], cancel: threading.Event) -> str:
    # Docker Desktop helpers may inherit pipe handles. Files let us await the
    # CLI itself instead of waiting forever for inherited stdout/stderr EOF.
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                ["docker", *argv], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise DockerChoiceError("无法启动 Docker 命令，请检查 Docker 是否已安装。") from None
        deadline = time.monotonic() + QUERY_SECONDS
        try:
            while process.poll() is None:
                if cancel.is_set():
                    raise DockerChoiceError("已取消 Docker 查询。")
                if time.monotonic() >= deadline:
                    raise DockerChoiceError("Docker 查询超时，请检查 Docker Desktop 和所选连接。")
                if output.tell() > MAX_QUERY_BYTES:
                    raise DockerChoiceError("Docker 列表过大，请手动填写具体镜像。")
                cancel.wait(0.05)
            if process.returncode:
                raise DockerChoiceError(
                    "Docker 查询失败：请检查服务、连接名称和本机镜像；不会自动下载镜像。"
                )
            output.seek(0)
            data = output.read(MAX_QUERY_BYTES + 1)
            if len(data) > MAX_QUERY_BYTES:
                raise DockerChoiceError("Docker 列表过大，请手动填写具体镜像。")
            try:
                return data.decode("utf-8", "strict")
            except UnicodeError:
                raise DockerChoiceError("Docker 返回的文字编码无效。") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


async def _query(*argv: str) -> str:
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_query_sync, argv, cancel))
    try:
        return await asyncio.shield(task)
    except BaseException:
        cancel.set()
        await await_worker_convergence(task)
        raise


def _name(value: str, label: str) -> str:
    value = value.strip()
    if not value or value.startswith("-") or any(c.isspace() or ord(c) < 32 for c in value):
        raise DockerChoiceError(f"请明确填写{label}，不能留空或包含空白字符。")
    return value


def _json_lines(text: str) -> list:
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        raise DockerChoiceError("Docker 返回的列表格式无效，请刷新重试。") from None


async def contexts() -> list[tuple[str, str]]:
    rows = _json_lines(await _query("context", "ls", "--format", "{{json .Name}}"))
    if any(not isinstance(row, str) or not row for row in rows):
        raise DockerChoiceError("Docker 返回的连接列表格式无效。")
    return [(name, name) for name in sorted(set(rows))]


async def images(context: str) -> list[tuple[str, str]]:
    context = _name(context, "Docker 连接名称")
    rows = _json_lines(await _query(
        "--context", context, "image", "ls", "--no-trunc", "--format", "{{json .}}",
    ))
    labels: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not IMAGE_ID.fullmatch(str(row.get("ID", ""))):
            raise DockerChoiceError("Docker 返回的镜像列表格式无效。")
        identity = row["ID"]
        names = labels.setdefault(identity, set())
        repo, tag = row.get("Repository"), row.get("Tag")
        if isinstance(repo, str) and repo and repo != "<none>":
            names.add(f"{repo}:{tag}" if tag and tag != "<none>" else repo)
    # A dropdown value is the observed content identity, never a mutable tag.
    return sorted(
        ((f"{' / '.join(sorted(names)) or '未命名镜像'} · {identity[7:19]}", identity)
         for identity, names in labels.items()),
        key=lambda item: item[0],
    )


async def resolve_image(context: str, reference: str) -> str:
    context = _name(context, "Docker 连接名称")
    reference = _name(reference, "镜像名称、标签或完整 ID")
    rows = _json_lines(await _query(
        "--context", context, "image", "inspect", "--format",
        '{"id":{{json .Id}},"os":{{json .Os}},"volumes":{{json (index .Config "Volumes")}}}',
        "--", reference,
    ))
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise DockerChoiceError("无法唯一确定镜像，请填写完整名称或 ID。")
    row = rows[0]
    identity = row.get("id")
    if not isinstance(identity, str) or not IMAGE_ID.fullmatch(identity):
        raise DockerChoiceError("Docker 未返回完整镜像身份。")
    if IMAGE_ID.fullmatch(reference) and reference != identity:
        raise DockerChoiceError("镜像身份不一致，请刷新后重新选择。")
    if row.get("os") != "linux" or row.get("volumes"):
        raise DockerChoiceError("当前沙箱要求 Linux 镜像，且不能声明自动挂载卷。")
    return identity
