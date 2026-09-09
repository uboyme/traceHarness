"""Shared Line/TUI wording; never print request bodies or exception text."""

from traceh.chat.context_pressure import ContextPressureView


def context_pressure_text(view: ContextPressureView | None) -> str:
    title = "上下文空间不足，本次请求未发送给模型。"
    if view is None:
        return title + "\n无法核对本次占用明细；原失败已保留。请检查上下文记录或调整配置后再试。"
    parts = dict(view.parts)
    maintenance = dict(view.maintenance)
    lines = [
        title,
        f"本地估算输入 {view.input_tokens} token，允许 {view.input_limit}，"
        f"超出 {view.input_tokens - view.input_limit}；"
        f"另预留输出 {view.output_reserve_tokens}、安全余量 {view.safety_margin_tokens}。",
        f"占用：对话与工具结果 {parts['conversation']}；"
        f"参考与当前问题回显 {parts['references_and_current_request']}；"
        f"任务状态 {parts['product']}；系统说明 {parts['system']}；"
        f"工具定义 {parts['tools']}；请求封装 {parts['envelope']}。",
        f"本轮维护：旧工具折叠 {maintenance['tool-fold']} 次，"
        f"旧历史摘录 {maintenance['automatic']} 次，语义摘要 {maintenance['semantic']} 次；"
        f"维护失败 {view.maintenance_failures} 次；"
        f"本次记录的参考 token 排除 {view.reference_exclusions} 项。",
    ]
    if view.summary_request:
        lines.append("本次被拦截的是摘要请求，旧历史没有因这次请求被替换。")
    lines.extend(
        [
            "可缩短当前输入；在配置中按模型实际容量调整 Token 窗口、输出预留及自动压缩。"
            "本轮工具结果过大时，可在新一轮改问具体片段，或新建会话继续。",
            "原日志和已执行工具结果仍保留；不会自动重跑工具。估算不等于模型实际计费。",
        ]
    )
    return "\n".join(lines)
