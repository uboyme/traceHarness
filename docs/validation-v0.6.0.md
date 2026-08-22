# TraceHarness Py v0.6.0 验证记录

验证日期：2026-08-23

本记录描述 `v0.6.0` 发布门禁。它补充而不改写最初版本的
[`VALIDATION.md`](../VALIDATION.md)：旧文件仍是当时的历史快照，本文件只记录本次发行事实。

## 1. 发布范围

本次发行包含两条已经接入既有主线的能力：

- L1–L4 受控能力演进控制面：候选编写技能、独立验证、固定 Suite 的
  baseline/candidate 对比，以及人工批准后的精确推广与回滚；
- Stage A–E 多 Agent 控制面：durable Agent identity、Inbox、Delivery、进程内
  Supervisor、owner 子树 child-first disposal，以及宿主显式装配的五个子 Agent Tool。

这些能力没有新增第二个事件事实源、AgentLoop、插件加载器或 Runtime 调度器。
`AgentRuntime` 也不负责多 Agent 队列、候选构建、比较或包管理。

## 2. 自动化门禁

| 门禁 | 结果 |
|---|---:|
| 核心测试收集 | 1707 |
| 核心完整测试 | 1706 passed, 1 skipped |
| Stage A–E 定向集合 | 545 passed |
| Stage E | 30 passed |
| 干净 Wheel E2E | 18 passed |
| Python Quality 插件契约 | 17 passed |
| Plugin Creator Skill 契约 | 10 passed |

唯一 skip 是 Windows 不允许路径包含 NUL 的平台边界。完整门禁包含真实 L2
递归核心回归；修改范围 Ruff、`compileall` 与 `git diff --check` 均通过。

## 3. 真实模型多 Agent 验收

RC 使用真实 OpenAI-compatible Provider 和公开 Runtime/Supervisor/Tool 接口完成：

1. parent 模型依次调用 `spawn_agent`、`send_agent_message`、`wait_agent`、
   `collect_agent_artifact`、`stop_agent`；
2. child 在独立 Session 中完成真实模型 Turn；
3. child 停止后，以相同 durable Agent identity 与 Session identity 显式恢复并完成
   第二个真实模型 Turn；
4. 另一轮执行被确定性 Gate 卡住后，通过 `interrupt()` 收敛为 durable
   `cancelled`，没有把取消误报为成功。

持久化证据为 2 条 Directory identity、1 条 ownership edge、4 条 Inbox acceptance
和 8 条 Delivery lifecycle event。parent 有 1 个闭合 Turn，child 有 3 个闭合 Turn；
两份 Session 都没有开放 Turn/Step，不变量违规和 Request Snapshot 重建违规均为 0。
验证记录不保存 Provider 凭据、模型配置、会话内容或本机路径。

## 4. 打包与干净安装

发行资产包括：

- `traceharness_py-0.6.0-py3-none-any.whl`；
- `traceharness_py-0.6.0.tar.gz`；
- `traceharness-py-v0.6.0-source.zip`；
- `traceh_plugin_creator_skill_plugin-0.2.0-py3-none-any.whl`；
- `traceh_python_quality_plugin-0.2.0-py3-none-any.whl`。

所有包只从干净、受 Git 管理的构建输入生成；构建输入以及 Wheel/源码 ZIP 审计拒绝
`.pyc`、`__pycache__`、旧 `build/`、旧 egg metadata、`.env`、Session 数据和 Git
元数据。按标准生成的 sdist 会携带 setuptools 在本次构建中创建的
`traceharness_py.egg-info`，它不是从工作区混入的旧构建产物。源码 ZIP 只收
`git ls-files` 的发行提交文件，并保持中文文件名和字节内容。

核心 Wheel、两个插件 Wheel 与 `packaging` 运行时依赖随后进入同一个本地
wheelhouse。在不含 `.env` 的新虚拟环境中使用 `--no-index` 离线安装后，确认：

- 核心 Distribution 与 `traceh.version` 都是 `0.6.0`；
- Plugin Creator 和 Python Quality Distribution 都是 `0.2.0`；
- `traceh doctor` 使用无密钥的 Scripted 默认配置通过；
- `traceh plugins list` 能发现两个插件；
- 两个插件的 `traceh plugins doctor` 都通过。

## 5. 安全检查与发布边界

提交和归档扫描不包含真实 API Key、Token、`.env`、Session/Event 数据、缓存、临时
脚本或本机绝对路径。真实模型验收只在仓库外临时目录运行，临时脚本没有进入提交。

`v0.6.0` 仍不提供 OS 沙箱、跨进程 Activation lease、冷恢复、stale claim takeover、
自动 retry/attempt identity、层级 Budget 强制、独立 Workspace/Patch Artifact、Workflow、
MCP、TUI 或模型流式输出。五个子 Agent Tool 也不会被默认 CLI 静默启用；宿主必须
显式提供 Supervisor、owner、preset/workspace 解析和 Tool 装配。
