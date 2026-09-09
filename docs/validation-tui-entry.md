# 一条命令启动与 TUI 应用配置：定向验证

日期：2026-09-08。本轮实现裸 `traceh`、个人连接默认、当前目录启动和 F2 应用配置。
这属于 Stop C 之后的用户体验工作，不改变 Session/Context 协议，不把旧 Stop C 审查当作本轮审查。

## 已验证结果

使用用户日常 Anaconda Python 3.12.7 和已安装的 Textual 8.2.8，直接运行以下七个相关文件：

```powershell
python -m pytest tests/test_tui_settings.py tests/test_tui_optional.py tests/test_cli_context_config.py tests/test_cli_env.py tests/test_cli_chat.py tests/test_tui.py tests/test_tui_governance.py --tb=short --durations=8
```

最终 **138 passed in 69.35s**，无 skip。配置面板文件现含 27 项检查。

- 裸 `main([])` 从当前目录启动，配置有效时跳过启动表单；无连接时进入待配置界面，输入禁用，
  不创建模型请求或用 scripted 假回答替代。原 Line、可选依赖与 CLI 环境解析回归通过。
- 个人默认不携带 workspace、Session、存储或插件选择；项目配置覆盖个人连接，CLI 显式值优先。
  个人环境文件只发布连接字段和所选密钥；项目/CLI 显式环境文件仍可提供完整宿主设置。
- 真实 headless TUI 输入 → 原 ChatDriver → 内置 Provider → 本机 HTTP → 原 SQLite 账本路径通过。
  两个 Runtime 使用同一 Session，模型由前一值切换到后一值；进入新 Runtime 前旧 Runtime 已 disposed。
  两轮请求通过原 `verify_request_snapshots()` 重建核查。
- 更换合成环境文件后，后一个 HTTP 请求携带新合成密钥，重试参数也重新解析；关闭后原进程环境恢复。
  清空 Context 路径再应用不会复用旧解析 DTO。密钥不写 profile 或事件。
- 本地 preflight 使用私有环境映射；配置解析失败不产生部分进程环境写入。cleanup 失败不会返回
  可继续执行的 RestartChat 意图。原 Product 审批、取消、TUI 退出收敛和治理回归通过。
- 聊天换行改由 ConversationLog 的实际尺寸变化触发，双栏切换后的行宽、前缀、原文完整性与横向
  滚动约束通过原断言检查。

## 失败记录与反向验证

初轮相关组 `137 passed, 1 failed`：新测试在 Screen.dismiss 的 App 回调执行前就检查 shutdown task。
改为等待真实 shutdown owner 的 Event，保留原操作、HTTP 和账本断言，没有增加任意 sleep。

后一轮相关组 `137 passed, 1 failed`：旧缩放用例暴露终端 Resize 后重排可能仍看到旧聊天宽度。
修复归属在 TUI 布局层：ConversationLog 发出实际尺寸变化通知，App 再执行原重排函数；测试同步到
对应重排事件，原换行和内容断言保留。修复后的直接相邻 28 项与最终七文件均通过。

两项反向验证均保留失败日志，生产源码随后按原字节恢复：

1. 把 preflight 的私有环境副本改回进程环境，公开校验路径使草稿变量进入进程，相关断言失败。
2. 移除 ConversationLog 尺寸通知，真实终端缩放无法到达新宽度重排事件，用例明确失败。

## 静态检查与边界

- `compileall -q src tests`、十个相关 Python 文件 Ruff、`git diff --check` 通过。
- `pytest --collect-only -q` 成功收集 **3377** 项，仅收集，不宣称全量通过。
- 两份上下文同步第 1、4、12、13.11、15、16 节；README、配置指南与 CHANGELOG 同步。
  章节、相对链接与 Mermaid 围栏检查通过；新增通用模块未使用示例身份或本机路径作为隐藏默认。
- 未跑全量、L2–L4、Wheel/安装、真实外部模型/API 或发布门禁；未 commit、push、tag 或 release。
- 配置保存和应用仍是独立动作；插件身份变更和 Product 配置变更遵守原归属约束。Context 仍为
  高级 JSON 编辑器；此轮没有新增向量检索、权限自动批准或插件安装功能。

当前行为见 [配置指南](tui-configuration.md)。[首版面板记录](validation-tui-configuration.md)仅保留历史基线。
