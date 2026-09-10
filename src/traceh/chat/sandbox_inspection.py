"""Read-only human view of the original sandbox receipts; no backend actions."""

from dataclasses import asdict

from traceh.api.sandbox import SandboxPolicy
from traceh.cli.command_line import escape_for_display
from traceh.sandbox.reader import read_execution_record
from traceh.session.event_store import EventStore
from traceh.session.service import SessionService


def _display(value):
    return escape_for_display(str(value))


def _policy_lines(policy):
    limits = policy["limits"]
    return [
        f"Docker 连接：{_display(policy['docker_context'])}",
        f"固定镜像：{_display(policy['image'])}",
        f"网络：{_display(policy['network'])}（none 表示关闭）",
        "读入范围：" + _display(", ".join(policy["read_paths"]) or "无"),
        "回写范围：" + _display(", ".join(policy["write_paths"]) or "无"),
        "额外排除：" + _display(", ".join(policy["excluded_paths"]) or "无"),
        "始终排除：.git、.traceh、.ssh、.aws、.env* 及链接／特殊节点",
        f"资源上限：内存 {limits['memory_bytes']} 字节；CPU {limits['cpus']} 核；"
        f"进程／线程 {limits['pids']}；时限 {limits['wall_seconds']} 秒",
        f"文件上限：{limits['workspace_bytes']} 字节／{limits['workspace_files']} 节点；"
        f"每路输出上限：{limits['output_bytes']} 字节",
    ]


async def sandbox_report(
    store: EventStore, *, session_id: str, policy: SandboxPolicy | None, limit: int = 20
) -> str:
    """Display selected policy separately from historical actual execution policies."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("sandbox-inspection-limit-invalid")
    lines = ["执行沙箱", "当前会话：" + _display(session_id), "", "本次运行配置："]
    if policy is None:
        lines.append("未启用：进程命令会被拒绝；不会回退到宿主执行。")
    else:
        lines.extend(_policy_lines(asdict(policy)))
        lines.append("上面是选定策略；实际后端是否可用，以执行回执为准。")
    stream_id = SessionService.effect_stream(session_id)
    events = await store.read(stream_id)
    requests = [e for e in events if e.type == "sandbox/request"]
    requests.extend(
        e for e in await store.read(SessionService.session_stream(session_id))
        if e.type == "sandbox/request"
    )
    for activation_stream in await store.list_streams(prefix="plugin-activation:"):
        requests.extend(
            e for e in await store.read(activation_stream) if e.type == "sandbox/request"
        )
    requests.sort(key=lambda event: (event.occurred_at, event.stream_id, event.seq))
    lines.append("历史范围：当前会话执行，以及本宿主账本中的应用级插件进程；插件进程不属于单个会话。")
    lines += ["", f"历史执行：共 {len(requests)} 次，显示最近 {min(limit, len(requests))} 次。"]
    for request in requests[-limit:]:
        view = await read_execution_record(
            store, stream_id=request.stream_id, execution_id=request.data["execution_id"]
        )
        lines += ["", "执行 ID：" + _display(view.request["execution_id"])]
        owner = view.request["owner"]
        lines.append(f"原始所有者：{_display(owner['kind'])} / {_display(owner['owner_id'])}")
        for field, label in (("agent_id", "Agent"), ("turn_id", "轮次"),
                             ("tool_call_id", "工具调用"), ("plugin_id", "插件"),
                             ("plugin_version", "插件版本"), ("activation_id", "激活实例")):
            if owner.get(field):
                lines.append(label + "：" + _display(owner[field]))
        lines.extend(_policy_lines(view.request["policy"]))
        stdio = view.request.get("stdio")
        if stdio is not None:
            lines.append(f"标准输入总量：{stdio['input_bytes']} 字节；"
                         f"单次传输：{stdio['frame_bytes']} 字节。")
        if view.outcome is None:
            lines.append("结果：只有请求记录；是否启动、是否已收尾尚未确认。")
            continue
        outcome = view.outcome
        backend = outcome.get("backend")
        actual_backend = (
            f"Docker Engine {backend['Version']} / API {backend['ApiVersion']} / "
            f"{backend['Os']} {backend['Arch']}" if backend else "未取得后端信息"
        )
        lines += [
            "结束状态：" + _display(outcome["status"]),
            "执行资源收敛：" + ("已确认" if outcome["converged"] else "未确认"),
            "实际后端：" + _display(actual_backend),
            "回执身份：" + _display(outcome["digest"]),
        ]
        if outcome.get("started_at"):
            lines.append("后端确认的启动时间：" + _display(outcome["started_at"]))
        if outcome.get("finished_at"):
            lines.append("后端确认的结束时间：" + _display(outcome["finished_at"]))
        meanings = {
            "finished": "命令已结束；这不等于验证通过，退出码由原工具／验证结果解释。",
            "timed-out": "命令达到执行时限，已要求终止。",
            "cancelled": "调用方取消了这次执行。",
            "output-exceeded": "标准输出或错误超过额度，已要求终止。",
            "unknown-convergence": "无法确认执行资源已经收尾，不能自动重试。",
            "backend-unavailable": "后端不可用，未成功启动工作负载。",
            "execution-failed": "执行或结果读取失败；具体原因见下面的失败代码。",
            "start-failed": "容器内命令未能启动，请检查镜像中的命令和工作目录。",
            "cleanup-failed": "工作负载已收尾，但主机控制文件清理失败。",
            "unsafe-workspace": "输出包含不允许的文件类型或路径，未导出文件。",
            "workspace-limit": "输出文件超过数量或字节上限，未导出文件。",
            "workspace-unreadable": "无法读取执行后的工作区，未导出文件。",
        }
        if outcome["status"] in meanings:
            lines.append("状态说明：" + meanings[outcome["status"]])
        if outcome.get("failure_code"):
            lines.append("失败原因：" + _display(outcome["failure_code"]))
        if outcome.get("cleanup_failures"):
            lines.append("清理失败：" + _display(", ".join(outcome["cleanup_failures"])))
        if view.publication is not None:
            publication = view.publication
            lines.append("回写状态：" + _display(publication["status"]))
            lines.append(f"已应用文件操作：{len(publication['applied'])} 项")
            if publication.get("failure_code"):
                lines.append("回写失败原因：" + _display(publication["failure_code"]))
            lines.append("回写回执身份：" + _display(publication["digest"]))
        elif view.request["export_workspace"]:
            lines.append("回写：无回写记录，不能据此断言宿主文件已更新。")
        else:
            lines.append("回写：验证副本不回写。")
    lines += ["", "仅核对原事件及回执关联；不读取输出正文，不运行命令，不重新执行。"]
    return "\n".join(lines)
